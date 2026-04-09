"""
재시도 정책 모듈 (Retry Policy Module)

일시적 장애를 견디는 강건한 재시도 오케스트레이션을 제공합니다.
- 성공할 때까지 기본적으로 재시도 (설정 가능)
- 지수 백오프 + 상한 캡 + 선택적 지터(jitter)
- 치명적(fatal) 오류와 회복 가능(recoverable) 오류 구분
- 매 시도 및 실패 원인 로깅
- 타임아웃 처리
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple, Type, Union

logger = logging.getLogger("retry_policy")


# ──────────────────────────────────────────────
# 치명적 오류 분류 (Fatal Error Classification)
# ──────────────────────────────────────────────

# 아래 키워드를 포함하는 오류 메시지는 재시도해도 소용 없는 치명적 오류로 처리
FATAL_ERROR_KEYWORDS: list[str] = [
    "model not found",
    "모델을 찾을 수 없",
    "invalid api key",
    "authentication failed",
    "인증 실패",
    "permission denied",
    "권한이 없",
    "not supported",
    "지원하지 않는",
    "invalid request",
    "잘못된 요청",
    "context window exceeded",  # 컨텍스트 초과는 토큰 축소로 처리하므로 여기서 제외 가능
]

# 재시도 가능 오류 키워드 (연결/서버 일시 장애)
RECOVERABLE_ERROR_KEYWORDS: list[str] = [
    "timeout",
    "타임아웃",
    "connection",
    "연결",
    "500",
    "502",
    "503",
    "504",
    "internal server error",
    "server error",
    "out of memory",
    "resource exhausted",
    "model requires more system memory",
    "insufficient memory",
    "cuda",
    "context",
    "num_ctx",
    "rate limit",
    "too many requests",
    "service unavailable",
    "temporarily unavailable",
    "retry",
]


def classify_error(error_message: str) -> str:
    """
    오류 메시지를 분류합니다.
    Returns:
        "fatal"       - 재시도해도 해결 불가능한 오류
        "recoverable" - 재시도로 해결 가능한 일시적 오류
        "unknown"     - 판단 불가 (보수적으로 재시도 허용)
    """
    lowered = (error_message or "").lower()

    # 치명적 오류 우선 체크
    for keyword in FATAL_ERROR_KEYWORDS:
        if keyword in lowered:
            return "fatal"

    # 회복 가능 오류 체크
    for keyword in RECOVERABLE_ERROR_KEYWORDS:
        if keyword in lowered:
            return "recoverable"

    # 알 수 없는 경우 — 보수적으로 재시도 허용
    return "unknown"


# ──────────────────────────────────────────────
# 재시도 정책 설정 (Retry Policy Config)
# ──────────────────────────────────────────────

@dataclass
class RetryPolicy:
    """
    재시도 동작을 제어하는 정책 클래스.

    Attributes:
        max_attempts: 최대 시도 횟수. 0 또는 음수이면 무제한 재시도.
        base_interval: 첫 번째 대기 시간(초). 기본 1.0초.
        backoff_factor: 지수 백오프 배율. 기본 2.0 (매 실패 시 대기 시간 2배).
        max_interval: 대기 시간 상한선(초). 기본 60초.
        jitter: True이면 대기 시간에 ±25% 무작위 흔들림 추가.
        operation_timeout: 단일 시도의 최대 실행 시간(초). None이면 무제한.
        total_timeout: 전체 재시도 루프의 최대 시간(초). None이면 무제한.
        fatal_stop: True이면 치명적 오류 발생 시 즉시 종료 (기본 True).
    """

    max_attempts: int = 0                  # 0 = 무제한 재시도
    base_interval: float = 1.0             # 기본 대기 시간(초)
    backoff_factor: float = 2.0            # 지수 백오프 배율
    max_interval: float = 60.0             # 최대 대기 시간 상한(초)
    jitter: bool = True                    # 지터 활성화 여부
    operation_timeout: Optional[float] = None  # 단일 시도 타임아웃
    total_timeout: Optional[float] = None  # 전체 루프 타임아웃
    fatal_stop: bool = True                # 치명적 오류 즉시 중단 여부

    def compute_wait(self, attempt: int) -> float:
        """
        현재 시도 번호(0-indexed)에 대한 대기 시간을 계산합니다.
        - 지수 백오프: base_interval * (backoff_factor ** attempt)
        - 상한 캡 적용: max_interval
        - 지터 적용: ±25% 무작위 흔들림
        """
        # 지수 백오프 계산
        wait = self.base_interval * (self.backoff_factor ** attempt)

        # 상한 캡 적용 (타이트한 무한 루프 방지)
        wait = min(wait, self.max_interval)

        # 선택적 지터 추가 (동시 다발적 재시도 시 충돌 분산)
        if self.jitter:
            jitter_range = wait * 0.25
            wait = wait + random.uniform(-jitter_range, jitter_range)

        # 최소 0.1초 보장 (너무 빠른 재시도 방지)
        return max(0.1, wait)

    def should_retry(self, attempt: int, error_class: str, elapsed: float) -> Tuple[bool, str]:
        """
        현재 상태에서 재시도 여부를 판단합니다.

        Args:
            attempt: 지금까지의 시도 횟수 (1-indexed: 첫 실패 후 = 1)
            error_class: classify_error() 반환값
            elapsed: 전체 경과 시간(초)

        Returns:
            (should_retry: bool, reason: str)
        """
        # 치명적 오류이면 즉시 중단
        if self.fatal_stop and error_class == "fatal":
            return False, "치명적 오류 — 재시도 불가"

        # 최대 시도 횟수 초과 체크 (0 이하면 무제한)
        if self.max_attempts > 0 and attempt >= self.max_attempts:
            return False, f"최대 시도 횟수({self.max_attempts}) 초과"

        # 전체 타임아웃 체크
        if self.total_timeout is not None and elapsed >= self.total_timeout:
            return False, f"전체 타임아웃({self.total_timeout}초) 초과"

        return True, "재시도 가능"


# ──────────────────────────────────────────────
# 기본 정책 인스턴스 (Default Policy Instances)
# ──────────────────────────────────────────────

# LLM 백엔드용 — 기본 3회 시도, 짧은 백오프
BACKEND_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    base_interval=1.0,
    backoff_factor=2.0,
    max_interval=30.0,
    jitter=True,
    fatal_stop=True,
)

# 오케스트레이션 스텝용 — 더 넉넉하게 5회, 중간 백오프
STEP_RETRY_POLICY = RetryPolicy(
    max_attempts=5,
    base_interval=2.0,
    backoff_factor=2.0,
    max_interval=60.0,
    jitter=True,
    fatal_stop=True,
)

# 무제한 재시도용 (외부 서비스 가용 대기 등)
INFINITE_RETRY_POLICY = RetryPolicy(
    max_attempts=0,           # 무제한
    base_interval=2.0,
    backoff_factor=2.0,
    max_interval=120.0,
    jitter=True,
    total_timeout=None,       # 전체 타임아웃 없음
    fatal_stop=True,
)


# ──────────────────────────────────────────────
# 재시도 실행기 (Retry Executor)
# ──────────────────────────────────────────────

class RetryExhausted(Exception):
    """최대 재시도 횟수를 초과했을 때 발생하는 예외"""
    def __init__(self, attempts: int, last_error: str, last_exception: Optional[Exception] = None):
        self.attempts = attempts
        self.last_error = last_error
        self.last_exception = last_exception
        super().__init__(
            f"재시도 {attempts}회 후 실패. 마지막 오류: {last_error}"
        )


class FatalError(Exception):
    """치명적 오류 — 재시도 불가"""
    def __init__(self, error: str, original_exception: Optional[Exception] = None):
        self.original_exception = original_exception
        super().__init__(f"치명적 오류 (재시도 불가): {error}")


def retry_call(
    fn: Callable,
    policy: RetryPolicy = BACKEND_RETRY_POLICY,
    error_extractor: Optional[Callable[[any], Optional[str]]] = None,
    success_checker: Optional[Callable[[any], bool]] = None,
    on_retry: Optional[Callable[[int, str, float], None]] = None,
    operation_name: str = "operation",
) -> any:
    """
    재시도 정책에 따라 fn()을 반복 호출합니다.

    Args:
        fn: 실행할 함수 (인자 없음)
        policy: RetryPolicy 인스턴스
        error_extractor: 결과 객체에서 오류 메시지를 추출하는 함수
                         (예: lambda r: None if r.success else r.error)
        success_checker: 결과가 성공인지 판단하는 함수
                         (예: lambda r: r.success)
                         None이면 예외 발생 여부로만 판단
        on_retry: 재시도 시 호출될 콜백 (attempt, error_msg, wait_time)
        operation_name: 로그용 작업 이름

    Returns:
        fn()의 성공 결과

    Raises:
        FatalError: 치명적 오류 발생 시
        RetryExhausted: 최대 재시도 초과 시
    """
    attempt = 0
    last_error = ""
    last_exception: Optional[Exception] = None
    loop_start = time.time()

    while True:
        attempt += 1
        elapsed = time.time() - loop_start

        logger.debug(
            "[%s] 시도 %d 시작 (경과: %.1f초)",
            operation_name, attempt, elapsed
        )

        try:
            result = fn()

            # 성공 체크 (성공 체커가 있으면 사용)
            if success_checker is not None:
                is_success = success_checker(result)
            else:
                is_success = True  # 예외 없으면 성공으로 간주

            # 오류 메시지 추출 (오류 추출기가 있으면 사용)
            error_msg = None
            if error_extractor is not None:
                error_msg = error_extractor(result)

            if is_success and not error_msg:
                # 성공 — 루프 종료
                if attempt > 1:
                    logger.info(
                        "[%s] %d번째 시도에서 성공 (총 경과: %.1f초)",
                        operation_name, attempt, time.time() - loop_start
                    )
                return result

            # 논리적 실패 (예외 없이 실패 결과 반환)
            last_error = error_msg or "알 수 없는 실패"
            last_exception = None

        except Exception as exc:
            # 예외로 인한 실패
            last_error = str(exc)
            last_exception = exc
            logger.debug(
                "[%s] 시도 %d 예외 발생: %s",
                operation_name, attempt, last_error
            )

        # 오류 분류
        error_class = classify_error(last_error)

        logger.warning(
            "[%s] 시도 %d 실패 [%s]: %s",
            operation_name, attempt, error_class, last_error[:200]
        )

        # 재시도 여부 판단
        should, reason = policy.should_retry(attempt, error_class, time.time() - loop_start)

        if not should:
            logger.error(
                "[%s] 재시도 중단 — %s (총 %d회 시도)",
                operation_name, reason, attempt
            )
            if error_class == "fatal":
                exc_to_wrap = last_exception or Exception(last_error)
                raise FatalError(last_error, exc_to_wrap)
            raise RetryExhausted(attempt, last_error, last_exception)

        # 대기 시간 계산 및 대기
        wait_time = policy.compute_wait(attempt - 1)

        logger.info(
            "[%s] %d번째 실패 후 %.1f초 대기 후 재시도... (원인: %s)",
            operation_name, attempt, wait_time, last_error[:100]
        )

        if on_retry:
            try:
                on_retry(attempt, last_error, wait_time)
            except Exception:
                pass  # 콜백 실패는 무시

        time.sleep(wait_time)
