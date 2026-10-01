"""
shared/detail_pane.py
=====================
RowDetailPane – the expandable detail panel that sits under a log-style table.

Three infra views (LB Traffic, Logs, RBAC Log) show wide rows whose interesting
values — paths, messages, user agents — are far too long for a single-line cell.
Selecting a row fills this pane with every field in full plus the pretty-printed
raw JSON, so reading an entry never requires opening a modal.

Widget styling lives in ``shared/styles/infra_qss.py`` (``INFRA_DETAIL_PANE_QSS``).
The body is a rich-text document, which a widget stylesheet cannot reach into, so
its colours come from ``INFRA_DETAIL_HTML_CSS`` — injected as a <style> block.
"""

from __future__ import annotations

import html
import json

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from shared.styles.infra_qss import INFRA_DETAIL_HTML_CSS

_EMPTY_HINT = "Select a row to see its full detail."
_COPY_FEEDBACK_MS = 1200


class RowDetailPane(QWidget):
    """
    Collapsible detail panel for the currently selected table row.

    Put it in a ``QSplitter`` below the table, then call :meth:`show_entry`
    from the table's ``itemSelectionChanged`` handler.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("detailPane")
        self._raw = ""
        self._expanded = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header — collapse toggle on the left, copy action on the right.
        header = QWidget()
        header.setObjectName("detailPaneHeader")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(8, 4, 8, 4)
        header_row.setSpacing(8)

        self._toggle_btn = QPushButton("▼ Details")
        self._toggle_btn.setObjectName("stsCollapseBtn")
        self._toggle_btn.setToolTip("Collapse or expand the detail pane")
        self._toggle_btn.clicked.connect(self._on_toggle)
        header_row.addWidget(self._toggle_btn)
        header_row.addStretch()

        self._copy_btn = QPushButton("Copy JSON")
        self._copy_btn.setEnabled(False)
        self._copy_btn.clicked.connect(self._on_copy)
        header_row.addWidget(self._copy_btn)
        layout.addWidget(header)

        # Body — fields then raw JSON, one monospace read-only block.
        self._body = QTextEdit()
        self._body.setObjectName("detailPaneBody")
        self._body.setReadOnly(True)
        self._body.setFont(self._mono_font())
        self._body.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._body.setPlainText(_EMPTY_HINT)
        layout.addWidget(self._body, 1)

        # Parented to self, so it dies with the pane rather than firing into a
        # deleted widget after the tab is closed.
        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(self._reset_copy_btn)

    # ------------------------------------------------------------------ api

    def show_entry(self, fields: list[tuple[str, str]], raw: str = "") -> None:
        """Render one row: ``fields`` as label/value pairs, then ``raw`` as JSON."""
        self._raw = raw or ""
        self._reset_copy_btn()
        self._body.setHtml(self._build_html(fields, self._raw))
        self._body.verticalScrollBar().setValue(0)

    def clear_entry(self) -> None:
        """Return to the empty state — no row selected."""
        self._raw = ""
        self._reset_copy_btn()
        self._body.setPlainText(_EMPTY_HINT)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._body.setVisible(expanded)
        self._toggle_btn.setText("▼ Details" if expanded else "▶ Details")

    # -------------------------------------------------------------- internals

    def _on_toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def _on_copy(self) -> None:
        if not self._raw:
            return
        QApplication.clipboard().setText(self._pretty(self._raw))
        self._copy_btn.setText("Copied")
        self._copy_timer.start(_COPY_FEEDBACK_MS)

    def _reset_copy_btn(self) -> None:
        self._copy_timer.stop()
        self._copy_btn.setText("Copy JSON")
        self._copy_btn.setEnabled(bool(self._raw))

    @staticmethod
    def _mono_font() -> QFont:
        font: QFont = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(11)
        return font

    @staticmethod
    def _pretty(raw: str) -> str:
        try:
            return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            return raw

    @classmethod
    def _build_html(cls, fields: list[tuple[str, str]], raw: str) -> str:
        rows = [
            f'<tr><td class="k">{html.escape(str(label))}</td>'
            f'<td class="v">{html.escape(str(value))}</td></tr>'
            for label, value in fields
            if value not in (None, "")
        ]
        parts = [INFRA_DETAIL_HTML_CSS]
        if rows:
            parts.append(f'<table class="f">{"".join(rows)}</table>')
        if raw:
            parts.append('<div class="sep">&#8212; Raw JSON &#8212;</div>')
            parts.append(f'<pre class="j">{html.escape(cls._pretty(raw))}</pre>')
        return "".join(parts)


def build_detail_fields(
    table: QTableWidget, row: int, columns: list[str], raw_col: int = 0
) -> tuple[list[tuple[str, str]], str]:
    """
    Read one visual row straight off the table.

    Reading the widget rather than indexing into the source list keeps the
    mapping correct no matter how the user has sorted the table.
    """
    fields: list[tuple[str, str]] = []
    for col, label in enumerate(columns):
        item = table.item(row, col)
        if item is not None and item.text():
            fields.append((label, item.text()))
    raw_item = table.item(row, raw_col)
    raw = str(raw_item.data(Qt.ItemDataRole.UserRole) or "") if raw_item is not None else ""
    return fields, raw
