"""
core.infra.pods
===============
Pure kubectl wrappers for Kubernetes pods — manifests, events, live usage and
the cross-pod comparison. No Qt imports.

These helpers are invoked from background QThread workers in
``features/infra/pods/`` and ``features/infra/cluster_profiling/`` — never from
the UI thread directly.
"""

from core.infra.pods.comparison import (
    USAGE_ERROR_PCT,
    USAGE_WARN_PCT,
    build_pod_comparison,
    fmt_bytes,
    fmt_cpu,
)
from core.infra.pods.manifests import fetch_pod_detail, fetch_pods

__all__ = [
    "USAGE_ERROR_PCT",
    "USAGE_WARN_PCT",
    "build_pod_comparison",
    "fetch_pod_detail",
    "fetch_pods",
    "fmt_bytes",
    "fmt_cpu",
]
