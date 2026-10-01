"""
core.infra.pods.comparison
==========================
Side-by-side comparison of the Weaviate pods in a namespace — the numbers that
should be roughly equal across nodes, so a skewed one stands out.

Per pod: node, zone, instance type, Weaviate version, CPU / memory used against
the container limit, how loaded the hosting node is, and restarts with the last
termination reason.

Sources
-------
* Pod manifests (already fetched for the pod list) — limits, image, restarts.
* ``kubectl top pods --containers`` — live usage. Needs metrics-server.
* ``kubectl get nodes <names> -o json`` — instance type, zone. Cluster-scoped.
* ``kubectl top nodes`` — node load. Cluster-scoped, needs metrics-server.

Every source except the manifests is optional: when one is refused or missing,
its columns stay empty and a note says why, rather than failing the comparison.
"""

from __future__ import annotations

import logging
import subprocess
from collections import Counter

from core.infra.pods.manifests import run_kubectl_json

logger = logging.getLogger(__name__)

_TOP_TIMEOUT_S = 30
_MAIN_CONTAINER = "weaviate"

# Usage thresholds, as % of the container limit / node allocatable.
USAGE_WARN_PCT = 80.0
USAGE_ERROR_PCT = 90.0

# Spread between the busiest and quietest pod that is worth flagging.
_MEM_SPREAD_RATIO = 1.5
_CPU_SPREAD_RATIO = 2.0
_CPU_SPREAD_FLOOR_M = 200  # ignore CPU spread while every pod is near idle

# Fields that should be identical on every pod; a minority value is flagged.
_UNIFORM_FIELDS = ("instance_type", "version", "cpu_limit_m", "mem_limit_b")

_MEM_SUFFIX: dict[str, int] = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}
_CPU_SUFFIX: dict[str, float] = {"n": 1e-6, "u": 1e-3, "m": 1.0}


# ---------------------------------------------------------------------------
# Quantity parsing / formatting
# ---------------------------------------------------------------------------


def parse_cpu_millicores(value: object) -> int | None:
    """``"250m"`` → 250, ``"2"`` → 2000, ``"1500000n"`` → 2. None if unparseable."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text[-1] in _CPU_SUFFIX:
            return round(float(text[:-1]) * _CPU_SUFFIX[text[-1]])
        return round(float(text) * 1000)
    except ValueError:
        return None


def parse_mem_bytes(value: object) -> int | None:
    """``"16Gi"`` → 17179869184, ``"512M"`` → 512000000. None if unparseable."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        for suffix in sorted(_MEM_SUFFIX, key=len, reverse=True):
            if text.endswith(suffix):
                return round(float(text[: -len(suffix)]) * _MEM_SUFFIX[suffix])
        return round(float(text))  # plain bytes, or exponent form like "129e6"
    except ValueError:
        return None


def fmt_cpu(millicores: int | None) -> str:
    if millicores is None:
        return "—"
    if millicores >= 1000:
        return f"{millicores / 1000:.2f}".rstrip("0").rstrip(".") + " cores"
    return f"{millicores}m"


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _pct(used: int | None, total: int | None) -> float | None:
    if used is None or not total:
        return None
    return used / total * 100


# ---------------------------------------------------------------------------
# Optional sources
# ---------------------------------------------------------------------------


def _run_top(cmd: list[str]) -> list[list[str]]:
    """Run a ``kubectl top … --no-headers`` command; return whitespace-split rows.

    Raises RuntimeError with kubectl's message on failure.
    """
    logger.debug("kubectl top: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_TOP_TIMEOUT_S)
    except FileNotFoundError as err:
        raise RuntimeError("kubectl not found") from err
    except subprocess.TimeoutExpired as err:
        raise RuntimeError(f"timed out (>{_TOP_TIMEOUT_S} s)") from err
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "kubectl top failed")
    return [line.split() for line in result.stdout.splitlines() if line.strip()]


def fetch_container_usage(namespace: str) -> dict[tuple[str, str], tuple[int | None, int | None]]:
    """``{(pod, container): (cpu_millicores, mem_bytes)}`` from ``kubectl top pods``."""
    rows = _run_top(["kubectl", "top", "pods", "-n", namespace, "--containers", "--no-headers"])
    usage: dict[tuple[str, str], tuple[int | None, int | None]] = {}
    for cols in rows:
        if len(cols) >= 4:  # POD  NAME  CPU(cores)  MEMORY(bytes)
            usage[(cols[0], cols[1])] = (parse_cpu_millicores(cols[2]), parse_mem_bytes(cols[3]))
    return usage


