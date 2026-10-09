"""
core/infra/profiling/profiling_bridge.py
=====================================

Low-level bridge for capturing Go pprof profiles from Weaviate pods.

Responsibilities
----------------
- Start / stop ``kubectl port-forward`` for a pod on port 6060.
- Curl pprof endpoints and save ``.pb.gz`` files.
- Fetch the goroutine text dump (``?debug=2``).
- Run cpu and fgprof one after the other (each blocks for ``duration`` seconds).
- Generate SVG flame-graphs via ``go tool pprof -svg``.

All CLI calls use ``subprocess.run`` / ``subprocess.Popen`` — no Python SDK
wrappers.
"""

import contextlib
import gzip
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Local port used for all port-forwards
_PF_PORT = 6060
_BASE_URL = f"http://localhost:{_PF_PORT}"

# Per-endpoint timeout (seconds).  CPU / fgprof run for up to duration + 30 s.
_CURL_OVERHEAD = 30
# Instant profiles use the caller's download timeout, or this when none is given.
_QUICK_TIMEOUT = 15

# Endpoints keyed by profile name
_ENDPOINTS: dict[str, str] = {
    "cpu": "/debug/pprof/profile?seconds={duration}",
    "fgprof": "/debug/fgprof?seconds={duration}&ignore=gopark&ignore=GoWrapper",
    "heap": "/debug/pprof/heap",
    "allocs": "/debug/pprof/allocs",
    "mutex": "/debug/pprof/mutex",
    "goroutine": "/debug/pprof/goroutine",
}

# Profiles that block for `duration` seconds
_TIMED_PROFILES = {"cpu", "fgprof"}


