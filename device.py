"""
Centralized device-selection for CUDA → MPS → CPU.

Usage
-----
from device import get_device, to_device, DeviceConfig

device = get_device()          # returns torch.device
cfg    = DeviceConfig.auto()   # full config object (dtype, amp, etc.)
tensor = to_device(tensor)     # moves any tensor / module to the best device
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core selection
# ---------------------------------------------------------------------------

def get_device(requested: Optional[str] = None) -> "torch.device":  # noqa: F821
    """
    Return the best available torch.device.

    Priority: CUDA → MPS → CPU

    Args:
        requested: "cuda" | "mps" | "cpu" | "auto" | None
                   None / "auto" → pick automatically.
    """
    import torch

    if requested and requested not in ("auto", "cuda", "mps", "cpu"):
        raise ValueError(f"Unknown device: {requested!r}. Use 'cuda', 'mps', 'cpu', or 'auto'.")

    if not requested or requested == "auto":
        if torch.cuda.is_available():
            chosen = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            chosen = "mps"
        else:
            chosen = "cpu"
    else:
        chosen = _validate_requested(requested)

    device = torch.device(chosen)
    logger.info("Device selected: %s", device)
    return device


def _validate_requested(requested: str) -> str:
    import torch

    if requested == "cuda" and not torch.cuda.is_available():
        logger.warning("CUDA requested but not available — falling back to CPU.")
        return "cpu"
    if requested == "mps":
        if not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
            logger.warning("MPS requested but not available — falling back to CPU.")
            return "cpu"
    return requested


# ---------------------------------------------------------------------------
# DeviceConfig  — carries dtype, amp flag, and cache helpers
# ---------------------------------------------------------------------------

@dataclass
class DeviceConfig:
    """
    Full device profile: device + recommended dtype + AMP availability.

    Build with :meth:`DeviceConfig.auto` for automatic selection.
    """

    device: "torch.device"  # noqa: F821
    dtype: "torch.dtype"    # noqa: F821
    amp_enabled: bool
    device_name: str = field(init=False)

    def __post_init__(self) -> None:
        self.device_name = str(self.device)

    # ------------------------------------------------------------------
    @classmethod
    def auto(cls, requested: Optional[str] = None) -> "DeviceConfig":
        """Auto-select the best device and derive dtype / AMP settings."""
        import torch

        dev = get_device(requested)
        dev_type = dev.type

        if dev_type == "cuda":
            dtype = torch.float16
            amp = True
        else:
            # MPS float16 has limited op support; keep float32.
            dtype = torch.float32
            amp = False

        return cls(device=dev, dtype=dtype, amp_enabled=amp)

    # ------------------------------------------------------------------
    def log_info(self) -> None:
        """Print device + memory information."""
        import torch

        logger.info(
            "DeviceConfig | device=%s | dtype=%s | amp=%s",
            self.device,
            self.dtype,
            self.amp_enabled,
        )

        if self.device.type == "cuda":
            idx = self.device.index or 0
            name = torch.cuda.get_device_name(idx)
            total = torch.cuda.get_device_properties(idx).total_memory / 2**30
            logger.info("  GPU: %s — %.1f GB VRAM", name, total)
        elif self.device.type == "mps":
            logger.info("  Backend: Apple Silicon MPS")
        else:
            import os
            logger.info("  CPU cores: %s", os.cpu_count())

    def empty_cache(self) -> None:
        """Release unused memory on the current device."""
        import torch

        if self.device.type == "cuda":
            torch.cuda.empty_cache()
        elif self.device.type == "mps" and hasattr(torch.mps, "empty_cache"):
            torch.mps.empty_cache()


# ---------------------------------------------------------------------------
# Convenience helper
# ---------------------------------------------------------------------------

_T = Union["torch.Tensor", "torch.nn.Module"]  # noqa: F821


def to_device(obj: _T, device: Optional["torch.device"] = None) -> _T:  # noqa: F821
    """Move a tensor or nn.Module to *device* (defaults to best available)."""
    if device is None:
        device = get_device()
    return obj.to(device)
