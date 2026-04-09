"""
LLM 백엔드 추상 인터페이스
모든 백엔드 구현은 이 클래스를 상속해야 합니다.
"""

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional, Iterator, Any
import time


@dataclass
class BackendConfig:
    """백엔드 설정"""
    model: str = "llama3.1:8b"
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 2048
    context_length: int = 4096
    timeout: int = 120
    retry_attempts: int = 3
    # 추가 백엔드별 설정
    extra: dict = field(default_factory=dict)

    def model_post_init(self, *args: Any) -> None:
        pass


@dataclass
class GenerateRequest:
    """LLM 생성 요청"""
    prompt: str = ""
    system_prompt: str = ""
    messages: list[dict] = field(default_factory=list)  # 대화 히스토리
    model_override: Optional[str] = None  # 이 요청에만 다른 모델 사용
    temperature_override: Optional[float] = None
    max_tokens_override: Optional[int] = None
    context_length_override: Optional[int] = None
    stream: bool = False
    images: list[str] = field(default_factory=list)  # 비전 모델용 이미지 경로


@dataclass
class GenerateResponse:
    """LLM 생성 응답"""
    content: str = ""
    model: str = ""
    tokens_input: int = 0
    tokens_output: int = 0
    duration_ms: float = 0.0
    success: bool = True
    error: str = ""
    raw_response: Optional[dict] = None


class LLMBackend(ABC):
    """
    LLM 백엔드 추상 기본 클래스
    Ollama, HuggingFace Transformers 등의 구현체가 이를 상속합니다.
    """

    def __init__(self, config: BackendConfig):
        self.config = config
        self._initialized = False

    @abstractmethod
    def initialize(self) -> bool:
        """
        백엔드 초기화 (모델 로드 등)
        Returns: 성공 여부
        """
        pass

    @abstractmethod
    def generate(self, request: GenerateRequest) -> GenerateResponse:
        """
        텍스트 생성 (동기)
        Args:
            request: 생성 요청
        Returns: 생성 응답
        """
        pass

    @abstractmethod
    def generate_stream(self, request: GenerateRequest) -> Iterator[str]:
        """
        스트리밍 텍스트 생성
        Args:
            request: 생성 요청
        Yields: 생성된 텍스트 청크
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """백엔드가 사용 가능한지 확인"""
        pass

    @abstractmethod
    def list_models(self) -> list[str]:
        """사용 가능한 모델 목록 반환"""
        pass

    def generate_with_retry(self, request: GenerateRequest) -> GenerateResponse:
        """
        재시도 로직이 포함된 생성
        네트워크 오류나 임시 장애 시 자동 재시도합니다.
        """
        last_error = ""
        current_request = deepcopy(request)

        for attempt in range(self.config.retry_attempts):
            try:
                response = self.generate(current_request)
                if response.success:
                    return response
                last_error = response.error
            except Exception as e:
                last_error = str(e)
            if attempt < self.config.retry_attempts - 1:
                current_request = self._prepare_retry_request(current_request, last_error, attempt)
                # 지수 백오프: 1초, 2초, 4초...
                time.sleep(2 ** attempt)

        return GenerateResponse(
            success=False,
            error=f"최대 재시도 횟수 초과. 마지막 오류: {last_error}",
        )

    def _prepare_retry_request(
        self,
        request: GenerateRequest,
        last_error: str,
        attempt: int,
    ) -> GenerateRequest:
        """
        재시도 전에 요청 크기를 줄이거나 빠른 모델로 폴백한다.
        """
        retry_request = deepcopy(request)
        lowered_error = (last_error or "").lower()
        recoverable_markers = [
            "timeout",
            "타임아웃",
            "500",
            "internal server error",
            "server error",
            "out of memory",
            "resource exhausted",
            "model requires more system memory",
            "insufficient memory",
            "cuda",
            "context",
            "num_ctx",
        ]

        if not any(marker in lowered_error for marker in recoverable_markers):
            return retry_request

        current_max_tokens = retry_request.max_tokens_override or self.config.max_tokens
        reduced_tokens = max(256, current_max_tokens // 2)
        if reduced_tokens < current_max_tokens:
            retry_request.max_tokens_override = reduced_tokens

        current_context = retry_request.context_length_override or self.config.context_length
        reduced_context = max(1024, current_context // 2)
        if reduced_context < current_context:
            retry_request.context_length_override = reduced_context

        current_temperature = retry_request.temperature_override
        if current_temperature is None:
            current_temperature = self.config.temperature
        retry_request.temperature_override = min(current_temperature, 0.3)

        fast_model = self.config.extra.get("fast_model")
        if fast_model and retry_request.model_override != fast_model and attempt >= 0:
            retry_request.model_override = fast_model

        return retry_request

    def _build_messages(self, request: GenerateRequest) -> list[dict]:
        """
        요청으로부터 메시지 리스트 구성
        시스템 프롬프트와 사용자 프롬프트를 적절히 조합합니다.
        """
        messages = []

        # 대화 히스토리가 있으면 사용
        if request.messages:
            messages = request.messages.copy()
            # 시스템 프롬프트가 없으면 추가
            if request.system_prompt and (not messages or messages[0].get("role") != "system"):
                messages.insert(0, {"role": "system", "content": request.system_prompt})
        else:
            # 새 대화 시작
            if request.system_prompt:
                messages.append({"role": "system", "content": request.system_prompt})
            if request.prompt:
                messages.append({"role": "user", "content": request.prompt})

        return messages

    def supports_vision(self) -> bool:
        """비전(이미지 입력) 지원 여부"""
        return False

    def get_context_length(self) -> int:
        """최대 컨텍스트 길이 반환"""
        return self.config.context_length

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(model={self.config.model})"
