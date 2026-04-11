"""
LLM 백엔드 추상 인터페이스
모든 백엔드 구현은 이 클래스를 상속해야 합니다.
"""

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional, Iterator, Any
import time

from ..retry_policy import (
    RetryPolicy,
    BACKEND_RETRY_POLICY,
    classify_error,
    FatalError,
    RetryExhausted,
)


@dataclass
class BackendConfig:
    """백엔드 설정"""
    model: str = "llama3.1:8b"
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 4096
    context_length: int = 4096
    timeout: int = 600
    retry_attempts: int = 5
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

    def generate_with_retry(
        self,
        request: GenerateRequest,
        policy: Optional[RetryPolicy] = None,
    ) -> GenerateResponse:
        """
        재시도 정책이 적용된 LLM 생성 메서드.

        [재시도 정책]
        - 기본값: BACKEND_RETRY_POLICY (3회 시도, 지수 백오프, 지터)
        - policy 파라미터로 커스텀 정책 주입 가능
        - config.retry_attempts 값이 있으면 policy.max_attempts 로 동기화

        [회복 가능 vs 치명적 오류]
        - 타임아웃, 서버 오류, 메모리 부족 등 → 재시도
        - 모델 없음, 인증 실패, 잘못된 요청 → 즉시 실패

        [종료 조건]
        - 성공 응답 반환 → 루프 종료
        - 최대 시도 횟수 초과 → 실패 응답 반환
        - 치명적 오류 → 실패 응답 반환 (예외 불전파)
        """
        import logging
        _logger = logging.getLogger("backend.retry")

        # 정책 결정: 주입된 정책 > 기본 BACKEND_RETRY_POLICY, retry_attempts 동기화
        if policy is None:
            policy = RetryPolicy(
                max_attempts=self.config.retry_attempts,  # 설정값 존중
                base_interval=BACKEND_RETRY_POLICY.base_interval,
                backoff_factor=BACKEND_RETRY_POLICY.backoff_factor,
                max_interval=BACKEND_RETRY_POLICY.max_interval,
                jitter=BACKEND_RETRY_POLICY.jitter,
                fatal_stop=BACKEND_RETRY_POLICY.fatal_stop,
            )

        current_request = deepcopy(request)
        last_error = ""
        last_exception: Optional[Exception] = None
        attempt = 0

        import time as _time
        loop_start = _time.time()

        while True:
            attempt += 1
            _logger.debug("LLM 생성 시도 %d (model=%s)", attempt, current_request.model_override or self.config.model)

            try:
                response = self.generate(current_request)

                if response.success:
                    # 성공 — 재시도 루프 종료
                    if attempt > 1:
                        _logger.info("LLM 생성 %d번째 시도에서 성공", attempt)
                    return response

                # 논리적 실패 (HTTP 200 이지만 success=False)
                last_error = response.error or "알 수 없는 오류"
                last_exception = None

            except Exception as exc:
                last_error = str(exc)
                last_exception = exc

            # 오류 분류 (치명적 vs 회복 가능)
            error_class = classify_error(last_error)
            _logger.warning(
                "LLM 생성 시도 %d 실패 [%s]: %s",
                attempt, error_class, last_error[:200]
            )

            # 재시도 여부 판단
            elapsed = _time.time() - loop_start
            should, reason = policy.should_retry(attempt, error_class, elapsed)

            if not should:
                _logger.error("LLM 생성 재시도 중단 — %s (총 %d회 시도)", reason, attempt)
                return GenerateResponse(
                    success=False,
                    error=f"최대 재시도 횟수 초과 ({attempt}회 시도). 마지막 오류: {last_error}",
                )

            # 다음 시도 준비: 컨텍스트/토큰 크기 축소 등 적용
            current_request = self._prepare_retry_request(current_request, last_error, attempt - 1)

            # 지수 백오프 + 지터 대기 (타이트한 루프 방지)
            wait_time = policy.compute_wait(attempt - 1)
            _logger.info(
                "LLM 생성 %d번째 실패 후 %.1f초 대기 후 재시도... (원인: %s)",
                attempt, wait_time, last_error[:100]
            )
            _time.sleep(wait_time)

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