def fetch_node_info(node_names: list[str]) -> dict[str, dict]:
    """``{node: {"zone", "instance_type", "alloc_cpu_m", "alloc_mem_b"}}``."""
    if not node_names:
        return {}
    data = run_kubectl_json(["kubectl", "get", "nodes", *node_names, "-o", "json"], what="nodes")
    # One name returns the Node itself; several return a List.
    items = data.get("items", []) if data.get("kind") == "List" else [data]
    info: dict[str, dict] = {}
    for node in items:
        meta = node.get("metadata", {}) or {}
        labels = meta.get("labels", {}) or {}
        alloc = (node.get("status", {}) or {}).get("allocatable", {}) or {}
        info[meta.get("name", "")] = {
            "zone": labels.get("topology.kubernetes.io/zone")
            or labels.get("failure-domain.beta.kubernetes.io/zone", ""),
            "instance_type": labels.get("node.kubernetes.io/instance-type")
            or labels.get("beta.kubernetes.io/instance-type", ""),
            "alloc_cpu_m": parse_cpu_millicores(alloc.get("cpu")),
            "alloc_mem_b": parse_mem_bytes(alloc.get("memory")),
        }
    return info


def fetch_node_usage() -> dict[str, tuple[int | None, int | None]]:
    """``{node: (cpu_millicores, mem_bytes)}`` from ``kubectl top nodes``."""
    usage: dict[str, tuple[int | None, int | None]] = {}
    for cols in _run_top(["kubectl", "top", "nodes", "--no-headers"]):
        if len(cols) >= 4:  # NAME  CPU(cores)  CPU%  MEMORY(bytes)  MEMORY%
            usage[cols[0]] = (parse_cpu_millicores(cols[1]), parse_mem_bytes(cols[3]))
    return usage


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _main_container(pod: dict) -> tuple[dict, dict]:
    """Return ``(container_spec, container_status)`` for the Weaviate container."""
    containers = (pod.get("spec", {}) or {}).get("containers", []) or []
    statuses = (pod.get("status", {}) or {}).get("containerStatuses", []) or []
    spec = next((c for c in containers if c.get("name") == _MAIN_CONTAINER), None)
    if spec is None:
        spec = containers[0] if containers else {}
    status = next((s for s in statuses if s.get("name") == spec.get("name")), {})
    return spec, status


def _image_version(image: str) -> str:
    """``semitechnologies/weaviate:1.28.2@sha256:…`` → ``1.28.2``."""
    name = image.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[1] if ":" in last else (last or "—")


def _majority(values: list[object]) -> object | None:
    """Most common non-None value, or None when there is nothing to compare."""
    present = [v for v in values if v is not None and v != ""]
    if len(present) < 2:
        return None
    return Counter(present).most_common(1)[0][0]


