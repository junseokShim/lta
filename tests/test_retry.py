"""
재시도(Retry) 동작 검증 테스트

다음 시나리오를 검증합니다:
1. 일시적 실패 후 재시도하여 성공
2. 성공 시 재시도 루프 즉시 종료
3. 치명적 오류 발생 시 즉시 중단 (재시도 없음)
4. 최대 시도 횟수 초과 시 RetryExhausted 발생
5. 지수 백오프 계산 정확도
6. 지터(Jitter) 범위 검증
7. 오류 분류 로직
8. 백엔드 generate_with_retry 통합 테스트
9. 오케스트레이션 스텝 재시도 통합 테스트
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.retry_policy import (
    RetryPolicy,
    RetryExhausted,
    FatalError,
    classify_error,
    retry_call,
    BACKEND_RETRY_POLICY,
    STEP_RETRY_POLICY,
    INFINITE_RETRY_POLICY,
)


# ──────────────────────────────────────────────
# 1. 오류 분류 테스트 (Error Classification)
# ──────────────────────────────────────────────

class TestClassifyError:
    """오류 메시지 분류 로직 검증"""

    def test_timeout_is_recoverable(self):
        assert classify_error("Connection timeout after 120s") == "recoverable"

    def test_server_error_is_recoverable(self):
        assert classify_error("500 internal server error") == "recoverable"

    def test_oom_is_recoverable(self):
        assert classify_error("out of memory") == "recoverable"

    def test_model_not_found_is_fatal(self):
        assert classify_error("model not found: llama3.1") == "fatal"

    def test_auth_failure_is_fatal(self):
        assert classify_error("Authentication failed: invalid API key") == "fatal"

    def test_empty_error_is_unknown(self):
        assert classify_error("") == "unknown"

    def test_unrecognized_error_is_unknown(self):
        assert classify_error("Some completely unexpected error XYZ123") == "unknown"

    def test_korean_timeout_is_recoverable(self):
        assert classify_error("Ollama 요청 타임아웃 (120초)") == "recoverable"

    def test_case_insensitive(self):
        assert classify_error("TIMEOUT ERROR") == "recoverable"
        assert classify_error("MODEL NOT FOUND") == "fatal"


# ──────────────────────────────────────────────
# 2. RetryPolicy 계산 테스트
# ──────────────────────────────────────────────

class TestRetryPolicy:
    """RetryPolicy 계산 및 판단 로직 검증"""

    def test_backoff_increases_with_attempt(self):
        """지수 백오프는 시도 횟수에 따라 증가해야 합니다"""
        policy = RetryPolicy(
            max_attempts=10,
            base_interval=1.0,
            backoff_factor=2.0,
            max_interval=100.0,
            jitter=False,  # 지터 비활성화로 결정론적 테스트
        )
        wait_0 = policy.compute_wait(0)  # 1.0 * 2^0 = 1.0
        wait_1 = policy.compute_wait(1)  # 1.0 * 2^1 = 2.0
        wait_2 = policy.compute_wait(2)  # 1.0 * 2^2 = 4.0

        assert wait_0 == pytest.approx(1.0, abs=0.01)
        assert wait_1 == pytest.approx(2.0, abs=0.01)
        assert wait_2 == pytest.approx(4.0, abs=0.01)
        assert wait_0 < wait_1 < wait_2

    def test_backoff_is_capped_at_max_interval(self):
        """대기 시간은 max_interval 을 초과하지 않아야 합니다"""
        policy = RetryPolicy(
            max_attempts=10,
            base_interval=1.0,
            backoff_factor=2.0,
            max_interval=10.0,
            jitter=False,
        )
        # 2^10 = 1024 이지만 cap 은 10.0
        wait_large = policy.compute_wait(10)
        assert wait_large <= 10.0

    def test_jitter_adds_randomness(self):
        """지터가 활성화되면 같은 attempt 에 대해 다른 대기 시간이 나와야 합니다"""
        policy = RetryPolicy(
            max_attempts=10,
            base_interval=4.0,
            backoff_factor=1.0,  # 고정 interval
            max_interval=100.0,
            jitter=True,
        )
        waits = {policy.compute_wait(0) for _ in range(20)}
        # 20회 중 적어도 2개 이상의 서로 다른 값이 나와야 합니다
        assert len(waits) > 1

    def test_jitter_range_within_25_percent(self):
        """지터 범위는 ±25% 이내여야 합니다"""
        policy = RetryPolicy(
            max_attempts=10,
            base_interval=4.0,
            backoff_factor=1.0,
            max_interval=100.0,
            jitter=True,
        )
        base = 4.0
        for _ in range(50):
            w = policy.compute_wait(0)
            assert 4.0 * 0.75 <= w <= 4.0 * 1.25

    def test_should_retry_false_when_max_attempts_exceeded(self):
        """최대 시도 횟수 초과 시 재시도 불가"""
        policy = RetryPolicy(max_attempts=3)
        should, reason = policy.should_retry(3, "unknown", 0.0)
        assert should is False
        assert "초과" in reason

    def test_should_retry_true_within_max_attempts(self):
        """최대 시도 횟수 내라면 재시도 가능"""
        policy = RetryPolicy(max_attempts=5)
        should, reason = policy.should_retry(2, "recoverable", 0.0)
        assert should is True

    def test_should_retry_false_on_fatal_error(self):
        """치명적 오류이면 즉시 재시도 불가"""
        policy = RetryPolicy(max_attempts=0, fatal_stop=True)
        should, reason = policy.should_retry(1, "fatal", 0.0)
        assert should is False
        assert "치명적" in reason

    def test_should_retry_true_when_unlimited(self):
        """max_attempts=0 이면 무제한 재시도"""
        policy = RetryPolicy(max_attempts=0, fatal_stop=False)
        # 1000번째 시도도 재시도 가능해야 합니다
        should, _ = policy.should_retry(1000, "recoverable", 0.0)
        assert should is True

    def test_total_timeout_stops_retry(self):
        """total_timeout 초과 시 재시도 불가"""
        policy = RetryPolicy(max_attempts=0, total_timeout=30.0)
        should, reason = policy.should_retry(5, "recoverable", 35.0)
        assert should is False
        assert "타임아웃" in reason


# ──────────────────────────────────────────────
# 3. retry_call 통합 테스트
# ──────────────────────────────────────────────

class TestRetryCall:
    """retry_call() 함수의 동작 검증"""

    def test_success_on_first_attempt(self):
        """첫 번째 시도에서 성공하면 즉시 반환"""
        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            return "success"

        policy = RetryPolicy(max_attempts=3, base_interval=0.01, jitter=False)
        result = retry_call(fn, policy=policy, operation_name="test")

        assert result == "success"
        assert call_count == 1

    def test_retry_on_transient_failure_then_success(self):
        """일시적 실패 후 재시도하여 성공"""
        call_count = 0
        failure_responses = [
            MagicMock(success=False, error="timeout"),
            MagicMock(success=False, error="500 server error"),
            MagicMock(success=True, error=""),
        ]

        def fn():
            nonlocal call_count
            resp = failure_responses[min(call_count, len(failure_responses) - 1)]
            call_count += 1
            return resp

        policy = RetryPolicy(max_attempts=5, base_interval=0.01, jitter=False)

        result = retry_call(
            fn,
            policy=policy,
            error_extractor=lambda r: None if r.success else r.error,
            success_checker=lambda r: r.success,
            operation_name="test_transient",
        )

        assert result.success is True
        assert call_count == 3

    def test_raises_retry_exhausted_on_max_attempts(self):
        """최대 시도 횟수 초과 시 RetryExhausted 발생"""
        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            return MagicMock(success=False, error="timeout")

        policy = RetryPolicy(max_attempts=3, base_interval=0.01, jitter=False)

        with pytest.raises(RetryExhausted) as exc_info:
            retry_call(
                fn,
                policy=policy,
                error_extractor=lambda r: None if r.success else r.error,
                success_checker=lambda r: r.success,
                operation_name="test_exhausted",
            )

        assert exc_info.value.attempts == 3
        assert call_count == 3

    def test_raises_fatal_on_fatal_error(self):
        """치명적 오류 발생 시 FatalError 발생"""
        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            return MagicMock(success=False, error="model not found: nonexistent_model")

        policy = RetryPolicy(max_attempts=5, base_interval=0.01, jitter=False, fatal_stop=True)

        with pytest.raises(FatalError):
            retry_call(
                fn,
                policy=policy,
                error_extractor=lambda r: None if r.success else r.error,
                success_checker=lambda r: r.success,
                operation_name="test_fatal",
            )

        # 치명적 오류이므로 딱 1번만 시도
        assert call_count == 1

    def test_exception_in_fn_triggers_retry(self):
        """fn()이 예외를 던지면 재시도해야 합니다"""
        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("connection refused")
            return "recovered"

        policy = RetryPolicy(max_attempts=5, base_interval=0.01, jitter=False)
        result = retry_call(fn, policy=policy, operation_name="test_exception")

        assert result == "recovered"
        assert call_count == 3

    def test_on_retry_callback_called(self):
        """on_retry 콜백이 재시도마다 호출되어야 합니다"""
        call_count = 0
        retry_events = []

        def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("temporary error")
            return "ok"

        def on_retry(attempt, error_msg, wait_time):
            retry_events.append((attempt, error_msg))

        policy = RetryPolicy(max_attempts=5, base_interval=0.01, jitter=False)
        retry_call(fn, policy=policy, on_retry=on_retry, operation_name="test_callback")

        assert len(retry_events) == 2
        assert retry_events[0][0] == 1
        assert retry_events[1][0] == 2


# ──────────────────────────────────────────────
# 4. 백엔드 generate_with_retry 통합 테스트
# ──────────────────────────────────────────────

class TestBackendGenerateWithRetry:
    """LLMBackend.generate_with_retry() 동작 검증"""

    def _make_backend(self):
        from src.backends.base import BackendConfig, GenerateRequest, GenerateResponse, LLMBackend

        class FakeBackend(LLMBackend):
            def __init__(self):
                super().__init__(BackendConfig(model="test", retry_attempts=3))
                self.call_count = 0
                self.responses = []

            def initialize(self): return True
            def is_available(self): return True
            def list_models(self): return ["test"]
            def generate_stream(self, request): yield "test"

            def generate(self, request):
                resp = self.responses[min(self.call_count, len(self.responses) - 1)]
                self.call_count += 1
                return resp

        return FakeBackend()

    def test_returns_success_immediately(self):
        """첫 번째 성공 응답 즉시 반환"""
        from src.backends.base import GenerateRequest, GenerateResponse, BackendConfig
        backend = self._make_backend()
        backend.responses = [GenerateResponse(content="hello", success=True)]

        req = GenerateRequest(prompt="test")
        policy = RetryPolicy(max_attempts=3, base_interval=0.01, jitter=False)
        resp = backend.generate_with_retry(req, policy=policy)

        assert resp.success is True
        assert resp.content == "hello"
        assert backend.call_count == 1

    def test_retries_on_timeout_then_succeeds(self):
        """타임아웃 후 재시도하여 성공"""
        from src.backends.base import GenerateRequest, GenerateResponse
        backend = self._make_backend()
        backend.responses = [
            GenerateResponse(success=False, error="timeout: request timed out"),
            GenerateResponse(success=False, error="timeout: request timed out"),
            GenerateResponse(content="success after retry", success=True),
        ]

        req = GenerateRequest(prompt="test")
        policy = RetryPolicy(max_attempts=5, base_interval=0.01, jitter=False)
        resp = backend.generate_with_retry(req, policy=policy)

        assert resp.success is True
        assert "success after retry" in resp.content
        assert backend.call_count == 3

    def test_stops_on_fatal_error(self):
        """치명적 오류(모델 없음) 발생 시 재시도 없이 즉시 실패 반환"""
        from src.backends.base import GenerateRequest, GenerateResponse
        backend = self._make_backend()
        backend.responses = [
            GenerateResponse(success=False, error="model not found: llama3.1:8b"),
        ]

        req = GenerateRequest(prompt="test")
        policy = RetryPolicy(max_attempts=5, base_interval=0.01, jitter=False, fatal_stop=True)
        resp = backend.generate_with_retry(req, policy=policy)

        assert resp.success is False
        # 치명적 오류이므로 정확히 1번만 시도
        assert backend.call_count == 1

    def test_exhausts_retries_returns_failure_response(self):
        """최대 재시도 초과 시 성공=False 응답 반환 (예외 아님)"""
        from src.backends.base import GenerateRequest, GenerateResponse
        backend = self._make_backend()
        backend.responses = [
            GenerateResponse(success=False, error="500 server error"),
        ]

        req = GenerateRequest(prompt="test")
        policy = RetryPolicy(max_attempts=3, base_interval=0.01, jitter=False)
        resp = backend.generate_with_retry(req, policy=policy)

        assert resp.success is False
        assert "최대 재시도" in resp.error or "시도" in resp.error
        assert backend.call_count == 3


# ──────────────────────────────────────────────
# 5. 오케스트레이션 스텝 재시도 통합 테스트
# ──────────────────────────────────────────────

class TestOrchestrationStepRetry:
    """OrchestrationEngine._execute_step_with_retry() 동작 검증"""

    def _make_engine(self, tmp_path):
        from src.setup import create_engine
        return create_engine(workspace_root=str(tmp_path))

    def test_step_retry_succeeds_after_failure(self, tmp_path):
        """스텝 실패 후 재시도하여 성공"""
        from src.orchestration.messages import OrchestrationState, AgentResult, AgentRole, TaskStatus
        from src.retry_policy import RetryPolicy

        engine = self._make_engine(tmp_path)
        state = OrchestrationState(original_task="테스트 작업")
        state.status = TaskStatus.IN_PROGRESS

        step = {"step_num": 1, "title": "테스트 스텝", "description": "테스트", "assigned_agent": "coder"}

        call_count = 0

        def mock_execute_step(s, st, retry_feedback=""):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return AgentResult(
                    task_id="t1",
                    agent_name="Coder",
                    agent_role=AgentRole.CODER,
                    content="코드 계획만 있고 구현 없음",
                    success=False,
                    error="timeout",
                )
            return AgentResult(
                task_id="t1",
                agent_name="Coder",
                agent_role=AgentRole.CODER,
                content="File: app.py\n```python\nprint('hello')\n```",
                success=True,
            )

        engine._execute_step = mock_execute_step
        # store_result 는 실제 동작 허용

        policy = RetryPolicy(max_attempts=5, base_interval=0.01, jitter=False)
        result = engine._execute_step_with_retry(state, step, policy=policy)

        assert result.success is True
        assert call_count == 3

    def test_step_retry_raises_after_exhaustion(self, tmp_path):
        """최대 재시도 초과 시 RuntimeError 발생"""
        from src.orchestration.messages import OrchestrationState, AgentResult, AgentRole, TaskStatus
        from src.retry_policy import RetryPolicy

        engine = self._make_engine(tmp_path)
        state = OrchestrationState(original_task="테스트 작업")
        state.status = TaskStatus.IN_PROGRESS

        step = {"step_num": 1, "title": "테스트 스텝", "description": "테스트", "assigned_agent": "coder"}

        def mock_execute_step(s, st, retry_feedback=""):
            return AgentResult(
                task_id="t1",
                agent_name="Coder",
                agent_role=AgentRole.CODER,
                content="계속 실패",
                success=False,
                error="timeout",
            )

        engine._execute_step = mock_execute_step

        policy = RetryPolicy(max_attempts=2, base_interval=0.01, jitter=False)

        with pytest.raises(RuntimeError) as exc_info:
            engine._execute_step_with_retry(state, step, policy=policy)

        assert "최종 실패" in str(exc_info.value)
        assert "2회 시도" in str(exc_info.value)

    def test_step_retry_stops_on_fatal_error(self, tmp_path):
        """치명적 오류 발생 시 즉시 RuntimeError 발생"""
        from src.orchestration.messages import OrchestrationState, AgentResult, AgentRole, TaskStatus
        from src.retry_policy import RetryPolicy

        engine = self._make_engine(tmp_path)
        state = OrchestrationState(original_task="테스트 작업")
        state.status = TaskStatus.IN_PROGRESS

        step = {"step_num": 1, "title": "테스트 스텝", "description": "테스트", "assigned_agent": "coder"}

        call_count = 0

        def mock_execute_step(s, st, retry_feedback=""):
            nonlocal call_count
            call_count += 1
            return AgentResult(
                task_id="t1",
                agent_name="Coder",
                agent_role=AgentRole.CODER,
                content="실패",
                success=False,
                error="model not found: unknown_model",
            )

        engine._execute_step = mock_execute_step

        policy = RetryPolicy(max_attempts=5, base_interval=0.01, jitter=False, fatal_stop=True)

        with pytest.raises(RuntimeError):
            engine._execute_step_with_retry(state, step, policy=policy)

        # 치명적 오류이므로 1번만 시도
        assert call_count == 1


# ──────────────────────────────────────────────
# 6. 기본 정책 인스턴스 검증
# ──────────────────────────────────────────────

class TestDefaultPolicies:
    """기본 정책 인스턴스의 설정값 검증"""

    def test_backend_policy_has_reasonable_defaults(self):
        assert BACKEND_RETRY_POLICY.max_attempts == 3
        assert BACKEND_RETRY_POLICY.base_interval == 1.0
        assert BACKEND_RETRY_POLICY.backoff_factor == 2.0
        assert BACKEND_RETRY_POLICY.max_interval == 30.0
        assert BACKEND_RETRY_POLICY.jitter is True
        assert BACKEND_RETRY_POLICY.fatal_stop is True

    def test_step_policy_is_more_generous(self):
        assert STEP_RETRY_POLICY.max_attempts == 5
        assert STEP_RETRY_POLICY.base_interval >= 1.0

    def test_infinite_policy_has_no_limit(self):
        assert INFINITE_RETRY_POLICY.max_attempts == 0  # 무제한
        assert INFINITE_RETRY_POLICY.total_timeout is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
