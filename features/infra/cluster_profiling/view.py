"""
ClusterProfilingView – a full tab view (not a dialog) for capturing pprof
profiles from every Weaviate pod in the cluster.

Layout
------
┌─────────────────────────────────────────────────────────┐
│ CLUSTER PROFILING                                        │
│ Cluster: weaviate-abc123                                 │
├──────────────────────────────── Settings ────────────────┤
│ Duration: ◉ 30s  ○ 60s  ○ 120s                          │
│ Save to:  [Choose Folder…]  /Users/x/profiles           │
│                                                          │
│ [▶ Start Capture]   [◼ Cancel]                          │
├──────────────────────────────── Capture Output ──────────┤
│ weaviate-0   [##########]  100%  ✅ Complete             │
│ weaviate-1   [#####·····]   50%  Capturing heap…         │
│ weaviate-2   [··········]    0%  Waiting…                │
│ Overall      [#######···]   56%  (1 of 3 pods)           │
│ ──────────────────────────────────────────────────────── │
│ ▶ Connecting to cluster: weaviate-abc123                 │
│ ▶ Found 3 pod(s): weaviate-0, weaviate-1, weaviate-2    │
│ ▶ Created output directory: /Users/x/…                  │
│ ==================================================       │
│ ▶ Processing pod: weaviate-0  (1/3)                      │
│   → Starting port-forward for weaviate-0…               │
│   → Downloading profiles (duration: 30s)…               │
│     - cpu…                                               │
│       ✓ cpu.pb.gz  (2341 KB)                            │
│     - fgprof…                                            │
│ …                                                        │
└──────────────────────────────────────────────────────────┘

Pod status and the live log share one bordered panel and one monospace font.
The status block is pinned at the top and rewritten in place on every progress
signal; the log scrolls underneath it and mirrors exactly what the reference
bash script prints.  The view keeps running when the user switches tabs.
"""

import logging
import os
import subprocess

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.state import AppState
from features.infra.cluster_profiling.worker import ClusterProfilingWorker
from shared.styles.infra_qss import INFRA_STYLESHEET
from shared.worker_mixin import WorkerMixin

logger = logging.getLogger(__name__)

_SETTINGS_KEY_FOLDER = "profiling/last_save_dir"
_SETTINGS_KEY_DUR = "profiling/cluster_duration"

# Text progress bar drawn in the pinned status block — same font as the log.
_BAR_WIDTH = 10
_BAR_FULL = "#"
_BAR_EMPTY = "·"
_STATUS_BLOCK_MAX_ROWS = 8  # status block scrolls past this; log keeps the space


