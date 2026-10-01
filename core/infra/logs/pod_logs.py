"""
core.infra.logs.pod_logs
========================
List Weaviate pods and fetch / parse their ``kubectl logs`` output.

Fetch strategy
--------------
1. List pods in the namespace matching a label selector (``app=weaviate``).
2. For each pod, run ``kubectl logs --tail=<tail> [--since=<window>] -n <namespace> <pod>``.
   ``--since`` limits lines to the chosen time window; ``--tail`` stays as a
   per-pod safety cap so a long window on a busy pod cannot flood the table.
3. Parse each line as JSON first; fall back to key-value parsing.

Parsed fields
-------------
timestamp, level, action, user, method, message, raw (+ pod, set by the fetcher)

RBAC special handling
---------------------
When ``action == "authorize"``, the parser also extracts:
- ``request_action``  – the CRUD letter (C/R/U/D) mapped from the raw verb.
- ``user``            – the subject performing the authorised action.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable

logger = logging.getLogger(__name__)

_LIST_TIMEOUT_S = 15
_FETCH_TIMEOUT_S = 60

# ---------------------------------------------------------------------------
# CRUD verb → letter mapping for RBAC "authorize" actions
# ---------------------------------------------------------------------------
_CRUD_MAP: dict[str, str] = {
    # Create
    "C": "C",
    "create": "C",
    "post": "C",
    # Read
    "R": "R",
    "read": "R",
    "get": "R",
    "list": "R",
    # Update
    "U": "U",
    "update": "U",
    "put": "U",
    "patch": "U",
    # Delete
    "D": "D",
    "delete": "D",
}

# Regex for key=value or key="value" log lines
_KV_PAIR = re.compile(r'(\w+)=(?:"([^"]*)"|([\S]*))')


def _normalise_crud(raw: str) -> str:
    """Return C/R/U/D from a raw verb, or the raw string if unknown."""
    return _CRUD_MAP.get(raw.lower(), raw.upper())


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_json_line(line: str) -> dict | None:
    """
    Attempt to parse *line* as a JSON object.
    Returns a normalised dict or None on failure.
    """
    try:
        data = json.loads(line)
        if not isinstance(data, dict):
            return None

        entry = {
            "timestamp": data.get("time") or data.get("timestamp") or data.get("ts") or "",
            "level": (data.get("level") or data.get("severity") or "").upper(),
            "action": data.get("action") or data.get("msg_action") or "",
            "user": data.get("user") or data.get("sub") or "",
            "method": data.get("method") or data.get("http_method") or "",
            "message": data.get("msg") or data.get("message") or "",
            "raw": line,
        }

        # RBAC special case: action="authorize"
        if entry["action"].lower() == "authorize":
            raw_verb = (
                data.get("request_action") or data.get("requestAction") or data.get("verb") or ""
            )
            entry["request_action"] = _normalise_crud(raw_verb) if raw_verb else ""
            # Override user with the authorised subject if present
            entry["user"] = (
                data.get("user") or data.get("subject") or data.get("sub") or entry["user"]
            )

        return entry
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_kv_line(line: str) -> dict | None:
    """
    Attempt to parse a key=value log line.
    Returns a normalised dict or None if no key-value pairs are found.
    """
    pairs = {k: (v1 or v2) for k, v1, v2 in _KV_PAIR.findall(line)}
    if not pairs:
        return None

    entry = {
        "timestamp": pairs.get("time") or pairs.get("timestamp") or pairs.get("ts") or "",
        "level": (pairs.get("level") or pairs.get("severity") or "").upper(),
        "action": pairs.get("action") or pairs.get("act") or "",
        "user": pairs.get("user") or pairs.get("sub") or "",
        "method": pairs.get("method") or pairs.get("http_method") or "",
        "message": pairs.get("msg") or pairs.get("message") or "",
        "raw": line,
    }

    if entry["action"].lower() == "authorize":
        raw_verb = (
            pairs.get("request_action") or pairs.get("requestAction") or pairs.get("verb") or ""
        )
        entry["request_action"] = _normalise_crud(raw_verb) if raw_verb else ""

    return entry


def parse_log_line(line: str) -> dict | None:
    """
    Parse a single log *line* into a dict.
    Tries JSON first, then key-value; returns None for blank lines.
    """
    line = line.strip()
    if not line:
        return None
    entry = _parse_json_line(line)
    if entry is None:
        entry = _parse_kv_line(line)
    if entry is None:
        # Fall back: treat the whole line as a message
        entry = {
            "timestamp": "",
            "level": "UNKNOWN",
            "action": "",
            "user": "",
            "method": "",
            "message": line,
            "raw": line,
        }
    return entry


# ---------------------------------------------------------------------------
# kubectl
# ---------------------------------------------------------------------------


def list_pods(namespace: str, selector: str = "app=weaviate") -> list[str]:
    """Return the names of pods matching *selector* in *namespace*.

    Raises RuntimeError when kubectl is missing or times out.
    """
    cmd = [
        "kubectl",
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        selector,
        "--no-headers",
        "-o",
        "custom-columns=NAME:.metadata.name",
    ]
    logger.debug("Listing pods: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_LIST_TIMEOUT_S)
    except FileNotFoundError as err:
        raise RuntimeError(
            "kubectl not found. Make sure kubectl is installed and on your PATH."
        ) from err
    except subprocess.TimeoutExpired as err:
        raise RuntimeError(f"Timed out listing pods (>{_LIST_TIMEOUT_S} s).") from err

    pods = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    logger.info("Found %d pod(s) in namespace '%s'", len(pods), namespace)
    return pods


def fetch_pod_logs(
    namespace: str,
    pod: str,
    *,
    tail: int,
    since: str | None = None,
    container: str | None = None,
) -> list[dict]:
    """Fetch and parse logs for a single pod.

    Each entry gets a ``pod`` key. A timeout or non-zero exit yields whatever
    was parsed (possibly nothing) and is logged — one bad pod must not fail
    the whole fetch.
    """
    cmd = ["kubectl", "logs", "--tail", str(tail), "-n", namespace, pod]
    if since:
        cmd += ["--since", since]
    if container:
        cmd += ["-c", container]

    logger.debug("Fetching logs: %s", " ".join(cmd))
    entries: list[dict] = []

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_FETCH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        logger.warning("Timed out fetching logs for pod '%s'", pod)
        return entries

    for line in result.stdout.splitlines():
        entry = parse_log_line(line)
        if entry:
            entry["pod"] = pod
            entries.append(entry)
    if result.returncode != 0 and not entries:
        logger.warning("kubectl logs returned non-zero for pod %s: %s", pod, result.stderr.strip())
    return entries


def fetch_namespace_logs(
    namespace: str,
    *,
    tail: int,
    since: str | None = None,
    on_pod: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[dict]:
    """Fetch and parse logs from every Weaviate pod in *namespace*.

    ``on_pod(pod)`` is called before each pod is fetched (progress reporting);
    ``should_stop()`` is checked between pods (cooperative cancellation) — when it
    returns True the entries gathered so far are returned.

    Raises RuntimeError when no pods are found or kubectl is unavailable.
    """
    pods = list_pods(namespace)
    if not pods:
        raise RuntimeError(f"No Weaviate pods found in namespace '{namespace}'.")

    entries: list[dict] = []
    for pod in pods:
        if should_stop and should_stop():
            break
        if on_pod:
            on_pod(pod)
        entries.extend(fetch_pod_logs(namespace, pod, tail=tail, since=since))
    return entries