def build_pod_comparison(namespace: str, pods: list[dict]) -> dict:
    """Compare the Weaviate pods in *namespace*.

    Returns ``{"rows": list[dict], "insights": list[(level, text)], "notes": list[str]}``.
    ``level`` is ``"ok"`` / ``"warn"`` / ``"error"``. Each row carries raw numbers
    plus ``"differs": {field: majority_value}`` for fields that deviate from the
    other pods.
    """
    weaviate_pods = [p for p in pods if _main_container(p)[0].get("name") == _MAIN_CONTAINER]
    pods = weaviate_pods or pods
    notes: list[str] = []

    try:
        usage = fetch_container_usage(namespace)
    except RuntimeError as exc:
        usage = {}
        notes.append(f"Live CPU / memory unavailable (kubectl top pods: {exc}).")

    node_names = sorted({(p.get("spec", {}) or {}).get("nodeName", "") for p in pods} - {""})
    try:
        nodes = fetch_node_info(node_names)
    except RuntimeError as exc:
        nodes = {}
        notes.append(f"Node zone / instance type unavailable (kubectl get nodes: {exc}).")

    try:
        node_usage = fetch_node_usage()
    except RuntimeError as exc:
        node_usage = {}
        notes.append(f"Node load unavailable (kubectl top nodes: {exc}).")

    rows: list[dict] = []
    for pod in pods:
        name = (pod.get("metadata", {}) or {}).get("name", "?")
        node = (pod.get("spec", {}) or {}).get("nodeName", "")
        spec, status = _main_container(pod)
        limits = (spec.get("resources", {}) or {}).get("limits", {}) or {}
        cpu_used, mem_used = usage.get((name, spec.get("name", "")), (None, None))
        cpu_limit = parse_cpu_millicores(limits.get("cpu"))
        mem_limit = parse_mem_bytes(limits.get("memory"))
        node_info = nodes.get(node, {})
        node_cpu, node_mem = node_usage.get(node, (None, None))
        last_term = (status.get("lastState", {}) or {}).get("terminated", {}) or {}

        rows.append(
            {
                "pod": name,
                "zone": node_info.get("zone") or "—",
                "instance_type": node_info.get("instance_type") or None,
                "version": _image_version(spec.get("image", "")),
                "cpu_used_m": cpu_used,
                "cpu_limit_m": cpu_limit,
                "cpu_pct": _pct(cpu_used, cpu_limit),
                "mem_used_b": mem_used,
                "mem_limit_b": mem_limit,
                "mem_pct": _pct(mem_used, mem_limit),
                "node_cpu_pct": _pct(node_cpu, node_info.get("alloc_cpu_m")),
                "node_mem_pct": _pct(node_mem, node_info.get("alloc_mem_b")),
                "restarts": int(status.get("restartCount", 0) or 0),
                "last_reason": last_term.get("reason", ""),
                "differs": {},
            }
        )

    for field in _UNIFORM_FIELDS:
        common = _majority([r[field] for r in rows])
        if common is None:
            continue
        for r in rows:
            if r[field] is not None and r[field] != common:
                r["differs"][field] = common

    rows.sort(key=lambda r: _natural_key(r["pod"]))
    return {"rows": rows, "insights": _insights(rows), "notes": notes}


def _natural_key(name: str) -> tuple[str, int]:
    """Sort ``weaviate-10`` after ``weaviate-9``."""
    head, _, tail = name.rpartition("-")
    return (head, int(tail)) if tail.isdigit() else (name, -1)


def _insights(rows: list[dict]) -> list[tuple[str, str]]:
    """Plain-language findings, worst first."""
    found: list[tuple[str, str]] = []

    for r in rows:
        if r["last_reason"] == "OOMKilled":
            found.append(
                (
                    "error",
                    f"{r['pod']} was last restarted because it ran out of memory (OOMKilled).",
                )
            )
        for key, label in (("mem_pct", "memory"), ("cpu_pct", "CPU")):
            pct = r[key]
            if pct is not None and pct >= USAGE_WARN_PCT:
                level = "error" if pct >= USAGE_ERROR_PCT else "warn"
                found.append((level, f"{r['pod']} is using {pct:.0f}% of its {label} limit."))

    mem = [(r["mem_used_b"], r["pod"]) for r in rows if r["mem_used_b"]]
    if len(mem) >= 2:
        (lo, lo_pod), (hi, hi_pod) = min(mem), max(mem)
        if hi / lo >= _MEM_SPREAD_RATIO:
            found.append(
                (
                    "warn",
                    f"Memory is uneven: {hi_pod} uses {fmt_bytes(hi)} vs {lo_pod} "
                    f"{fmt_bytes(lo)} ({hi / lo:.1f}×) — check shard / tenant balance.",
                )
            )

    cpu = [(r["cpu_used_m"], r["pod"]) for r in rows if r["cpu_used_m"]]
    if len(cpu) >= 2:
        (lo, lo_pod), (hi, hi_pod) = min(cpu), max(cpu)
        if hi >= _CPU_SPREAD_FLOOR_M and hi / lo >= _CPU_SPREAD_RATIO:
            found.append(
                (
                    "warn",
                    f"CPU is uneven: {hi_pod} uses {fmt_cpu(hi)} vs {lo_pod} "
                    f"{fmt_cpu(lo)} ({hi / lo:.1f}×) — one node may be taking more queries.",
                )
            )

    for field, label in (
        ("version", "Weaviate versions"),
        ("instance_type", "instance types"),
        ("cpu_limit_m", "CPU limits"),
        ("mem_limit_b", "memory limits"),
    ):
        odd = [r["pod"] for r in rows if field in r["differs"]]
        if odd:
            verb = "differs" if len(odd) == 1 else "differ"
            found.append(("warn", f"Mixed {label}: {', '.join(odd)} {verb} from the other pods."))

    if not found and rows:
        found.append(("ok", "No significant differences between pods."))

    order = {"error": 0, "warn": 1, "ok": 2}
    return sorted(found, key=lambda item: order[item[0]])
