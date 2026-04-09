"""
PyTorch 디바이스 선택 유틸리티
CUDA → MPS (Apple Silicon) → CPU 우선순위로 최적 디바이스를 선택합니다.
"""

from __future__ import annotations

from typing import Optional
from ..logging_utils import get_logger

logger = get_logger("backend.device")


def get_best_device() -> str:
    """
    현재 환경에서 사용 가능한 최적의 PyTorch 디바이스를 반환합니다.

    우선순위:
      1. CUDA  — NVIDIA GPU가 있는 경우
      2. MPS   — Apple Silicon (M1/M2/M3) Mac인 경우
      3. CPU   — 폴백

    Returns:
        "cuda" | "mps" | "cpu"
    """
    try:
        import torch
    except ImportError:
        logger.debug("torch가 설치되지 않아 cpu로 폴백합니다.")
        return "cpu"

    if torch.cuda.is_available():
        device = "cuda"
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"

    logger.info(f"선택된 디바이스: {device}")
    return device


def resolve_device(requested: Optional[str] = None) -> str:
    """
    요청된 디바이스 문자열을 실제 사용 가능한 디바이스로 해석합니다.

    Args:
        requested: "auto" | "cuda" | "mps" | "cpu" | None
                   None 또는 "auto"이면 get_best_device()를 사용합니다.

    Returns:
        실제 사용할 디바이스 문자열
    """
    if requested is None or requested == "auto":
        return get_best_device()

    try:
        import torch
    except ImportError:
        return "cpu"

    if requested == "cuda":
        if not torch.cuda.is_available():
            logger.warning("CUDA를 요청했지만 사용할 수 없습니다. CPU로 폴백합니다.")
            return "cpu"
    elif requested == "mps":
        if not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
            logger.warning("MPS를 요청했지만 사용할 수 없습니다. CPU로 폴백합니다.")
            return "cpu"

    return requested


def supports_quantization(device: str) -> bool:
    """
    해당 디바이스에서 bitsandbytes 양자화(4bit/8bit)를 지원하는지 확인합니다.
    bitsandbytes는 현재 CUDA 전용입니다.

    Args:
        device: "cuda" | "mps" | "cpu"

    Returns:
        True이면 4bit/8bit 양자화 가능
    """
    return device == "cuda"


def get_torch_dtype(device: str):
    """
    디바이스에 적합한 기본 torch dtype을 반환합니다.

    - CUDA: float16 (메모리 절약, 대부분 지원)
    - MPS:  float32 (float16은 MPS에서 일부 연산 미지원)
    - CPU:  float32

    Args:
        device: "cuda" | "mps" | "cpu"

    Returns:
        torch.dtype
    """
    try:
        import torch
        if device == "cuda":
            return torch.float16
        else:
            # MPS와 CPU는 float32가 안정적입니다.
            # MPS에서 float16은 일부 연산(예: LayerNorm)이 지원되지 않을 수 있습니다.
            return torch.float32
    except ImportError:
        return None


def log_memory_stats(device: str) -> None:
    """
    선택된 디바이스의 메모리 사용량을 로깅합니다.

    Args:
        device: "cuda" | "mps" | "cpu"
    """
    try:
        import torch
        if device == "cuda" and torch.cuda.is_available():
            allocated = torch.cuda.memory_allocated() / 1024 ** 3
            reserved = torch.cuda.memory_reserved() / 1024 ** 3
            logger.info(f"CUDA 메모리: 할당={allocated:.2f}GB, 예약={reserved:.2f}GB")
        elif device == "mps":
            # MPS는 torch 2.x 이상에서 메모리 통계 일부 지원
            if hasattr(torch.mps, "current_allocated_memory"):
                allocated = torch.mps.current_allocated_memory() / 1024 ** 3
                logger.info(f"MPS 메모리 할당: {allocated:.2f}GB")
            else:
                logger.info("MPS 디바이스 사용 중 (메모리 통계 미지원)")
        else:
            logger.info("CPU 디바이스 사용 중")
    except Exception:
        pass


def clear_device_cache(device: str) -> None:
    """
    선택된 디바이스의 캐시를 비웁니다.

    Args:
        device: "cuda" | "mps" | "cpu"
    """
    try:
        import torch
        if device == "cuda" and torch.cuda.is_available():
            torch.cuda.empty_cache()
            logger.debug("CUDA 캐시 비움")
        elif device == "mps":
            if hasattr(torch.mps, "empty_cache"):
                torch.mps.empty_cache()
                logger.debug("MPS 캐시 비움")
    except Exception:
        pass
