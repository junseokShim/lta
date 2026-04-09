from .base import LLMBackend, BackendConfig, GenerateRequest, GenerateResponse
from .ollama_backend import OllamaBackend
from .device_utils import get_best_device, resolve_device, supports_quantization

__all__ = [
    "LLMBackend",
    "BackendConfig",
    "GenerateRequest",
    "GenerateResponse",
    "OllamaBackend",
    "get_best_device",
    "resolve_device",
    "supports_quantization",
]


def create_backend(backend_type: str, config: dict) -> "LLMBackend":
    """백엔드 타입에 따라 적절한 백엔드 인스턴스를 생성합니다"""
    if backend_type == "ollama":
        from .ollama_backend import OllamaBackend
        return OllamaBackend(BackendConfig(**config))
    elif backend_type == "transformers":
        from .transformers_backend import TransformersBackend
        return TransformersBackend(BackendConfig(**config))
    else:
        raise ValueError(f"지원하지 않는 백엔드 타입: {backend_type}")
