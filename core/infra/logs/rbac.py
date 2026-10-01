"""
core.infra.logs.rbac
====================
Reduce parsed pod-log entries to RBAC authorization entries
(``action == "authorize"``) and enrich each with the fields the RBAC views show:

* ``source_ip``  – client IP address
* ``resource``   – first permissions[].resource string
* ``result``     – first permissions[].results value (``"success"`` / ``"denied"``)
"""

from __future__ import annotations

import json


def _enrich(entry: dict) -> dict:
    """Add RBAC fields from the entry's raw JSON payload (empty strings if absent)."""
    entry["source_ip"] = ""
    entry["resource"] = ""
    entry["result"] = ""
    try:
        data = json.loads(entry.get("raw", ""))
    except (json.JSONDecodeError, TypeError):
        return entry
    if not isinstance(data, dict):
        return entry

    entry["source_ip"] = data.get("source_ip", "")
    perms = data.get("permissions", [])
    if perms:
        entry["resource"] = perms[0].get("resource", "")
        entry["result"] = perms[0].get("results", "")
    return entry


def filter_rbac_entries(entries: list[dict]) -> list[dict]:
    """Return only the ``authorize`` entries, each enriched with RBAC fields."""
    return [_enrich(e) for e in entries if e.get("action", "").lower() == "authorize"]
