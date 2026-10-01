"""
Log Explorer – fetches and displays Kubernetes logs for Weaviate pods.

Components
----------
* ``LogView``       – the main widget (toolbar + table).
* ``JsonDialog``    – raw-JSON viewer opened on row double-click.

Styling
-------
All colours / QSS come from ``shared/styles/infra_qss.py``.
No inline ``setStyleSheet`` calls are made in this module.
"""

import json
import logging

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QFontDatabase
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.state import AppState
from features.infra.logs.worker import LogWorker
from shared.detail_pane import RowDetailPane, build_detail_fields
from shared.loading_bar import LoadingBar
from shared.styles.infra_qss import (
    COLOR_LEVEL_DEBUG_TEXT,
    COLOR_LEVEL_INFO_TEXT,
    COLOR_LEVEL_PANIC_ERROR_TEXT,
    COLOR_LEVEL_TRACE_TEXT,
    COLOR_LEVEL_WARNING_TEXT,
    INFRA_STYLESHEET,
    INFRA_TEXT_PRIMARY,
)
from shared.worker_mixin import WorkerMixin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Time window options
# ---------------------------------------------------------------------------
# Each tuple: (display label, value passed to ``kubectl logs --since``).
# kubectl takes Go durations, which have no "d" unit — days are written in hours.

_TIME_WINDOWS: list[tuple[str, str]] = [
    ("Last 15 minutes", "15m"),
    ("Last 1 hour", "1h"),
    ("Last 6 hours", "6h"),
    ("Last 12 hours", "12h"),
    ("Last 24 hours", "24h"),
    ("Last 3 days", "72h"),
    ("Last 7 days", "168h"),
]
_DEFAULT_TIME_WINDOW = 1  # "Last 1 hour"

# Max lines fetched per pod (``kubectl logs --tail``). Fetching is cheap; the cost
# is building the table on the UI thread afterwards, which grows with row count.
_TAIL_DEFAULT = 5000
_TAIL_MIN = 100
_TAIL_MAX = 100_000
_TAIL_STEP = 1000
_TAIL_SLOW_THRESHOLD = 20_000  # above this per pod, warn that it will take a while

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

COLUMNS = ["Timestamp", "Level", "Action", "Message", "User", "Request", "Method", "Pod"]

# Level → row text colour. Only the foreground is honoured: a QSS background on
# QTableWidget overrides QTableWidgetItem.setBackground(), so a background map
# here would silently render nothing.
_LEVEL_FG: dict[str, str] = {
    "PANIC": COLOR_LEVEL_PANIC_ERROR_TEXT,
    "FATAL": COLOR_LEVEL_PANIC_ERROR_TEXT,
    "ERROR": COLOR_LEVEL_PANIC_ERROR_TEXT,
    "WARNING": COLOR_LEVEL_WARNING_TEXT,
    "WARN": COLOR_LEVEL_WARNING_TEXT,
    "INFO": COLOR_LEVEL_INFO_TEXT,
    "DEBUG": COLOR_LEVEL_DEBUG_TEXT,
    "TRACE": COLOR_LEVEL_TRACE_TEXT,
}
COL_TIMESTAMP = 0
COL_LEVEL = 1
COL_ACTION = 2
COL_MESSAGE = 3
COL_USER = 4
COL_REQUEST = 5
COL_METHOD = 6
COL_POD = 7


# ---------------------------------------------------------------------------
# Raw-JSON viewer dialog
# ---------------------------------------------------------------------------


