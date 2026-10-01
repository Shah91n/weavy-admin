"""
core.infra.logs
===============
Pure subprocess wrappers and parsers for Weaviate pod logs via ``kubectl``.
No Qt imports.

These helpers are invoked from background QThread workers in
``features/infra/logs/``, ``features/infra/rbac_log/`` and
``features/infra/rbac_analysis/`` — never from the UI thread directly.
"""

from core.infra.logs.pod_logs import fetch_namespace_logs
from core.infra.logs.rbac import filter_rbac_entries

__all__ = [
    "fetch_namespace_logs",
    "filter_rbac_entries",
]
