"""
에이전트 간 메시지 타입 정의
모든 에이전트 통신은 이 모듈의 데이터 클래스를 통해 이루어집니다.
"""

from dataclasses import dataclass, field
from typing import Optional, Any
from enum import Enum
from datetime import datetime
import uuid


class AgentRole(str, Enum):
    """에이전트 역할 열거형"""
    MANAGER = "manager"
    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    RESEARCHER = "researcher"
    TESTER = "tester"
    DOCUMENT = "document"
    VISION = "vision"
    SYSTEM = "system"


class TaskStatus(str, Enum):
    """태스크 상태 열거형"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_REVISION = "needs_revision"


class MessageType(str, Enum):
    """메시지 유형 열거형"""
    TASK_ASSIGN = "task_assign"       # 태스크 할당
    TASK_RESULT = "task_result"       # 태스크 결과
    FEEDBACK = "feedback"             # 피드백/리뷰
    TOOL_CALL = "tool_call"           # 도구 호출 요청
    TOOL_RESULT = "tool_result"       # 도구 결과
    STATUS_UPDATE = "status_update"   # 상태 업데이트
    ERROR = "error"                   # 오류
    FINALIZE = "finalize"             # 최종화


@dataclass
class Artifact:
    """에이전트가 생성한 아티팩트 (파일, 코드, 문서 등)"""
    artifact_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    artifact_type: str = "text"  # text, code, file, image, report
    content: str = ""
    file_path: Optional[str] = None
    language: Optional[str] = None  # 코드인 경우 언어
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class AgentTask:
    """에이전트에게 할당되는 태스크"""
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    description: str = ""
    assigned_to: AgentRole = AgentRole.MANAGER
    assigned_by: AgentRole = AgentRole.SYSTEM
    priority: int = 5  # 1(낮음) ~ 10(높음)
    context: dict = field(default_factory=dict)
    files: list[str] = field(default_factory=list)  # 관련 파일 경로
    dependencies: list[str] = field(default_factory=list)  # 선행 태스크 ID
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    """에이전트가 반환하는 결과"""
    result_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    task_id: str = ""
    agent_name: str = ""
    agent_role: AgentRole = AgentRole.SYSTEM
    content: str = ""
    artifacts: list[Artifact] = field(default_factory=list)
    success: bool = True
    error: str = ""
    tokens_used: int = 0
    duration_ms: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentMessage:
    """에이전트 간 통신 메시지"""
    message_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    message_type: MessageType = MessageType.STATUS_UPDATE
    sender: AgentRole = AgentRole.SYSTEM
    receiver: AgentRole = AgentRole.MANAGER
    content: str = ""
    payload: Optional[Any] = None  # AgentTask 또는 AgentResult
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ProjectPlan:
    """플래너가 생성하는 프로젝트 계획"""
    plan_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    title: str = ""
    objective: str = ""
    steps: list[dict] = field(default_factory=list)
    estimated_complexity: str = "medium"  # low, medium, high
    required_agents: list[AgentRole] = field(default_factory=list)
    artifacts_expected: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class OrchestrationState:
    """오케스트레이션 엔진의 현재 상태"""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: Optional[str] = None
    original_task: str = ""
    current_plan: Optional[ProjectPlan] = None
    tasks: list[AgentTask] = field(default_factory=list)
    results: list[AgentResult] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)
    iteration: int = 0
    status: TaskStatus = TaskStatus.PENDING
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None
    final_output: str = ""
    metadata: dict = field(default_factory=dict)

    def add_result(self, result: AgentResult) -> None:
        """결과 추가"""
        self.results.append(result)

    def add_message(self, message: AgentMessage) -> None:
        """메시지 추가"""
        self.messages.append(message)

    def get_results_by_role(self, role: AgentRole) -> list[AgentResult]:
        """특정 역할의 결과만 필터링"""
        return [r for r in self.results if r.agent_role == role]

    def get_context_summary(self) -> str:
        """현재까지의 컨텍스트 요약 생성"""
        lines = [f"작업: {self.original_task}", ""]
        if self.current_plan:
            lines.append(f"계획: {self.current_plan.title}")
            for i, step in enumerate(self.current_plan.steps, 1):
                lines.append(f"  {i}. {step.get('title', step.get('description', ''))}")
            lines.append("")

        # 최근 결과 요약
        for result in self.results[-5:]:  # 최근 5개 결과만
            if result.success:
                lines.append(f"[{result.agent_role.value}] {result.content[:200]}...")
            else:
                lines.append(f"[{result.agent_role.value}] 오류: {result.error}")

        return "\n".join(lines)
