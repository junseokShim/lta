"""
에이전트 기본 클래스
모든 전문 에이전트는 이 클래스를 상속합니다.
백엔드, 도구, 메모리에 대한 공통 인터페이스를 제공합니다.
"""

import time
from abc import ABC, abstractmethod
from typing import Optional, Any, Iterator

from ..backends.base import LLMBackend, GenerateRequest, GenerateResponse
from ..orchestration.messages import AgentTask, AgentResult, AgentRole, Artifact
from ..logging_utils import AgentLogger
from ..tools.filesystem import FilesystemTool
from ..tools.document import DocumentTool
from ..tools.image import ImageTool
from ..tools.shell import ShellTool
from ..tools.web_search import WebSearchTool


class AgentBase(ABC):
    """
    에이전트 기본 클래스

    각 에이전트는:
    1. 고유한 역할과 시스템 프롬프트를 가집니다.
    2. LLM 백엔드를 통해 추론합니다.
    3. 도구(파일시스템, 문서, 이미지, 셸)를 활용합니다.
    4. 태스크를 받아 AgentResult를 반환합니다.
    """

    def __init__(
        self,
        backend: LLMBackend,
        role: AgentRole,
        name: str,
        filesystem_tool: Optional[FilesystemTool] = None,
        document_tool: Optional[DocumentTool] = None,
        image_tool: Optional[ImageTool] = None,
        shell_tool: Optional[ShellTool] = None,
        web_search_tool: Optional[WebSearchTool] = None,
        model_override: Optional[str] = None,
        use_fast_model: bool = False,
        fast_model: Optional[str] = None,
    ):
        self.backend = backend
        self.role = role
        self.name = name
        self.logger = AgentLogger(name)

        # 도구 인스턴스
        self.fs = filesystem_tool
        self.doc = document_tool
        self.img = image_tool
        self.shell = shell_tool
        self.web = web_search_tool

        # 모델 설정
        self.model_override = model_override
        self.use_fast_model = use_fast_model
        self.fast_model = fast_model

        # 대화 히스토리 (컨텍스트 유지용)
        self._conversation_history: list[dict] = []
        self._max_history = 10  # 최대 히스토리 길이

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """에이전트의 시스템 프롬프트 (역할, 능력, 규칙 정의)"""
        pass

    @abstractmethod
    def run(self, task: AgentTask) -> AgentResult:
        """
        태스크 실행 - 각 에이전트가 구현
        Args:
            task: 수행할 태스크
        Returns: 실행 결과
        """
        pass

    def generate(
        self,
        prompt: str,
        system_prompt_override: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        images: Optional[list[str]] = None,
    ) -> str:
        """
        LLM 텍스트 생성 헬퍼 메서드
        Args:
            prompt: 사용자 프롬프트
            system_prompt_override: 시스템 프롬프트 오버라이드
            temperature: 온도 오버라이드
            max_tokens: 최대 토큰 수 오버라이드
            images: 이미지 파일 경로 목록 (비전 모델용)
        Returns: 생성된 텍스트
        """
        model = self._select_model()
        system = system_prompt_override or self.system_prompt

        request = GenerateRequest(
            prompt=prompt,
            system_prompt=system,
            model_override=model,
            temperature_override=temperature,
            max_tokens_override=max_tokens,
            images=images or [],
        )

        self.logger.debug(f"LLM 호출 (model={model or 'default'})")
        start = time.time()

        response = self.backend.generate_with_retry(request)

        duration = (time.time() - start) * 1000
        self.logger.debug(f"LLM 응답 ({duration:.0f}ms, {len(response.content)} 문자)")

        if not response.success:
            self.logger.error(f"LLM 생성 실패: {response.error}")
            return f"[오류] LLM 생성 실패: {response.error}"

        return response.content

    def generate_stream(
        self,
        prompt: str,
        system_prompt_override: Optional[str] = None,
    ) -> Iterator[str]:
        """스트리밍 텍스트 생성"""
        model = self._select_model()
        system = system_prompt_override or self.system_prompt

        request = GenerateRequest(
            prompt=prompt,
            system_prompt=system,
            model_override=model,
            stream=True,
        )

        yield from self.backend.generate_stream(request)

    def _select_model(self) -> Optional[str]:
        """사용할 모델 선택 로직"""
        if self.model_override:
            return self.model_override
        if self.use_fast_model and self.fast_model:
            return self.fast_model
        return None  # 백엔드 기본 모델 사용

    def _success_result(
        self,
        task: AgentTask,
        content: str,
        artifacts: Optional[list[Artifact]] = None,
        duration_ms: float = 0.0,
    ) -> AgentResult:
        """성공 결과 생성 헬퍼"""
        return AgentResult(
            task_id=task.task_id,
            agent_name=self.name,
            agent_role=self.role,
            content=content,
            artifacts=artifacts or [],
            success=True,
            duration_ms=duration_ms,
        )

    def _error_result(
        self,
        task: AgentTask,
        error: str,
        duration_ms: float = 0.0,
    ) -> AgentResult:
        """오류 결과 생성 헬퍼"""
        self.logger.error(f"태스크 실패: {error}")
        return AgentResult(
            task_id=task.task_id,
            agent_name=self.name,
            agent_role=self.role,
            content=f"태스크 실패: {error}",
            success=False,
            error=error,
            duration_ms=duration_ms,
        )

    def _timed_run(self, task: AgentTask) -> AgentResult:
        """타이머가 포함된 run() 래퍼"""
        start = time.time()
        self.logger.task_start(task.task_id, task.description[:80])
        try:
            result = self.run(task)
            result.duration_ms = (time.time() - start) * 1000
            self.logger.task_end(task.task_id, result.success, result.duration_ms)
            return result
        except Exception as e:
            duration_ms = (time.time() - start) * 1000
            self.logger.error(f"에이전트 실행 중 예외 발생: {e}")
            return self._error_result(task, str(e), duration_ms)

    def _format_context(self, task: AgentTask) -> str:
        """태스크 컨텍스트를 프롬프트용 문자열로 포맷"""
        parts = [f"태스크: {task.description}"]

        if task.context:
            parts.append("\n컨텍스트:")
            for key, value in task.context.items():
                if isinstance(value, str):
                    parts.append(f"  {key}: {value[:500]}")
                else:
                    import json
                    parts.append(f"  {key}: {json.dumps(value, ensure_ascii=False)[:300]}")

        if task.files:
            parts.append("\n관련 파일:")
            for f in task.files[:5]:
                parts.append(f"  - {f}")

        return "\n".join(parts)

    def _read_file_for_context(self, file_path: str, max_chars: int = 3000) -> str:
        """
        파일을 읽어 컨텍스트에 포함할 문자열 반환
        파일이 너무 크면 앞부분만 포함합니다.
        """
        if not self.fs:
            return f"[파일시스템 도구 없음: {file_path}]"
        try:
            content = self.fs.read_file(file_path)
            if len(content) > max_chars:
                return content[:max_chars] + f"\n... [파일이 잘림: {len(content)} 문자 중 {max_chars} 문자만 표시]"
            return content
        except Exception as e:
            return f"[파일 읽기 실패: {file_path} - {e}]"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(role={self.role.value}, name={self.name})"
