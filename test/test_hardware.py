"""Tests for ``kodo.llms._hardware`` -- GPU VRAM / RAM / unified-memory
autodetection.

Covers:
* :func:`_snap_to_tier` snapping raw GB values to the nearest tier.
* :func:`_detect_nvidia_vram_bytes` success / missing-driver / no-GPU.
* :func:`_detect_mac_unified_memory_bytes` success / failure.
* :func:`detect_vram_gb` on darwin vs. other platforms.
* :func:`detect_ram_gb` on darwin vs. other platforms.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from kodo.llms._hardware import (
    _snap_to_tier,
)

# ---------------------------------------------------------------------------
# _snap_to_tier -- pure logic
# ---------------------------------------------------------------------------


def test_snap_to_tier_exact_match() -> None:
    assert _snap_to_tier(8.0) == 8
    assert _snap_to_tier(16.0) == 16
    assert _snap_to_tier(24.0) == 24


def test_snap_to_tier_rounds_to_nearest() -> None:
    # 7.0 GiB: equidistant from 6 and 8; min returns the first (6).
    assert _snap_to_tier(7.0) == 6
    # 9.0 GiB: equidistant from 8 and 10; min returns 8.
    assert _snap_to_tier(9.0) == 8
    # 14.0 GiB: equidistant from 12 and 16; min returns 12.
    assert _snap_to_tier(14.0) == 12


def test_snap_to_tier_below_minimum() -> None:
    assert _snap_to_tier(1.0) == 4  # closest tier is 4
    assert _snap_to_tier(3.0) == 4  # closest tier
    assert _snap_to_tier(5.0) == 4  # equidistant between 4 and 6 -- min picks 4


def test_snap_to_tier_zero_or_negative() -> None:
    assert _snap_to_tier(0.0) == 0
    assert _snap_to_tier(-1.0) == 0


def test_snap_to_tier_at_max_tier() -> None:
    assert _snap_to_tier(256.0) == 256


def test_snap_to_tier_above_max_clamps_to_32gb_multiples() -> None:
    """Above the top tier (256 GB) we round to the nearest 32 GB instead of clamping."""
    # 260 GiB -- 260/32 = 8.125, round = 8, so 256.
    assert _snap_to_tier(260.0) == 256
    # 300 GiB -- 300/32 = 9.375, round = 9, so 288.
    assert _snap_to_tier(300.0) == 288
    # 320 GiB -- 320/32 = 10.0, round = 10, so 320.
    assert _snap_to_tier(320.0) == 320


def test_snap_to_tier_extreme_values() -> None:
    # 2000 GiB -- 2000/32 = 62.5, round (banker's) = 62, so 1984.
    assert _snap_to_tier(2000.0) == 1984


# ---------------------------------------------------------------------------
# _detect_nvidia_vram_bytes -- mocked pynvml via sys.modules
# ---------------------------------------------------------------------------


def _install_pynvml_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Pre-populate sys.modules['pynvml'] with a mock so ``import pynvml`` succeeds."""
    mock = MagicMock()
    monkeypatch.setitem(sys.modules, "pynvml", mock)
    return mock


def test_detect_nvidia_vram_bytes_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mocked pynvml with one GPU returns its total memory."""
    pynvml = _install_pynvml_mock(monkeypatch)
    mock_handle = MagicMock()
    mock_mem = MagicMock()
    mock_mem.total = 16_000_000_000

    pynvml.nvmlInit.return_value = None
    pynvml.nvmlDeviceGetCount.return_value = 1
    pynvml.nvmlDeviceGetHandleByIndex.return_value = mock_handle
    pynvml.nvmlDeviceGetMemoryInfo.return_value = mock_mem
    pynvml.nvmlShutdown.return_value = None

    from kodo.llms._hardware import _detect_nvidia_vram_bytes

    result = _detect_nvidia_vram_bytes()
    assert result == 16_000_000_000


def test_detect_nvidia_vram_bytes_no_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pynvml reporting 0 GPUs returns None."""
    pynvml = _install_pynvml_mock(monkeypatch)
    pynvml.nvmlInit.return_value = None
    pynvml.nvmlDeviceGetCount.return_value = 0
    pynvml.nvmlShutdown.return_value = None

    from kodo.llms._hardware import _detect_nvidia_vram_bytes

    result = _detect_nvidia_vram_bytes()
    assert result is None


def test_detect_nvidia_vram_bytes_driver_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No NVIDIA driver present is not an error -- returns None."""
    pynvml = _install_pynvml_mock(monkeypatch)
    pynvml.nvmlInit.side_effect = RuntimeError("no driver")
    pynvml.nvmlShutdown.return_value = None

    from kodo.llms._hardware import _detect_nvidia_vram_bytes

    result = _detect_nvidia_vram_bytes()
    assert result is None


def test_detect_nvidia_vram_bytes_device_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Device query failure returns None (best-effort)."""
    pynvml = _install_pynvml_mock(monkeypatch)
    pynvml.nvmlInit.return_value = None
    pynvml.nvmlDeviceGetCount.return_value = 1
    pynvml.nvmlDeviceGetHandleByIndex.side_effect = RuntimeError("bad handle")
    pynvml.nvmlShutdown.return_value = None

    from kodo.llms._hardware import _detect_nvidia_vram_bytes

    result = _detect_nvidia_vram_bytes()
    assert result is None