class ProfilingBridge:
    """Static helpers for pprof capture operations."""

    # ------------------------------------------------------------------
    # Port-forward lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def port_forward(pod_name: str, namespace: str) -> subprocess.Popen:
        """
        Start ``kubectl port-forward pod/<pod_name> 6060:6060 -n <namespace>``
        in the background.

        Returns the background process.  Caller must call
        :meth:`stop_port_forward` when done.

        Raises
        ------
        RuntimeError
            If kubectl is not found or port-forward fails to start.
        """
        cmd = [
            "kubectl",
            "port-forward",
            f"pod/{pod_name}",
            f"{_PF_PORT}:{_PF_PORT}",
            "-n",
            namespace,
        ]
        logger.debug("Starting port-forward: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as err:
            raise RuntimeError(
                "kubectl not found. Make sure kubectl is installed and on your PATH."
            ) from err
        # Give the tunnel a moment to establish
        time.sleep(3)
        return proc

    @staticmethod
    def stop_port_forward(process: subprocess.Popen) -> None:
        """Kill the port-forward process and wait for it to exit."""
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                process.kill()

    # ------------------------------------------------------------------
    # Profile capture
    # ------------------------------------------------------------------

    @staticmethod
    def capture_profile(
        profile_type: str,
        duration: int,
        output_path: str,
        download_timeout: int | None = None,
    ) -> bool:
        """
        Curl a pprof endpoint and save the ``.pb.gz`` binary to *output_path*.

        Parameters
        ----------
        profile_type:
            One of ``"cpu"``, ``"heap"``, ``"allocs"``, ``"goroutine"``,
            ``"mutex"``, ``"fgprof"``.
        duration:
            Sampling duration in seconds (used for CPU and fgprof only).
        output_path:
            Absolute path for the output ``.pb.gz`` file.
        download_timeout:
            Max seconds to download an instant profile (heap, allocs, mutex,
            goroutine).  ``None`` uses the default (15 s).  Large pods can need
            minutes for heap / allocs.

        Returns
        -------
        bool
            ``True`` on success, ``False`` on failure.
        """
        endpoint_tmpl = _ENDPOINTS.get(profile_type)
        if endpoint_tmpl is None:
            logger.error("Unknown profile type: %s", profile_type)
            return False

        endpoint = endpoint_tmpl.format(duration=duration)
        url = _BASE_URL + endpoint

        if profile_type in _TIMED_PROFILES:
            timeout = duration + _CURL_OVERHEAD
        else:
            timeout = download_timeout or _QUICK_TIMEOUT

        cmd = ["curl", "-s", "--max-time", str(timeout), "-o", output_path, url]
        logger.debug("Capturing %s profile: %s", profile_type, " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if result.returncode != 0:
                logger.warning(
                    "curl failed for %s (exit %d): %s",
                    profile_type,
                    result.returncode,
                    result.stderr.decode(errors="replace"),
                )
                _remove_partial(output_path)
                return False
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                logger.warning("Output empty for %s: %s", profile_type, output_path)
                return False
            if not _is_complete_gzip(output_path):
                logger.warning("Truncated %s profile: %s", profile_type, output_path)
                _remove_partial(output_path)
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.warning("Timeout capturing %s profile", profile_type)
            _remove_partial(output_path)
            return False
        except FileNotFoundError:
            logger.error("curl not found. Install curl and ensure it is on PATH.")
            return False
        except Exception as exc:
            logger.exception("Error capturing %s profile: %s", profile_type, exc)
            return False

    @staticmethod
    def capture_goroutine_text_dump(output_path: str, download_timeout: int | None = None) -> bool:
        """
        Fetch ``/debug/pprof/goroutine?debug=2`` and save the full text dump.

        Returns ``True`` on success.
        """
        url = f"{_BASE_URL}/debug/pprof/goroutine?debug=2"
        timeout = download_timeout or _QUICK_TIMEOUT
        cmd = ["curl", "-s", "--max-time", str(timeout), "-o", output_path, url]
        logger.debug("Capturing goroutine text dump")
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if result.returncode != 0:
                logger.warning(
                    "curl failed for goroutine text dump (exit %d): %s",
                    result.returncode,
                    result.stderr.decode(errors="replace"),
                )
                return False
            return os.path.exists(output_path) and os.path.getsize(output_path) > 0
        except Exception as exc:
            logger.exception("Error capturing goroutine text dump: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Sequential timed capture (cpu, then fgprof)
    # ------------------------------------------------------------------

    @staticmethod
    def capture_timed_profiles_sequential(
        duration: int,
        output_dir: str,
        profiles: list[str],
    ) -> dict[str, tuple[bool, str]]:
        """
        Capture ``cpu`` and ``fgprof`` one after the other — never at the same
        time.

        When both profilers run concurrently each one shows up in the other's
        profile, which makes them harder to read.  ``cpu`` is captured first,
        then ``fgprof``, so each covers its own clean window.  Only the ones in
        *profiles* run, so the method blocks for ``duration`` seconds per
        selected profile.

        Parameters
        ----------
        duration:
            Sampling duration in seconds, per profile.
        output_dir:
            Directory where ``.pb.gz`` files are written.
        profiles:
            Selected profiles; any of ``"cpu"`` / ``"fgprof"`` in it are run.

        Returns
        -------
        dict
            ``{profile_name: (success, file_path)}`` for each profile run.
        """
        results: dict[str, tuple[bool, str]] = {}

        for name in [p for p in ("cpu", "fgprof") if p in profiles]:
            output_path = os.path.join(output_dir, f"{name}.pb.gz")
            logger.debug("Sequential capture start: %s (%ds)", name, duration)
            ok = ProfilingBridge.capture_profile(name, duration, output_path)
            results[name] = (ok, output_path)

        return results

    @staticmethod
    def generate_svg(pb_gz_path: str, svg_path: str) -> tuple[bool, str]:
        """
        Generate an SVG flame-graph via ``go tool pprof -svg <pb_gz_path>``.

        Requires ``go`` and ``graphviz`` to be installed.  Searches the
        process PATH and common Go install locations.

        Returns
        -------
        (success, warning)
            ``success`` – True if the SVG was written successfully.
            ``warning`` – Non-empty user-visible string when ``go`` is
            missing; empty string on success or on execution error.
        """
        go_bin = _find_binary("go")
        if not go_bin:
            return False, (
                "⚠️  go not found — SVG generation skipped.\n"
                "    Install: brew install go  (or https://go.dev/dl/)"
            )
        cmd = [go_bin, "tool", "pprof", "-svg", pb_gz_path]
        logger.debug("Generating SVG: %s -> %s", pb_gz_path, svg_path)
        try:
            with open(svg_path, "w", encoding="utf-8") as fout:
                result = subprocess.run(
                    cmd,
                    stdout=fout,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                )
            if result.returncode != 0 or os.path.getsize(svg_path) == 0:
                if os.path.exists(svg_path):
                    os.remove(svg_path)
                return False, ""
            return True, ""
        except Exception as exc:
            logger.warning("SVG generation failed for %s: %s", pb_gz_path, exc)
            return False, ""


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def _is_complete_gzip(path: str) -> bool:
    """True when *path* decompresses to the end — i.e. the download was not cut off.

    Streams in chunks so multi-GB heap profiles never sit in memory at once.
    """
    try:
        with gzip.open(path, "rb") as fh:
            while fh.read(1 << 20):
                pass
        return True
    except (OSError, EOFError):  # BadGzipFile is an OSError
        return False


def _remove_partial(path: str) -> None:
    """Delete a half-written profile so it is never mistaken for a good one."""
    with contextlib.suppress(OSError):
        os.remove(path)


def _find_binary(name: str) -> str | None:
    """
    Locate *name* on the system, checking both the process PATH and the
    common locations where ``go install`` drops binaries.

    Returns the full path to the binary, or ``None`` if not found.
    """
    # 1. Fast path – already on the process PATH
    found = shutil.which(name)
    if found:
        return found

    # 2. Common Go binary locations (checked in priority order)
    candidates = [
        Path.home() / "go" / "bin" / name,  # default GOPATH
        Path(os.environ.get("GOPATH", "")) / "bin" / name,  # explicit GOPATH
        Path("/usr/local/go/bin") / name,  # system Go install
        Path("/opt/homebrew/bin") / name,  # Apple Silicon brew
        Path("/usr/local/bin") / name,  # Intel brew / manual
    ]

    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            logger.debug("Found %s at %s (not on PATH)", name, candidate)
            return str(candidate)

    return None
