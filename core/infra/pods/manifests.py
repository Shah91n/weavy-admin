"""
core.infra.pods.manifests
=========================
Raw pod manifests and events via ``kubectl get … -o json``.

All functions are synchronous, Qt-free, and raise ``RuntimeError`` on kubectl
failure / timeout / missing binary / unparseable JSON.
"""

from __future__ import annotations

import json
import logging
import subprocess

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30
_MAX_EVENTS = 20


def run_kubectl_json(cmd: list[str], *, what: str) -> dict:
    """Run a ``kubectl … -o json`` command and return the parsed object.

    *what* names the resource in error messages ("pods", "pod manifest", …).
    """
    logger.debug("kubectl (%s): %s", what, " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_S)
    except FileNotFoundError as err:
        raise RuntimeError(
            "kubectl not found. Make sure kubectl is installed and on your PATH."
        ) from err
    except subprocess.TimeoutExpired as err:
        raise RuntimeError(f"Timed out fetching {what} (>{_TIMEOUT_S} s).") from err

    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"kubectl error (exit {result.returncode}): {err}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse {what} JSON: {exc}") from exc


def fetch_pods(namespace: str) -> list[dict]:
    """Return every pod manifest in *namespace* (``kubectl get pods -o json`` items)."""
    data = run_kubectl_json(["kubectl", "get", "pods", "-n", namespace, "-o", "json"], what="pods")
    return data.get("items", [])


def fetch_pod_detail(namespace: str, pod_name: str) -> tuple[dict, list[dict]]:
    """Return ``(pod_manifest, events)`` for one pod.

    Events are the newest ``_MAX_EVENTS`` sorted by ``lastTimestamp``. They are
    informational, so an event fetch failure yields an empty list instead of
    failing the whole call.
    """
    pod = run_kubectl_json(
        ["kubectl", "get", "pod", pod_name, "-n", namespace, "-o", "json"],
        what="pod manifest",
    )

    events: list[dict] = []
    try:
        ev_data = run_kubectl_json(
            [
                "kubectl",
                "get",
                "events",
                f"--field-selector=involvedObject.name={pod_name}",
                "-n",
                namespace,
                "-o",
                "json",
                "--sort-by=.lastTimestamp",
            ],
            what="pod events",
        )
        events = ev_data.get("items", [])[-_MAX_EVENTS:]
    except RuntimeError:
        logger.warning("Could not fetch events for pod '%s'", pod_name, exc_info=True)

    return pod, events
