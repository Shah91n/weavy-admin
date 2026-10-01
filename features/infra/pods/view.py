"""
PodView – the pods in the Kubernetes namespace, in two sections.

* **Pods** – pod name, phase, ready ratio, restart count, age, node, and IP.
  Double-clicking or right-clicking a row emits ``pod_detail_requested(pod_name)``
  so the caller (main_window) can open a PodDetailView tab for that pod.
* **Pod Comparison** – the Weaviate pods side by side: node, zone, instance type,
  version, CPU / memory against the limit, node load and restarts. Values that
  differ from the other pods or run hot are coloured, and a short list of
  findings sits above the table.

Styling
-------
All colours / QSS come from ``shared/styles/infra_qss.py``.
No inline ``setStyleSheet`` calls on individual widgets.
"""

import logging
from datetime import datetime, timezone

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.state import AppState
from core.infra.pods import USAGE_ERROR_PCT, USAGE_WARN_PCT, fmt_bytes, fmt_cpu
from features.infra.pods.worker import PodListWorker
from shared.loading_bar import LoadingBar
from shared.styles.infra_qss import (
    COLOR_BRIDGE_CONNECTED,
    COLOR_BRIDGE_ERROR,
    COLOR_BRIDGE_PENDING,
    INFRA_STYLESHEET,
    INFRA_TEXT_MUTED,
    INFRA_TEXT_PRIMARY,
)
from shared.worker_mixin import WorkerMixin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column indices
# ---------------------------------------------------------------------------
_COL_NAME = 0
_COL_STATUS = 1
_COL_READY = 2
_COL_RESTARTS = 3
_COL_AGE = 4
_COL_NODE = 5
_COL_IP = 6

_HEADERS = ["Pod Name", "Status", "Ready", "Restarts", "Age", "Node", "IP"]

