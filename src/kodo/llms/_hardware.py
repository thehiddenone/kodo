"""Best-effort GPU VRAM / Apple Silicon unified-memory autodetection.

Used only to inform local-model hardware recommendations surfaced to
kodo-vsix over ``hello.ack`` (see :func:`kodo.llms.detect_vram_gb` and
``doc/LLM_REGISTRY.md`` §4.3) — detection failures are never fatal and never
propagate, since this must not block the WebSocket handshake.

AMD GPU detection is intentionally not implemented (out of scope for now):
an AMD-only machine on Linux/Windows will report ``None`` even with a
discrete GPU present.
"""

from __future__ import annotations

import logging
import sys

__all__ = ["detect_vram_gb"]

_log = logging.getLogger(__name__)

# Canonical GPU/unified-memory tiers, ascending. Raw byte counts from real
# hardware rarely land exactly on a round number (e.g. a "24GB" card reports
# ~23.99 GiB total) — snapping to the nearest tier gives a clean, stable
# figure to display and compare against.
_VRAM_TIERS_GB: tuple[int, ...] = (
    4,
    6,
    8,
    10,
    12,
    16,
    20,
    24,
    32,
    40,
    48,
    64,
    80,
    96,
    128,
    192,
    256,
)


def _snap_to_tier(raw_gb: float) -> int:
    if raw_gb <= 0:
        return 0
    if raw_gb >= _VRAM_TIERS_GB[-1]:
        # Beyond our largest single tier (e.g. multi-GPU rigs) — round to the
        # nearest 32 GB instead of clamping to the top of the list.
        return round(raw_gb / 32) * 32
    return min(_VRAM_TIERS_GB, key=lambda tier: abs(tier - raw_gb))


def _detect_nvidia_vram_bytes() -> int | None:
    """Sum VRAM across every NVIDIA GPU visible to the driver, via pynvml."""
    try:
        import pynvml  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        pynvml.nvmlInit()
    except Exception:  # noqa: BLE001 — no NVIDIA driver present is normal, not an error
        return None
    try:
        count = pynvml.nvmlDeviceGetCount()
        if count <= 0:
            return None
        total_bytes = 0
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            total_bytes += int(pynvml.nvmlDeviceGetMemoryInfo(handle).total)
        return total_bytes
    except Exception:  # noqa: BLE001 — best-effort detection, never crash the caller
        _log.debug("NVML VRAM query failed", exc_info=True)
        return None
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:  # noqa: BLE001 — shutdown failure is harmless here
            pass


def _detect_mac_unified_memory_bytes() -> int | None:
    """Total system RAM on Apple Silicon, treated as VRAM-equivalent since
    the GPU shares the same unified memory pool with the CPU."""
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        return int(psutil.virtual_memory().total)
    except Exception:  # noqa: BLE001 — best-effort detection, never crash the caller
        _log.debug("psutil memory query failed", exc_info=True)
        return None


def detect_vram_gb() -> int | None:
    """Best-effort total GPU VRAM, normalized to the nearest tier in
    :data:`_VRAM_TIERS_GB`.

    On macOS this reports total unified memory (Apple Silicon shares memory
    between CPU and GPU, so there is no separate VRAM figure to query). On
    other platforms it sums VRAM across every NVIDIA GPU visible to the
    driver via ``pynvml``. Returns ``None`` if nothing could be detected —
    no supported GPU, missing driver, or missing/uninstalled library — which
    is expected on plenty of machines and is not itself an error.
    """
    if sys.platform == "darwin":
        raw_bytes = _detect_mac_unified_memory_bytes()
    else:
        raw_bytes = _detect_nvidia_vram_bytes()
    if raw_bytes is None:
        return None
    return _snap_to_tier(raw_bytes / (1024**3))