class JsonDialog(QDialog):
    """Show the raw JSON source of a log entry."""

    def __init__(self, raw: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Log Entry – Raw JSON")
        self.setMinimumSize(640, 460)
        self.setStyleSheet(INFRA_STYLESHEET)
        self._build_ui(raw)

    def _build_ui(self, raw: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Pretty-print if possible
        try:
            pretty = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pretty = raw

        text = QTextEdit()
        text.setObjectName("infraJsonText")
        text.setReadOnly(True)
        text.setPlainText(pretty)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setPointSize(11)
        text.setFont(mono)
        layout.addWidget(text)

        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)


# ---------------------------------------------------------------------------
# Main log view
# ---------------------------------------------------------------------------


class LogView(QWidget, WorkerMixin):
    """
    Log Explorer widget.

    Parameters
    ----------
    namespace:
        Kubernetes namespace to fetch logs from.
        Pass an empty string to show the "not configured" state.
    """

    def __init__(self, namespace: str = "", parent=None) -> None:
        super().__init__(parent)
        _state = AppState.instance()
        self._namespace = namespace or _state.namespace
        self._all_entries: list[dict] = []
        self._worker: LogWorker | None = None
        self._alive: bool = True
        self.setStyleSheet(INFRA_STYLESHEET)
        self._build_ui()
        _state.namespace_changed.connect(self.set_namespace)

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
        """Change the target namespace and refresh the status label."""
        self._namespace = namespace
        self._update_status(
            f"Namespace: {namespace} — click 'Get Logs' to fetch."
            if namespace
            else "No namespace configured."
        )

    def get_logs(self) -> None:
        """Fetch the latest logs from the configured namespace."""
        if not self._namespace:
            self._update_status("No namespace configured. Cannot fetch logs.")
            return
        if self._worker is not None:
            self._detach_worker()

        label, since = _TIME_WINDOWS[self._time_combo.currentIndex()]
        tail = self._tail_spin.value()
        slow_hint = " — large limit, this may take a while" if tail > _TAIL_SLOW_THRESHOLD else ""

        self._set_controls_enabled(False)
        self._update_status(
            f"Fetching up to {tail:,} lines/pod ({label}) from '{self._namespace}'{slow_hint} …"
        )
        self._table.setRowCount(0)

        self._worker = LogWorker(self._namespace, tail=tail, since=since)
        self._worker.logs_ready.connect(self._on_logs_ready)
        self._worker.progress.connect(self._update_status)
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

        # Table over an expandable detail pane — selecting a row shows every
        # field in full, so long paths / messages no longer need a modal.
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setObjectName("detailSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_table())
        self._detail_pane = RowDetailPane()
        splitter.addWidget(self._detail_pane)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([460, 240])  # px: table keeps roughly two thirds
        layout.addWidget(splitter, 1)

    def _build_toolbar(self) -> QWidget:
        toolbar = QWidget()
        toolbar.setObjectName("infraToolbar")
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        # Time window selector
        time_label = QLabel("Time window:")
        time_label.setObjectName("infraStatusLabel")
        row.addWidget(time_label)

        self._time_combo = QComboBox()
        self._time_combo.setObjectName("infraFilterCombo")
        for display, _ in _TIME_WINDOWS:
            self._time_combo.addItem(display)
        self._time_combo.setCurrentIndex(_DEFAULT_TIME_WINDOW)
        self._time_combo.setToolTip(
            "How far back to fetch logs.\n"
            "Only covers the current container — lines from before a pod restart are gone."
        )
        row.addWidget(self._time_combo)

        # Per-pod line limit — always set (a spin box cannot be empty)
        tail_label = QLabel("Lines/pod:")
        tail_label.setObjectName("infraStatusLabel")
        row.addWidget(tail_label)

        self._tail_spin = QSpinBox()
        self._tail_spin.setRange(_TAIL_MIN, _TAIL_MAX)
        self._tail_spin.setSingleStep(_TAIL_STEP)
        self._tail_spin.setValue(_TAIL_DEFAULT)
        self._tail_spin.setGroupSeparatorShown(True)
        self._tail_spin.setToolTip(
            "Maximum lines fetched from each pod — the newest ones within the time window.\n"
            f"Higher limits show more history but take longer to load and filter\n"
            f"(above {_TAIL_SLOW_THRESHOLD:,} per pod expect a noticeable wait)."
        )
        row.addWidget(self._tail_spin)

        # Get Logs button
        self._get_logs_btn = QPushButton("Get Logs")
        self._get_logs_btn.setToolTip(
            "Fetch log lines from each pod for the selected time window and line limit."
        )
        self._get_logs_btn.clicked.connect(self.get_logs)
        row.addWidget(self._get_logs_btn)

        # Level filter (standalone dropdown)
        level_label = QLabel("Level:")
        level_label.setObjectName("infraStatusLabel")
        row.addWidget(level_label)

        self._level_combo = QComboBox()
        self._level_combo.setObjectName("infraFilterCombo")
        self._level_combo.addItems(
            ["All Levels", "INFO", "WARNING", "ERROR", "DEBUG", "TRACE", "PANIC", "FATAL"]
        )
        self._level_combo.setFixedWidth(120)
        self._level_combo.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self._level_combo)

        # Type filter (field selector, works with search bar)
        type_label = QLabel("Type:")
        type_label.setObjectName("infraStatusLabel")
        row.addWidget(type_label)

        self._type_combo = QComboBox()
        self._type_combo.setObjectName("infraFilterCombo")
        self._type_combo.addItems(["Message", "Action", "User", "Method", "Pod"])
        self._type_combo.setFixedWidth(100)
        self._type_combo.setToolTip(
            "Field the Search box matches against.\n"
            "Pick 'Pod' and type e.g. 'weaviate-0' to keep only that pod's rows."
        )
        self._type_combo.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self._type_combo)

        # Search bar (searches the field selected in Type combo)
        self._search_bar = QLineEdit()
        self._search_bar.setObjectName("infraSearchBar")
        self._search_bar.setPlaceholderText("Search selected field …")
        self._search_bar.textChanged.connect(self._apply_filter)
        self._search_bar.setMinimumWidth(80)
        row.addWidget(self._search_bar)

        # Separator label
        sep = QLabel("|")
        sep.setObjectName("infraStatusLabel")
        row.addWidget(sep)

        # Exclude field selector
        excl_label = QLabel("Exclude:")
        excl_label.setObjectName("infraStatusLabel")
        row.addWidget(excl_label)

        self._excl_field_combo = QComboBox()
        self._excl_field_combo.setObjectName("infraFilterCombo")
        self._excl_field_combo.addItems(["Action", "Message", "User", "Method", "Pod"])
        self._excl_field_combo.setFixedWidth(100)
        self._excl_field_combo.setToolTip(
            "Field to apply the exclusion term against.\n"
            "Rows where this field contains the exclusion text will be hidden.\n"
            "Pick 'Pod' to hide one or more pods by name (comma-separated)."
        )
        self._excl_field_combo.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self._excl_field_combo)

        # Exclude input bar
        self._excl_bar = QLineEdit()
        self._excl_bar.setObjectName("infraSearchBar")
        self._excl_bar.setPlaceholderText("Exclude term …")
        self._excl_bar.setToolTip(
            "Hide rows where the selected Exclude field contains this text.\n"
            "Separate multiple terms with commas."
        )
        self._excl_bar.textChanged.connect(self._apply_filter)
        self._excl_bar.setMinimumWidth(80)
        row.addWidget(self._excl_bar)

        row.addStretch()

        # Status
        self._status_label = QLabel(
            f"Namespace: {self._namespace} — click 'Get Logs' to fetch."
            if self._namespace
            else "No namespace configured."
        )
        self._status_label.setObjectName("infraLogStatus")
        row.addWidget(self._status_label)

        return toolbar

    def _build_table(self) -> QTableWidget:
        self._table = QTableWidget()
        self._table.setObjectName("infraLogTable")
        self._table.setColumnCount(len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(False)  # manual level colours
        self._table.setWordWrap(False)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)

        h_header = self._table.horizontalHeader()
        h_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h_header.setSectionResizeMode(COL_MESSAGE, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

        self._table.cellDoubleClicked.connect(self._on_row_double_clicked)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        return self._table

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_logs_ready(self, entries: list[dict]) -> None:
        self._detach_worker()
        if not self._alive:
            return
        try:
            self._all_entries = entries
            self._apply_filter()
            self._update_status(f"{len(entries):,} entries loaded.")
            self._set_controls_enabled(True)
        except RuntimeError:
            self._alive = False

    def _on_error(self, msg: str) -> None:
        self._detach_worker()
        if not self._alive:
            return
        try:
            self._update_status(f"Error: {msg}")
            self._set_controls_enabled(True)
            logger.error("LogWorker error: %s", msg)
        except RuntimeError:
            self._alive = False

    def _on_row_selected(self) -> None:
        """Fill the detail pane from the selected row."""
        if not self._alive:
            return
        rows = {idx.row() for idx in self._table.selectedIndexes()}
        if not rows:
            self._detail_pane.clear_entry()
            return
        fields, raw = build_detail_fields(self._table, min(rows), COLUMNS, COL_TIMESTAMP)
        self._detail_pane.show_entry(fields, raw)

    def _on_row_double_clicked(self, row: int, _col: int) -> None:
        # Retrieve raw JSON stored in the hidden user-data of the first cell
        item = self._table.item(row, COL_TIMESTAMP)
        if item is None:
            return
        raw = item.data(Qt.ItemDataRole.UserRole) or ""
        dlg = JsonDialog(raw, parent=self)
        dlg.exec()

    def _apply_filter(self) -> None:
        """Filter displayed rows combining the Level dropdown, Type+search bar, and Exclude field+bar."""
        if not self._alive:
            return
        selected_level = self._level_combo.currentText()  # e.g. "INFO" or "All Levels"
        query = self._search_bar.text().strip().lower()
        field = self._type_combo.currentText()  # e.g. "Message", "Action", …
        excl_field = self._excl_field_combo.currentText()
        excl_terms = [t.strip().lower() for t in self._excl_bar.text().split(",") if t.strip()]

        filtered = [
            e
            for e in self._all_entries
            if self._level_matches(e, selected_level)
            and self._text_matches(e, field, query)
            and not self._is_excluded(e, excl_field, excl_terms)
        ]
        self._populate_table(filtered)

    # ------------------------------------------------------------------
    # Table population
    # ------------------------------------------------------------------

    def _populate_table(self, entries: list[dict]) -> None:
        if not self._alive:
            return
        try:
            # Disable sorting during population to avoid mid-insert reordering
            self._table.setSortingEnabled(False)
            self._table.setUpdatesEnabled(False)
            self._table.setRowCount(0)
            self._table.setRowCount(len(entries))

            for row, entry in enumerate(entries):
                level = entry.get("level", "").upper()

                cells = [
                    entry.get("timestamp", ""),
                    level,
                    entry.get("action", ""),
                    entry.get("message", ""),
                    entry.get("user", ""),
                    entry.get("request", ""),
                    entry.get("method", ""),
                    entry.get("pod", ""),
                ]

                fg = QBrush(QColor(_LEVEL_FG.get(level, INFRA_TEXT_PRIMARY)))

                for col, text in enumerate(cells):
                    item = QTableWidgetItem(str(text))
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    item.setForeground(fg)

                    if col == COL_TIMESTAMP:
                        item.setData(Qt.ItemDataRole.UserRole, entry.get("raw", ""))

                    self._table.setItem(row, col, item)

            self._table.setUpdatesEnabled(True)
            self._table.setSortingEnabled(True)
        except RuntimeError:
            self._alive = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _level_matches(entry: dict, selected_level: str) -> bool:
        """Return True when the entry's level matches the dropdown selection."""
        if selected_level == "All Levels":
            return True
        level = entry.get("level", "").upper()
        # Normalise WARN → WARNING
        if level == "WARN":
            level = "WARNING"
        return level == selected_level.upper()

    @staticmethod
    def _text_matches(entry: dict, field: str, query: str) -> bool:
        """Return True when *query* matches the chosen *field* (or always if query is empty)."""
        if not query:
            return True
        field_map = {
            "Message": (entry.get("message", "") + " " + entry.get("raw", "")),
            "Action": entry.get("action", ""),
            "Request": entry.get("request", ""),
            "User": entry.get("user", ""),
            "Method": entry.get("method", ""),
            "Pod": entry.get("pod", ""),
        }
        haystack = field_map.get(field, entry.get("message", ""))
        return query in haystack.lower()

    @staticmethod
    def _is_excluded(entry: dict, excl_field: str, excl_terms: list[str]) -> bool:
        """Return True when *any* of *excl_terms* is found in the entry's *excl_field*.

        Supports comma-separated multi-term exclusion: each term is checked
        independently against only the specified field so that e.g. excluding
        ``"authorize"`` from Action does not suppress rows that merely contain
        that word in their Message.
        """
        if not excl_terms:
            return False
        field_map = {
            "Action": entry.get("action", ""),
            "Message": entry.get("message", ""),
            "User": entry.get("user", ""),
            "Method": entry.get("method", ""),
            "Pod": entry.get("pod", ""),
        }
        haystack = field_map.get(excl_field, "").lower()
        return any(term in haystack for term in excl_terms)

    def _update_status(self, msg: str) -> None:
        self._status_label.setText(msg)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._loading_bar.set_busy(not enabled)
        self._time_combo.setEnabled(enabled)
        self._tail_spin.setEnabled(enabled)
        self._get_logs_btn.setEnabled(enabled)
        self._search_bar.setEnabled(enabled)
        self._level_combo.setEnabled(enabled)
        self._type_combo.setEnabled(enabled)
        self._excl_field_combo.setEnabled(enabled)
        self._excl_bar.setEnabled(enabled)