# No Node column: node names are long and already in the Pods table above;
# zone + instance type identify where each pod runs.
_CMP_HEADERS = [
    "Pod",
    "Zone",
    "Instance",
    "Version",
    "CPU used / limit",
    "CPU %",
    "Memory used / limit",
    "Memory %",
    "Node CPU / Mem",
    "Restarts",
]
# Comparison field → column, for the "differs from the other pods" highlight.
_CMP_DIFF_COL = {"instance_type": 2, "version": 3, "cpu_limit_m": 4, "mem_limit_b": 6}
_CMP_DIFF_LABEL = {
    "instance_type": "instance type",
    "version": "version",
    "cpu_limit_m": "CPU limit",
    "mem_limit_b": "memory limit",
}
_INSIGHT_OBJECT = {"ok": "stsHealthOk", "warn": "stsHealthWarn", "error": "stsHealthError"}
_INSIGHT_ICON = {"ok": "✅", "warn": "⚠️", "error": "❌"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _relative_time(ts: str) -> str:
    """Return a compact relative-time string like '3h', '2d', '45m'."""
    if not ts or ts == "N/A":
        return "N/A"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        delta = datetime.now(tz=timezone.utc) - dt
        s = int(delta.total_seconds())
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m"
        if s < 86400:
            return f"{s // 3600}h"
        return f"{s // 86400}d"
    except (ValueError, TypeError):
        return ts


def _phase_color(phase: str) -> str:
    if phase == "Running":
        return COLOR_BRIDGE_CONNECTED
    if phase in ("Pending", "Terminating"):
        return COLOR_BRIDGE_PENDING
    return COLOR_BRIDGE_ERROR


def _phase_icon(phase: str) -> str:
    if phase == "Running":
        return f"✅  {phase}"
    if phase in ("Pending", "Terminating"):
        return f"⚠️  {phase}"
    return f"❌  {phase}"


def _restart_color(count: int) -> str:
    if count == 0:
        return COLOR_BRIDGE_CONNECTED
    if count < 5:
        return COLOR_BRIDGE_PENDING
    return COLOR_BRIDGE_ERROR


def _pod_ready_ratio(pod: dict) -> str:
    """Return 'X/Y' ready containers string."""
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if not statuses:
        return "0/0"
    total = len(statuses)
    ready = sum(1 for s in statuses if s.get("ready", False))
    return f"{ready}/{total}"


def _pod_restarts(pod: dict) -> int:
    """Return total restart count across all containers."""
    statuses = pod.get("status", {}).get("containerStatuses", [])
    return sum(s.get("restartCount", 0) for s in statuses)


def _usage_color(pct: float | None) -> str:
    if pct is None:
        return INFRA_TEXT_MUTED
    if pct >= USAGE_ERROR_PCT:
        return COLOR_BRIDGE_ERROR
    if pct >= USAGE_WARN_PCT:
        return COLOR_BRIDGE_PENDING
    return INFRA_TEXT_PRIMARY


def _fmt_pct(pct: float | None) -> str:
    return "—" if pct is None else f"{pct:.0f}%"


def _fmt_pair(used: str, limit: str) -> str:
    return f"{used} / {limit}" if limit != "—" else used


def _fit_columns(table: QTableWidget, stretch_col: int) -> None:
    """Size every column to its content, stretch one, keep the rest user-resizable.

    Fixed pixel widths cut off long node names and pod names; sizing to content
    once per refresh shows them in full without the user dragging headers.
    """
    header = table.horizontalHeader()
    table.resizeColumnsToContents()
    for col in range(table.columnCount()):
        mode = (
            QHeaderView.ResizeMode.Stretch
            if col == stretch_col
            else QHeaderView.ResizeMode.Interactive
        )
        header.setSectionResizeMode(col, mode)


# ---------------------------------------------------------------------------
# Main view
# ---------------------------------------------------------------------------


class PodView(QWidget, WorkerMixin):
    """
    Pod List View – shows all pods in the namespace as a compact table.

    Parameters
    ----------
    namespace:
        Kubernetes namespace (resolved by the bridge). Pass an empty string
        initially; call :meth:`set_namespace` once the namespace is known.

    Signals
    -------
    pod_detail_requested(str)
        Emitted with the pod name when the user wants to open the detail view.
    pods_loaded(list)
        Emitted with pod name strings after a successful fetch so the caller
        (main_window) can propagate them to the sidebar context menu.
    """

    pod_detail_requested = pyqtSignal(str)
    pods_loaded = pyqtSignal(list)  # list[str] – pod names

    def __init__(self, namespace: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _state = AppState.instance()
        self._namespace = namespace or _state.namespace
        self._worker: PodListWorker | None = None
        self._alive: bool = True
        self._pods_summary = ""

        self.setStyleSheet(INFRA_STYLESHEET)
        self._build_ui()
        _state.namespace_changed.connect(self.set_namespace)

        if self._namespace:
            self.fetch_pods()

    def cleanup(self) -> None:
        import contextlib

        with contextlib.suppress(RuntimeError, TypeError):
            AppState.instance().namespace_changed.disconnect(self.set_namespace)
        self._alive = False
        super().cleanup()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def set_namespace(self, namespace: str) -> None:
        """Push a late-arriving namespace and trigger an initial fetch."""
        self._namespace = namespace
        if namespace:
            self.fetch_pods()

    def fetch_pods(self) -> None:
        """Fetch (or re-fetch) the pod list."""
        if self._worker is not None:
            self._detach_worker()
        if not self._namespace:
            self._status_label.setText("Waiting for namespace…")
            return

        self._set_controls_enabled(False)
        self._status_label.setText("Fetching pods…")
        self._table.setRowCount(0)
        self._cmp_table.setRowCount(0)
        self._set_insights([], [])

        self._worker = PodListWorker(self._namespace, with_comparison=True)
        self._worker.pods_ready.connect(self._on_pods_ready)
        self._worker.comparison_ready.connect(self._on_comparison_ready)
        self._worker.progress.connect(self._status_label.setText)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_toolbar())
        self._loading_bar = LoadingBar()
        layout.addWidget(self._loading_bar)

        # Two sections: the pod list on top, the cross-pod comparison below.
        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(self._build_section("Pods", self._build_table()))
        self._splitter.addWidget(self._build_comparison_section())
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        layout.addWidget(self._splitter, 1)

    @staticmethod
    def _build_section(title: str, body: QWidget) -> QGroupBox:
        group = QGroupBox(title)
        group.setObjectName("stsSection")
        box = QVBoxLayout(group)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(0)
        box.addWidget(body)
        return group

    def _build_comparison_section(self) -> QGroupBox:
        body = QWidget()
        box = QVBoxLayout(body)
        box.setContentsMargins(8, 4, 8, 0)
        box.setSpacing(4)

        # Findings first — the table is the evidence for them.
        self._insights_box = QVBoxLayout()
        self._insights_box.setSpacing(2)
        box.addLayout(self._insights_box)

        self._cmp_table = QTableWidget(0, len(_CMP_HEADERS))
        self._cmp_table.setObjectName("podTable")
        self._cmp_table.setHorizontalHeaderLabels(_CMP_HEADERS)
        self._cmp_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._cmp_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._cmp_table.verticalHeader().setVisible(False)
        self._cmp_table.horizontalHeaderItem(_CMP_HEADERS.index("Node CPU / Mem")).setToolTip(
            "How loaded the node hosting this pod is (CPU % / memory % of allocatable)."
        )
        _fit_columns(self._cmp_table, stretch_col=len(_CMP_HEADERS) - 1)
        box.addWidget(self._cmp_table, 1)
        return self._build_section("Pod Comparison", body)

    def _build_toolbar(self) -> QWidget:
        toolbar = QWidget()
        toolbar.setObjectName("infraToolbar")
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        self._refresh_btn = QPushButton("↻")
        self._refresh_btn.setObjectName("refreshIconBtn")
        self._refresh_btn.setFixedSize(28, 28)
        self._refresh_btn.setToolTip("Refresh Pods")
        self._refresh_btn.clicked.connect(self.fetch_pods)
        row.addWidget(self._refresh_btn)

        row.addStretch()

        hint = QLabel("Double-click a pod to view details")
        hint.setObjectName("infraStatusLabel")
        row.addWidget(hint)

        self._status_label = QLabel("Ready")
        self._status_label.setObjectName("infraPodsStatus")
        row.addWidget(self._status_label)

        return toolbar

    def _build_table(self) -> QTableWidget:
        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setObjectName("podTable")
        self._table.setHorizontalHeaderLabels(_HEADERS)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(False)
        _fit_columns(self._table, stretch_col=_COL_IP)

        self._table.doubleClicked.connect(self._on_row_double_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_row_context_menu)

        return self._table

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_pods_ready(self, pods: list) -> None:
        # Not terminal — comparison_ready follows — so the worker stays attached.
        if not self._alive:
            return
        try:
            self._populate_pods(pods)
        except RuntimeError:
            self._alive = False

    def _on_comparison_ready(self, comparison: dict) -> None:
        self._detach_worker()
        if not self._alive:
            return
        try:
            self._populate_comparison(comparison.get("rows", []))
            self._set_insights(comparison.get("insights", []), comparison.get("notes", []))
            self._status_label.setText(self._pods_summary)
            self._set_controls_enabled(True)
        except RuntimeError:
            self._alive = False

    def _populate_pods(self, pods: list) -> None:
        self._table.setRowCount(0)

        pod_names: list[str] = []

        for pod in pods:
            meta = pod.get("metadata", {})
            status = pod.get("status", {})
            spec = pod.get("spec", {})

            name = meta.get("name", "N/A")
            phase = status.get("phase", "Unknown")
            node = spec.get("nodeName", "N/A")
            pod_ip = status.get("podIP", "N/A")
            start = meta.get("creationTimestamp", "")
            ready = _pod_ready_ratio(pod)
            restarts = _pod_restarts(pod)

            # Check for CrashLoopBackOff or other error states in container statuses
            for cs in status.get("containerStatuses", []):
                waiting = cs.get("state", {}).get("waiting", {})
                reason = waiting.get("reason", "")
                if reason in (
                    "CrashLoopBackOff",
                    "Error",
                    "OOMKilled",
                    "ImagePullBackOff",
                    "ErrImagePull",
                    "CreateContainerConfigError",
                ):
                    phase = reason
                    break

            row = self._table.rowCount()
            self._table.insertRow(row)

            # Pod name
            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setForeground(QBrush(QColor(INFRA_TEXT_PRIMARY)))
            name_item.setData(Qt.ItemDataRole.UserRole, name)
            self._table.setItem(row, _COL_NAME, name_item)

            # Status
            status_item = QTableWidgetItem(_phase_icon(phase))
            status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            status_item.setForeground(QBrush(QColor(_phase_color(phase))))
            self._table.setItem(row, _COL_STATUS, status_item)

            # Ready
            ready_item = QTableWidgetItem(ready)
            ready_item.setFlags(ready_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            ready_item.setForeground(QBrush(QColor(INFRA_TEXT_PRIMARY)))
            ready_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, _COL_READY, ready_item)

            # Restarts
            restart_item = QTableWidgetItem(str(restarts))
            restart_item.setFlags(restart_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            restart_item.setForeground(QBrush(QColor(_restart_color(restarts))))
            restart_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, _COL_RESTARTS, restart_item)

            # Age
            age_item = QTableWidgetItem(_relative_time(start))
            age_item.setFlags(age_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            age_item.setForeground(QBrush(QColor(INFRA_TEXT_MUTED)))
            age_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, _COL_AGE, age_item)

            # Node
            node_item = QTableWidgetItem(node)
            node_item.setFlags(node_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            node_item.setForeground(QBrush(QColor(INFRA_TEXT_MUTED)))
            self._table.setItem(row, _COL_NODE, node_item)

            # IP
            ip_item = QTableWidgetItem(pod_ip)
            ip_item.setFlags(ip_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            ip_item.setForeground(QBrush(QColor(INFRA_TEXT_MUTED)))
            self._table.setItem(row, _COL_IP, ip_item)

            pod_names.append(name)

        _fit_columns(self._table, stretch_col=_COL_IP)
        self._fit_pods_section()
        count = len(pods)
        self._pods_summary = (
            f"{count} pod{'s' if count != 1 else ''}  |  namespace: {self._namespace}"
        )
        self._status_label.setText(self._pods_summary)
        self.pods_loaded.emit(pod_names)

    def _fit_pods_section(self) -> None:
        """Give the Pods section just enough height for its rows (max half the view)."""
        table = self._table
        rows_h = sum(table.rowHeight(r) for r in range(table.rowCount()))
        chrome_h = table.horizontalHeader().sizeHint().height() + 2 * table.frameWidth() + 40
        total = sum(self._splitter.sizes())
        top = min(rows_h + chrome_h, total // 2) if total else rows_h + chrome_h
        self._splitter.setSizes([top, max(total - top, 1)])

    def _populate_comparison(self, rows: list[dict]) -> None:
        table = self._cmp_table
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            node_load = (
                f"{_fmt_pct(row['node_cpu_pct'])} / {_fmt_pct(row['node_mem_pct'])}"
                if row["node_cpu_pct"] is not None or row["node_mem_pct"] is not None
                else "—"
            )
            restarts = str(row["restarts"])
            if row["last_reason"]:
                restarts += f"  (last: {row['last_reason']})"
            cells = [
                (row["pod"], INFRA_TEXT_PRIMARY),
                (row["zone"], INFRA_TEXT_MUTED),
                (row["instance_type"] or "—", INFRA_TEXT_PRIMARY),
                (row["version"], INFRA_TEXT_PRIMARY),
                (
                    _fmt_pair(fmt_cpu(row["cpu_used_m"]), fmt_cpu(row["cpu_limit_m"])),
                    INFRA_TEXT_PRIMARY,
                ),
                (_fmt_pct(row["cpu_pct"]), _usage_color(row["cpu_pct"])),
                (
                    _fmt_pair(fmt_bytes(row["mem_used_b"]), fmt_bytes(row["mem_limit_b"])),
                    INFRA_TEXT_PRIMARY,
                ),
                (_fmt_pct(row["mem_pct"]), _usage_color(row["mem_pct"])),
                (
                    node_load,
                    _usage_color(max(row["node_cpu_pct"] or 0, row["node_mem_pct"] or 0) or None),
                ),
                (restarts, _restart_color(row["restarts"])),
            ]
            for c, (text, color) in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QBrush(QColor(color)))
                item.setToolTip(text)
                table.setItem(r, c, item)

            # Values that should match every other pod but don't.
            for field, common in row["differs"].items():
                item = table.item(r, _CMP_DIFF_COL[field])
                if item is None:
                    continue
                if field == "cpu_limit_m":
                    common_text = fmt_cpu(common)
                elif field == "mem_limit_b":
                    common_text = fmt_bytes(common)
                else:
                    common_text = str(common)
                item.setForeground(QBrush(QColor(COLOR_BRIDGE_PENDING)))
                item.setToolTip(
                    f"Differs from the other pods — their {_CMP_DIFF_LABEL[field]} is {common_text}."
                )

        _fit_columns(table, stretch_col=len(_CMP_HEADERS) - 1)

    def _set_insights(self, insights: list, notes: list) -> None:
        """Replace the findings list above the comparison table."""
        while self._insights_box.count():
            widget = self._insights_box.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        for level, text in insights:
            label = QLabel(f"{_INSIGHT_ICON[level]}   {text}")
            label.setObjectName(_INSIGHT_OBJECT[level])
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._insights_box.addWidget(label)
        for note in notes:
            label = QLabel(note)
            label.setObjectName("infraStatusLabel")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._insights_box.addWidget(label)

    def _on_error(self, msg: str) -> None:
        self._detach_worker()
        if not self._alive:
            return
        try:
            self._status_label.setText(f"Error: {msg}")
            self._set_controls_enabled(True)
            logger.error("PodListWorker error: %s", msg)
        except RuntimeError:
            self._alive = False

    def _on_row_double_clicked(self, index) -> None:
        pod_name = self._pod_name_at_row(index.row())
        if pod_name:
            self.pod_detail_requested.emit(pod_name)

    def _show_row_context_menu(self, pos) -> None:
        row = self._table.rowAt(pos.y())
        if row < 0:
            return
        pod_name = self._pod_name_at_row(row)
        if not pod_name:
            return

        menu = QMenu(self)
        detail_action = menu.addAction(f"📋  View Details — {pod_name}")
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == detail_action:
            self.pod_detail_requested.emit(pod_name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pod_name_at_row(self, row: int) -> str | None:
        item = self._table.item(row, _COL_NAME)
        if item:
            return item.data(Qt.ItemDataRole.UserRole)
        return None

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._loading_bar.set_busy(not enabled)
        self._refresh_btn.setEnabled(enabled)
