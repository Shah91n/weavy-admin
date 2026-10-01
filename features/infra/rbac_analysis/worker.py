"""
Background worker that fetches ``kubectl logs`` from Weaviate pods and keeps
only RBAC authorization entries (``action == "authorize"``), enriched with
``source_ip`` / ``resource`` / ``result``.

All kubectl calls, parsing and RBAC filtering live in ``core/infra/logs/`` —
this worker only runs them off the UI thread and reports progress.
"""

import logging

from PyQt6.QtCore import pyqtSignal

from core.infra.logs import fetch_namespace_logs, filter_rbac_entries
from shared.base_worker import BaseWorker

logger = logging.getLogger(__name__)

_TAIL_PER_POD = 5000


class RBACAnalysisWorker(BaseWorker):
    """
    Fetch Kubernetes logs for every Weaviate pod and emit only RBAC entries.

    Signals
    -------
    logs_ready(list[dict])
        RBAC-filtered and enriched log entries.
    progress(str), error(str)
        Inherited from :class:`BaseWorker`.
    """

    logs_ready = pyqtSignal(list)  # list[dict]

    def __init__(self, namespace: str, parent: object | None = None) -> None:
        super().__init__(parent)
        self._namespace = namespace

    def run(self) -> None:
        try:
            entries = fetch_namespace_logs(
                self._namespace,
                tail=_TAIL_PER_POD,
                on_pod=lambda pod: self.progress.emit(f"Fetching RBAC logs from {pod} …"),
                should_stop=lambda: self._cancelled,
            )
            self.logs_ready.emit(filter_rbac_entries(entries))
        except Exception as exc:
            logger.exception("RBACAnalysisWorker encountered an error")
            self.error.emit(str(exc))