def test_detect_nvidia_vram_bytes_missing_module_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing pynvml in sys.modules returns None."""
    monkeypatch.delitem(sys.modules, "pynvml", raising=False)

    from kodo.llms._hardware import _detect_nvidia_vram_bytes

    result = _detect_nvidia_vram_bytes()
    assert result is None


def test_detect_nvidia_vram_bytes_multiple_gpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sums VRAM across all visible GPUs."""
    pynvml = _install_pynvml_mock(monkeypatch)
    handle_0 = MagicMock()
    handle_1 = MagicMock()
    mem_0 = MagicMock()
    mem_0.total = 12_000_000_000
    mem_1 = MagicMock()
    mem_1.total = 24_000_000_000

    pynvml.nvmlInit.return_value = None
    pynvml.nvmlDeviceGetCount.return_value = 2
    pynvml.nvmlDeviceGetHandleByIndex.side_effect = [handle_0, handle_1]
    pynvml.nvmlDeviceGetMemoryInfo.side_effect = [mem_0, mem_1]
    pynvml.nvmlShutdown.return_value = None

    from kodo.llms._hardware import _detect_nvidia_vram_bytes

    result = _detect_nvidia_vram_bytes()
    assert result == 36_000_000_000


# ---------------------------------------------------------------------------
# _detect_mac_unified_memory_bytes
# ---------------------------------------------------------------------------


def test_detect_mac_unified_memory_bytes_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """psutil.virtual_memory().total is returned."""
    import psutil

    monkeypatch.setattr(
        psutil,
        "virtual_memory",
        MagicMock(return_value=MagicMock(total=32_000_000_000)),
    )
    from kodo.llms._hardware import _detect_mac_unified_memory_bytes

    result = _detect_mac_unified_memory_bytes()
    assert result == 32_000_000_000


def test_detect_mac_unified_memory_bytes_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """psutil query failure returns None (best-effort)."""
    import psutil

    monkeypatch.setattr(psutil, "virtual_memory", MagicMock(side_effect=OSError("no access")))
    from kodo.llms._hardware import _detect_mac_unified_memory_bytes

    result = _detect_mac_unified_memory_bytes()
    assert result is None


def test_detect_mac_unified_memory_bytes_no_psutil_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If psutil itself is missing (ImportError during import), returns None.

    We simulate the import failure by making the psutil module raise
    ImportError on attribute access (the broad except Exception in the
    function will then catch it).
    """
    import kodo.llms._hardware as _hw

    class _BrokenPsutil:
        """A module whose virtual_memory raises ImportError when accessed."""

        @staticmethod
        def virtual_memory():
            raise ImportError("psutil unavailable")

    monkeypatch.setitem(sys.modules, "psutil", _BrokenPsutil())
    result = _hw._detect_mac_unified_memory_bytes()
    assert result is None


# ---------------------------------------------------------------------------
# detect_vram_gb
# ---------------------------------------------------------------------------


def test_detect_vram_gb_on_darwin_uses_mac_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """On macOS we go through ``_detect_mac_unified_memory_bytes``."""
    import kodo.llms._hardware as _hw

    mac_helper = MagicMock(return_value=32_000_000_000)
    monkeypatch.setattr(_hw, "_detect_mac_unified_memory_bytes", mac_helper)
    monkeypatch.setattr(_hw.sys, "platform", "darwin")
    result = _hw.detect_vram_gb()
    assert result == 32
    mac_helper.assert_called_once()


def test_detect_vram_gb_on_linux_uses_nvidia(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Linux we go through ``_detect_nvidia_vram_bytes``."""
    import kodo.llms._hardware as _hw

    nvidia_helper = MagicMock(return_value=16_000_000_000)
    monkeypatch.setattr(_hw, "_detect_nvidia_vram_bytes", nvidia_helper)
    monkeypatch.setattr(_hw.sys, "platform", "linux")
    result = _hw.detect_vram_gb()
    assert result == 16
    nvidia_helper.assert_called_once()


def test_detect_vram_gb_none_when_nothing_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    import kodo.llms._hardware as _hw

    monkeypatch.setattr(_hw, "_detect_mac_unified_memory_bytes", MagicMock(return_value=None))
    monkeypatch.setattr(_hw.sys, "platform", "darwin")
    result = _hw.detect_vram_gb()
    assert result is None


def test_detect_vram_gb_snaps_to_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raw byte counts that don't land on a tier get snapped."""
    import kodo.llms._hardware as _hw

    # ~7.9 GiB -- should snap to 8.
    monkeypatch.setattr(
        _hw,
        "_detect_mac_unified_memory_bytes",
        MagicMock(return_value=int(7.9 * 1024**3)),
    )
    monkeypatch.setattr(_hw.sys, "platform", "darwin")
    result = _hw.detect_vram_gb()
    assert result == 8


# ---------------------------------------------------------------------------
# detect_ram_gb
# ---------------------------------------------------------------------------


def test_detect_ram_gb_on_darwin_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS returns None -- VRAM already covers unified memory."""
    import kodo.llms._hardware as _hw

    monkeypatch.setattr(_hw.sys, "platform", "darwin")
    result = _hw.detect_ram_gb()
    assert result is None


def test_detect_ram_gb_on_linux_uses_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Linux we go through psutil for system RAM."""
    import psutil

    import kodo.llms._hardware as _hw

    monkeypatch.setattr(_hw.sys, "platform", "linux")
    monkeypatch.setattr(
        psutil,
        "virtual_memory",
        MagicMock(return_value=MagicMock(total=64_000_000_000)),
    )
    result = _hw.detect_ram_gb()
    assert result == 64


def test_detect_ram_gb_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """psutil query failure returns None (best-effort)."""
    import psutil

    import kodo.llms._hardware as _hw

    monkeypatch.setattr(_hw.sys, "platform", "linux")
    monkeypatch.setattr(psutil, "virtual_memory", MagicMock(side_effect=OSError("no access")))
    result = _hw.detect_ram_gb()
    assert result is None