class ClusterProfilingView(QWidget, WorkerMixin):
    """
    Full-tab cluster profiling view.

    Parameters
    ----------
    namespace:
        Kubernetes namespace (= cluster_id in WCS).
    cluster_id:
        Human-readable cluster identifier.
    """

    def __init__(self, namespace: str = "", cluster_id: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("profilingView")
        self.setStyleSheet(INFRA_STYLESHEET)

        _state = AppState.instance()
        self.namespace = namespace or _state.namespace
        self.cluster_id = cluster_id or self.namespace
        self._settings = QSettings()
        self._worker: ClusterProfilingWorker | None = None
        # Insertion-ordered: pod name -> (percent, status text). Rendered as the
        # pinned text block above the log rather than as per-pod widgets.
        self._pod_state: dict[str, tuple[int, str]] = {}
        self._overall: tuple[int, int] = (0, 0)
        self._final_dir = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)

        # Header
        hdr = QLabel("CLUSTER PROFILING")
        hdr.setObjectName("profilingSectionHeader")
        root.addWidget(hdr)

        cluster_lbl = QLabel(f"Cluster: {self.cluster_id}")
        cluster_lbl.setObjectName("infraStatusLabel")
        root.addWidget(cluster_lbl)

        # Settings panel
        root.addWidget(self._build_settings_panel())

        # Capture output — pod status and the live log share one bordered panel
        # and one monospace font, so the whole capture reads as a single stream.
        out_hdr = QLabel("Capture Output")
        out_hdr.setObjectName("profilingSectionSubHeader")
        root.addWidget(out_hdr)

        out_box = QWidget()
        out_box.setObjectName("profilingOutputBox")
        out_layout = QVBoxLayout(out_box)
        out_layout.setContentsMargins(0, 0, 0, 0)
        out_layout.setSpacing(0)

        self._status_block = QPlainTextEdit()
        self._status_block.setReadOnly(True)
        self._status_block.setObjectName("profilingStatusBlock")
        self._status_block.setFont(self._mono_font())
        self._status_block.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._status_block.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._status_block.setVisible(False)  # nothing to pin before a capture
        out_layout.addWidget(self._status_block)

        self._status_divider = QFrame()
        self._status_divider.setObjectName("profilingOutputDivider")
        self._status_divider.setVisible(False)
        out_layout.addWidget(self._status_divider)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setObjectName("profilingLog")
        self._log.setFont(self._mono_font())
        self._log.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        out_layout.addWidget(self._log, 1)

        root.addWidget(out_box, 1)

        # Result bar (hidden until done)
        self._result_bar = QWidget()
        self._result_bar.setVisible(False)
        result_l = QHBoxLayout(self._result_bar)
        result_l.setContentsMargins(0, 4, 0, 0)
        self._result_label = QLabel()
        self._result_label.setObjectName("profilingSectionSubHeader")
        self._result_label.setWordWrap(True)
        result_l.addWidget(self._result_label, 1)
        open_btn = QPushButton("Open Folder")
        open_btn.clicked.connect(self._open_final_dir)
        result_l.addWidget(open_btn)
        root.addWidget(self._result_bar)

    # ------------------------------------------------------------------ #
    # Settings panel                                                       #
    # ------------------------------------------------------------------ #

    def _build_settings_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("profilingSection")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        # Duration
        dur_row = QHBoxLayout()
        dur_lbl = QLabel("Duration:")
        dur_lbl.setObjectName("infraStatusLabel")
        dur_lbl.setFixedWidth(70)
        dur_row.addWidget(dur_lbl)
        self._dur_group = QButtonGroup(self)
        saved_dur = int(self._settings.value(_SETTINGS_KEY_DUR, 30))
        for label, secs in [("30s", 30), ("60s", 60), ("120s", 120)]:
            rb = QRadioButton(label)
            rb.setProperty("duration_secs", secs)
            if secs == saved_dur:
                rb.setChecked(True)
            self._dur_group.addButton(rb)
            dur_row.addWidget(rb)
        dur_row.addStretch()
        layout.addLayout(dur_row)

        # Ensure one is always selected
        if not self._dur_group.checkedButton() and self._dur_group.buttons():
            self._dur_group.buttons()[0].setChecked(True)

        # Save folder
        folder_row = QHBoxLayout()
        choose_btn = QPushButton("Choose Folder…")
        choose_btn.clicked.connect(self._choose_folder)
        folder_row.addWidget(choose_btn)
        self._folder_label = QLabel(
            self._settings.value(_SETTINGS_KEY_FOLDER, os.path.expanduser("~"))
        )
        self._folder_label.setObjectName("infraStatusLabel")
        self._folder_label.setWordWrap(True)
        folder_row.addWidget(self._folder_label, 1)
        layout.addLayout(folder_row)

        # Action buttons
        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("Start Capture")
        self._start_btn.clicked.connect(self._start_capture)
        btn_row.addWidget(self._start_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel_capture)
        btn_row.addWidget(self._cancel_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)
        return panel

    # ------------------------------------------------------------------ #
    # Capture flow                                                         #
    # ------------------------------------------------------------------ #

    def _start_capture(self) -> None:
        if self._worker is not None:
            return

        btn = self._dur_group.checkedButton()
        duration = btn.property("duration_secs") if btn else 30
        self._settings.setValue(_SETTINGS_KEY_DUR, duration)

        base_dir = self._settings.value(_SETTINGS_KEY_FOLDER, os.path.expanduser("~"))

        # Reset UI
        self._log.clear()
        self._result_bar.setVisible(False)
        self._pod_state.clear()
        self._overall = (0, 0)
        self._render_status_block()

        self._start_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

        self._worker = ClusterProfilingWorker(
            namespace=self.namespace,
            duration=duration,
            base_save_dir=base_dir,
            cluster_id=self.cluster_id,
        )
        self._worker.log_line.connect(self._append_log)
        self._worker.pod_started.connect(self._on_pod_started)
        self._worker.pod_progress.connect(self._on_pod_progress)
        self._worker.pod_complete.connect(self._on_pod_complete)
        self._worker.overall_progress.connect(self._on_overall_progress)
        self._worker.all_complete.connect(self._on_all_complete)
        self._worker.pod_error.connect(self._on_pod_error)
        self._worker.fatal_error.connect(self._on_fatal_error)
        self._worker.start()

    def cleanup(self) -> None:
        super().cleanup()

    def _detach_worker(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        super()._detach_worker()

    def _cancel_capture(self) -> None:
        if self._worker:
            self._worker.cancel()
        self._start_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)

    # ------------------------------------------------------------------ #
    # Worker signal handlers                                               #
    # ------------------------------------------------------------------ #

    def _append_log(self, line: str) -> None:
        self._log.appendPlainText(line)
        self._log.moveCursor(QTextCursor.MoveOperation.End)

    def _set_pod(self, pod_name: str, pct: int, status: str) -> None:
        self._pod_state[pod_name] = (pct, status)
        self._render_status_block()

    def _on_pod_started(self, pod_name: str) -> None:
        self._set_pod(pod_name, 10, "Starting…")

    def _on_pod_progress(self, pod_name: str, msg: str) -> None:
        if pod_name in self._pod_state:
            self._set_pod(pod_name, 50, msg)

    def _on_pod_complete(self, pod_name: str, success: bool) -> None:
        self._set_pod(pod_name, 100, "✅ Complete" if success else "❌ Failed")

    def _on_pod_error(self, pod_name: str, msg: str) -> None:
        self._set_pod(pod_name, 100, f"⚠️ {msg}")

    def _on_overall_progress(self, done: int, total: int) -> None:
        self._overall = (done, total)
        self._render_status_block()

    def _on_all_complete(self, save_dir: str) -> None:
        self._detach_worker()
        self._final_dir = save_dir
        self._start_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        done, total = self._overall
        self._overall = (total or done, total or done)
        self._render_status_block()
        self._result_label.setText(f"✅ Profiles saved to: {save_dir}")
        self._result_bar.setVisible(True)

    def _on_fatal_error(self, msg: str) -> None:
        self._detach_worker()
        self._start_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._append_log(f"❌ Fatal error: {msg}")

    # ------------------------------------------------------------------ #
    # Pinned status block                                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _mono_font() -> QFont:
        """The one font shared by the status block and the log."""
        font = QFont("Menlo")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFamilies(["Menlo", "Consolas", "Courier New", "monospace"])
        font.setPointSize(11)
        return font

    @staticmethod
    def _text_bar(pct: int) -> str:
        filled = round(max(0, min(100, pct)) / 100 * _BAR_WIDTH)
        return f"[{_BAR_FULL * filled}{_BAR_EMPTY * (_BAR_WIDTH - filled)}]"

    def _render_status_block(self) -> None:
        """
        Rewrite the pinned block above the log.

        Cheap enough to redo wholesale on every signal — it is a handful of
        lines — and rewriting avoids having to track per-line cursor positions.
        """
        lines: list[str] = []
        name_width = max((len(n) for n in self._pod_state), default=0)
        name_width = max(name_width, len("Overall"))

        for pod_name, (pct, status) in self._pod_state.items():
            lines.append(f"{pod_name:<{name_width}}  {self._text_bar(pct)} {pct:>4}%  {status}")

        done, total = self._overall
        if total:
            pct = int(done / total * 100)
            lines.append(
                f"{'Overall':<{name_width}}  {self._text_bar(pct)} {pct:>4}%  "
                f"({done} of {total} pods)"
            )

        has_content = bool(lines)
        self._status_block.setVisible(has_content)
        self._status_divider.setVisible(has_content)
        if not has_content:
            self._status_block.clear()
            return

        self._status_block.setPlainText("\n".join(lines))
        self._resize_status_block(len(lines))

    def _resize_status_block(self, line_count: int) -> None:
        """Grow the block to fit its lines so the log keeps the remaining space."""
        rows = min(line_count, _STATUS_BLOCK_MAX_ROWS)
        line_height = self._status_block.fontMetrics().lineSpacing()
        margins = self._status_block.contentsMargins()
        # 12px = the 6px QSS padding top and bottom; 2px keeps the last line clear.
        height = rows * line_height + margins.top() + margins.bottom() + 14
        self._status_block.setFixedHeight(height)

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _choose_folder(self) -> None:
        current = self._settings.value(_SETTINGS_KEY_FOLDER, os.path.expanduser("~"))
        folder = QFileDialog.getExistingDirectory(self, "Choose Save Folder", current)
        if folder:
            self._settings.setValue(_SETTINGS_KEY_FOLDER, folder)
            self._folder_label.setText(folder)

    def _open_final_dir(self) -> None:
        if not self._final_dir or not os.path.exists(self._final_dir):
            return
        subprocess.Popen(["open", self._final_dir])
