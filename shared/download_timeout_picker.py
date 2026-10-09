"""
shared/download_timeout_picker.py
=================================
DownloadTimeoutPicker – the "Download timeout" radio row shared by the single-pod
Profiling view and Cluster Profiling.

The timeout bounds how long an *instant* profile (heap, allocs, mutex,
goroutine) may take to download. It is a ceiling only — curl stops as soon as
the download finishes — so the longest option is the safe default. Large pods
can produce heap / allocs profiles of several GB that need minutes.

The choice is remembered in QSettings under one key, so both views share it.
"""

from __future__ import annotations

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QRadioButton, QWidget

_SETTINGS_KEY = "profiling/download_timeout"
_OPTIONS: list[tuple[str, int]] = [("60s", 60), ("300s", 300), ("600s", 600)]
_DEFAULT_SECS = 600
_TOOLTIP = (
    "Max wait while downloading heap, allocs, mutex and goroutine.\n"
    "A ceiling only: each download stops as soon as it finishes.\n"
    "On large pods heap / allocs can be GBs and need minutes."
)


def _saved_secs(settings: QSettings) -> int:
    """The remembered timeout, or the default when missing or not a number."""
    try:
        return int(settings.value(_SETTINGS_KEY, _DEFAULT_SECS))
    except (TypeError, ValueError):  # e.g. an older build saved "Default"
        return _DEFAULT_SECS


class DownloadTimeoutPicker(QWidget):
    """Label + one radio button per timeout option, preselected from QSettings."""

    def __init__(self, label_width: int | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = QSettings()

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)

        label = QLabel("Download timeout:")
        label.setObjectName("infraStatusLabel")
        label.setToolTip(_TOOLTIP)
        if label_width is not None:
            label.setFixedWidth(label_width)  # align with sibling setting rows
        row.addWidget(label)

        self._group = QButtonGroup(self)
        saved = _saved_secs(self._settings)
        for text, secs in _OPTIONS:
            button = QRadioButton(text)
            button.setProperty("timeout_secs", secs)
            button.setToolTip(_TOOLTIP)
            button.setChecked(secs == saved)
            self._group.addButton(button)
            row.addWidget(button)
        if self._group.checkedButton() is None:  # saved value is not an option
            self._group.buttons()[-1].setChecked(True)
        row.addStretch()

    def value(self) -> int:
        """Selected timeout in seconds."""
        button = self._group.checkedButton()
        return int(button.property("timeout_secs")) if button else _DEFAULT_SECS

    def save(self) -> int:
        """Remember the selected timeout for next time and return it."""
        secs = self.value()
        self._settings.setValue(_SETTINGS_KEY, secs)
        return secs
