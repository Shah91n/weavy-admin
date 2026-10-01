"""
shared/loading_bar.py
=====================
LoadingBar – thin indeterminate strip that animates while background work runs.

Place it directly under a view's toolbar and call :meth:`set_busy` when a worker
starts and again when it finishes or fails. The work itself stays on the worker
thread; this only tells the user something is running.

Styled through ``QProgressBar#loadingBar`` — green accent in
``shared/styles/global_qss.py``, blue accent override in
``shared/styles/infra_qss.py`` for infra views.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QProgressBar, QWidget


class LoadingBar(QProgressBar):
    """Indeterminate busy indicator, hidden while idle."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("loadingBar")
        self.setRange(0, 0)  # indeterminate — Qt animates the chunk itself
        self.setTextVisible(False)
        # Keep its height reserved when hidden so the content below does not jump.
        policy = self.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.setSizePolicy(policy)
        self.setVisible(False)

    def set_busy(self, busy: bool) -> None:
        self.setVisible(busy)
