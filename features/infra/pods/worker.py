"""
Background workers for Kubernetes pod operations.

PodListWorker   – lists all pods in the namespace, optionally followed by the
                  cross-pod resource comparison.
PodDetailWorker – fetches the full manifest + events for a single pod.

All kubectl calls live in ``core/infra/pods/`` — these workers only run them off
the UI thread. The bridge (BridgeCoordinator) must already have configured
kubectl credentials.
"""

import logging

from PyQt6.QtCore import pyqtSignal

from core.infra.pods import build_pod_comparison, fetch_pod_detail, fetch_pods
from shared.base_worker import BaseWorker

logger = logging.getLogger(__name__)


class PodListWorker(BaseWorker):
    """
    List all pods in a namespace.

    With ``with_comparison=True`` the pod list is emitted first (so the table
    fills immediately) and the slower comparison — ``kubectl top`` and node
    lookups — follows as the terminal signal.

    Signals
    -------
    pods_ready(list)
        Pod manifest dicts.
    comparison_ready(dict)
        ``build_pod_comparison`` result. Only emitted with ``with_comparison``.
    progress(str), error(str)
        Inherited from :class:`BaseWorker`.
    """

    pods_ready = pyqtSignal(list)
    comparison_ready = pyqtSignal(dict)

    def __init__(
        self,
        namespace: str,
        *,
        with_comparison: bool = False,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._namespace = namespace
        self._with_comparison = with_comparison

    def run(self) -> None:
        try:
            self.progress.emit(f"Listing pods in namespace '{self._namespace}'…")
            pods = fetch_pods(self._namespace)
            self.pods_ready.emit(pods)
            if not self._with_comparison or self._cancelled:
                return
            self.progress.emit("Comparing pods — fetching live usage…")
            self.comparison_ready.emit(build_pod_comparison(self._namespace, pods))
        except Exception as exc:
            logger.exception("PodListWorker error")
            self.error.emit(str(exc))


class PodDetailWorker(BaseWorker):
    """
    Fetch the full manifest and recent events for a single pod.

    Signals
    -------
    pod_ready(dict, list)
        ``(pod_manifest, events)``.
    progress(str), error(str)
        Inherited from :class:`BaseWorker`.
    """

    pod_ready = pyqtSignal(dict, list)

    def __init__(self, namespace: str, pod_name: str, parent: object | None = None) -> None:
        super().__init__(parent)
        self._namespace = namespace
        self._pod_name = pod_name

    def run(self) -> None:
        try:
            self.progress.emit(f"Fetching pod '{self._pod_name}' in namespace '{self._namespace}'…")
            pod, events = fetch_pod_detail(self._namespace, self._pod_name)
            self.pod_ready.emit(pod, events)
        except Exception as exc:
            logger.exception("PodDetailWorker error")
            self.error.emit(str(exc))
