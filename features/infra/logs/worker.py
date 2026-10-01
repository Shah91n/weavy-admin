"""
Background worker that fetches ``kubectl logs`` from Weaviate pods in the
discovered namespace and emits parsed log entries to the UI.

All kubectl calls and parsing live in ``core/infra/logs/`` — this worker only
runs them off the UI thread and reports progress.
"""

import logging

from PyQt6.QtCore import pyqtSignal

from core.infra.logs import fetch_namespace_logs
from shared.base_worker import BaseWorker

logger = logging.getLogger(__name__)


class LogWorker(BaseWorker):
    """
    Fetch and parse Kubernetes logs for every Weaviate pod in a namespace.

    Signals
    -------
    logs_ready(list[dict])
        Emitted when all pods have been fetched and parsed.
    progress(str), error(str)
        Inherited from :class:`BaseWorker`.
    """

    logs_ready = pyqtSignal(list)  # list[dict]

    def __init__(
        self,
        namespace: str,
        *,
        tail: int,
        since: str | None = None,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._namespace = namespace
        self._tail = tail
        self._since = since

    def run(self) -> None:
        try:
            entries = fetch_namespace_logs(
                self._namespace,
                tail=self._tail,
                since=self._since,
                on_pod=lambda pod: self.progress.emit(f"Fetching logs from {pod} …"),
                should_stop=lambda: self._cancelled,
            )
            self.logs_ready.emit(entries)
        except Exception as exc:
            logger.exception("LogWorker encountered an error")
            self.error.emit(str(exc))
