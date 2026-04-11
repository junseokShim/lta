"""
Multi-agent orchestration engine.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .messages import (
    AgentRole,
    AgentTask,
    AgentResult,
    Artifact,
    OrchestrationState,
    TaskStatus,
)
from ..agents.base import AgentBase
from ..agents.coder import CoderAgent
from ..agents.document_agent import DocumentAgent
from ..agents.manager import ManagerAgent
from ..agents.planner import PlannerAgent
from ..agents.researcher import ResearcherAgent
from ..agents.reviewer import ReviewerAgent
from ..agents.tester import TesterAgent
from ..agents.vision_agent import VisionAgent
from ..logging_utils import get_logger
from ..memory.task_history import TaskHistory, TaskRecord
from ..workspace.manager import WorkspaceManager
from ..retry_policy import RetryPolicy, STEP_RETRY_POLICY, classify_error


logger = get_logger("orchestration.engine")


class OrchestrationEngine:
    """Coordinates planning, implementation, review, validation, and reporting."""

    RESULT_OFFLOAD_THRESHOLD = 4000
    ARTIFACT_OFFLOAD_THRESHOLD = 4000

    def __init__(
        self,
        manager: ManagerAgent,
        planner: PlannerAgent,
        coder: CoderAgent,
        reviewer: ReviewerAgent,
        researcher: ResearcherAgent,
        tester: Optional[TesterAgent] = None,
        document_agent: Optional[DocumentAgent] = None,
        vision_agent: Optional[VisionAgent] = None,
        workspace_manager: Optional[WorkspaceManager] = None,
        task_history: Optional[TaskHistory] = None,
        max_iterations: int = 5,
        on_status_update: Optional[Callable[[str, str], None]] = None,
    ):
        self.manager = manager
        self.planner = planner
        self.coder = coder
        self.reviewer = reviewer
        self.researcher = researcher
        self.tester = tester
        self.document_agent = document_agent
        self.vision_agent = vision_agent

        self.workspace = workspace_manager
        self.history = task_history or TaskHistory()
        self.max_iterations = max_iterations
        self._on_status = on_status_update
        self._current_state: Optional[OrchestrationState] = None

    def bind_project_root(self, project_id: Optional[str] = None) -> Optional[Path]:
        """Bind tools and retrieval to the current project root."""
        if not self.workspace:
            return None

        try:
            if self.workspace.is_attached_mode():
                project_root = self.workspace.workspace_root
            elif project_id:
                project_root = self.workspace.get_project_path(project_id)
            else:
                return None
        except Exception as exc:
            logger.warning("Project root binding failed: %s", exc)
            return None

        for agent in [
            self.manager,
            self.planner,
            self.coder,
            self.reviewer,
            self.researcher,
            self.tester,
            self.document_agent,
            self.vision_agent,
        ]:
            if not agent:
                continue
            for tool_attr in ["fs", "doc", "img", "shell"]:
                tool = getattr(agent, tool_attr, None)
                if tool and hasattr(tool, "set_workspace_root"):
                    tool.set_workspace_root(str(project_root))

        if self.researcher and self.researcher.retrieval:
            self.researcher.retrieval.set_workspace_root(str(project_root), clear_index=True)

        return project_root

    def _ensure_project_context(self, project_id: Optional[str], task: str = "") -> Optional[str]:
        """Ensure an attached workspace has a stable project_id for context-aware commands."""
        if not self.workspace:
            return project_id
        if project_id:
            return project_id
        if self.workspace.is_attached_mode():
            projects = self.workspace.list_projects()
            if projects:
                return projects[0].project_id
            return self.workspace.create_project(self.workspace.workspace_root.name, task).project_id
        return None

    def _get_project_guidance(self, project_id: Optional[str]) -> str:
        """Collect project instruction files such as AGENTS.md, CLAUDE.md, and README."""
        if not self.workspace or not project_id:
            return ""
        try:
            return self.workspace.get_project_guidance(project_id)
        except Exception as exc:
            logger.debug("Project guidance collection failed: %s", exc)
            return ""

    def _get_git_context(self) -> str:
        """Return a short git status summary for the current project, when available."""
        shell_tool = getattr(self.manager, "shell", None)
        if not shell_tool or not hasattr(shell_tool, "get_git_status"):
            return ""
        try:
            return shell_tool.get_git_status()
        except Exception as exc:
            logger.debug("Git context collection failed: %s", exc)
            return ""

    def _format_additional_context(self, additional_context: Optional[dict]) -> str:
        """Render additional conversation/session context into compact text."""
        if not additional_context:
            return ""
        parts = []
        for key, value in additional_context.items():
            parts.append(f"### {key}\n{str(value)[:1500]}")
        return "\n\n".join(parts)

    def _list_visible_project_files(self, project_root: Path, limit: int = 200) -> list[str]:
        """List user-facing project files while skipping workspace metadata folders."""
        skip_dirs = {
            ".git",
            ".lta",
            "__pycache__",
            ".pytest_cache",
            "artifacts",
            "logs",
            "reports",
            "cache",
            "inputs",
            "venv",
        }
        skip_names = {".project.json", ".history.db"}

        files: list[str] = []
        for path in sorted(project_root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(project_root)
            if any(part in skip_dirs for part in relative.parts):
                continue
            if path.name in skip_names:
                continue
            files.append(relative.as_posix())
            if len(files) >= limit:
                break
        return files

    def _normalize_task_analysis(
        self,
        state: OrchestrationState,
        user_task: str,
        task_analysis: Optional[dict],
    ) -> dict:
        """Apply deterministic safeguards so project-generation validation is not left to the model alone."""
        normalized = dict(task_analysis or {})
        lowered = (user_task or "").lower()
        initial_files = state.metadata.get("initial_visible_files") or []

        create_keywords = [
            "create",
            "build",
            "generate",
            "make",
            "new",
            "scaffold",
            "start",
            "만들",
            "생성",
            "작성",
            "구축",
        ]
        project_keywords = [
            "project",
            "app",
            "application",
            "api",
            "cli",
            "tool",
            "script",
            "service",
            "package",
            "program",
            "프로젝트",
            "앱",
            "애플리케이션",
            "도구",
            "스크립트",
            "서비스",
            "패키지",
        ]
        python_keywords = ["python", "파이썬"]

        looks_like_python = any(keyword in lowered for keyword in python_keywords)
        looks_like_new_build = any(keyword in lowered for keyword in create_keywords) and any(
            keyword in lowered for keyword in project_keywords
        )
        is_new_python_project = looks_like_python and (looks_like_new_build or not initial_files)

        required_agents = list(normalized.get("required_agents") or [])
        if is_new_python_project:
            required_agents.extend(["planner", "coder", "reviewer", "tester", "document"])
            normalized["task_type"] = "code"
            normalized["needs_file_access"] = True
            normalized["needs_code_execution"] = True

        normalized["required_agents"] = sorted(set(required_agents))
        normalized["project_generation"] = {
            "is_new_project": is_new_python_project,
            "language": "python" if is_new_python_project else normalized.get("project_generation", {}).get("language"),
            "requires_entrypoint": is_new_python_project,
            "initial_visible_file_count": len(initial_files),
        }
        return normalized

    def _is_python_project_workflow(self, state: OrchestrationState) -> bool:
        analysis = state.metadata.get("task_analysis") or {}
        profile = analysis.get("project_generation") or {}
        if profile.get("is_new_project") and profile.get("language") == "python":
            return True

        # ── 개선: 초기 파일이 있어도 에이전트가 새 Python 파일을 생성했으면 Python 워크플로우 ──
        # 이전 코드: `not initial_files` 조건 때문에 config.py만 있는 워크스페이스에서
        # Python 프로젝트 생성 작업을 해도 검증 루프가 실행되지 않는 버그를 수정한다.
        initial_files = state.metadata.get("initial_visible_files") or []
        initial_py_set = set(f for f in initial_files if f.endswith(".py"))
        changed_files = self._collect_changed_files_from_artifacts(state)
        # 에이전트가 생성한 새 Python 파일이 있으면 Python 워크플로우로 판단
        new_py_files = [f for f in changed_files if f.endswith(".py") and f not in initial_py_set]
        return bool(new_py_files)

    def _store_result(self, state: OrchestrationState, result: AgentResult) -> None:
        """상태에 결과를 추가하고, 큰 본문은 디스크 캐시로 오프로드한다."""
        state.add_result(result)
        self._offload_result_payloads(state, result)

    def _offload_result_payloads(self, state: OrchestrationState, result: AgentResult) -> None:
        """큰 result/artifact 본문을 디스크에 저장하고 메모리에는 요약만 남긴다."""
        if not self.workspace or not state.project_id:
            return

        if result.content and len(result.content) > self.RESULT_OFFLOAD_THRESHOLD:
            cache_name = f"results/{result.result_id}_{result.agent_role.value}.txt"
            self.workspace.save_cache_text(state.project_id, cache_name, result.content)
            result.metadata["content_cache_path"] = cache_name
            result.metadata["content_preview"] = result.content[:1200]
            result.metadata["content_offloaded"] = True
            result.content = result.content[:1200] + "\n\n[full result offloaded to disk cache]"

        for artifact in result.artifacts:
            if not artifact.content or len(artifact.content) <= self.ARTIFACT_OFFLOAD_THRESHOLD:
                continue
            safe_name = (artifact.name or artifact.artifact_id or "artifact").replace("\\", "_").replace("/", "_")
            cache_name = f"artifacts/{artifact.artifact_id}_{safe_name}.txt"
            self.workspace.save_cache_text(state.project_id, cache_name, artifact.content)
            artifact.metadata["content_cache_path"] = cache_name
            artifact.metadata["content_preview"] = artifact.content[:600]
            artifact.metadata["content_offloaded"] = True
            artifact.content = ""

    def _save_step_artifacts(
        self,
        state: OrchestrationState,
        result: AgentResult,
    ) -> list[str]:
        """[A] 태스크 완료 즉시 아티팩트를 워크스페이스 디스크에 저장한다.

        ─── 증분 아티팩트 지속성 ─────────────────────────────────────────
        태스크가 완료될 때마다 호출되어 해당 태스크의 산출물을 즉시 파일로 저장한다.
        "메모리에만 완료된 태스크"는 완료된 것이 아니다 — 파일이 디스크에 존재해야
        다른 에이전트가 읽고 의존할 수 있다.

        반환값: 저장된 파일들의 프로젝트 루트 기준 상대 경로 목록
        ────────────────────────────────────────────────────────────────
        """
        if not self.workspace or not state.project_id:
            return []

        saved_paths: list[str] = []
        for artifact in result.artifacts:
            # 이미 저장된 아티팩트는 건너뜀 (중복 저장 방지)
            if artifact.persisted:
                continue
            if not self._should_persist_artifact(state, result, artifact):
                continue

            artifact_content = self._hydrate_artifact_content(state, artifact)
            if not artifact_content:
                logger.warning(
                    "[즉시 저장 건너뜀] %s — 빈 콘텐츠: %s (캐시 경로: %s)",
                    result.agent_name,
                    artifact.name,
                    artifact.metadata.get("content_cache_path", "없음"),
                )
                continue

            target_path = self._artifact_target_path(artifact)
            try:
                if target_path:
                    saved_path = self.workspace.save_project_file(
                        state.project_id, target_path, artifact_content
                    )
                else:
                    filename = artifact.name or f"artifact_{artifact.artifact_id}.txt"
                    subfolder = "reports" if filename.endswith((".md", ".txt", ".rst")) else "artifacts"
                    saved_path = self.workspace.save_artifact(
                        state.project_id, filename, artifact_content, subfolder=subfolder
                    )

                # 저장 완료 후 아티팩트 상태 업데이트
                relative = self._relative_saved_path(state.project_id, saved_path)
                artifact.file_path = relative
                artifact.saved_path = relative
                artifact.persisted = True  # 다음 _save_artifacts 호출 시 중복 저장 방지
                saved_paths.append(relative)

                # ── [E] 에이전트 간 통신: 저장된 파일 레지스트리 갱신 ───────
                # OrchestrationState.get_context_summary() 가 이 레지스트리를 읽어
                # 하위 에이전트의 프롬프트에 자동으로 삽입한다.
                if "saved_artifacts" not in state.metadata:
                    state.metadata["saved_artifacts"] = {}
                state.metadata["saved_artifacts"][relative] = {
                    "agent": result.agent_name,
                    "role": result.agent_role.value,
                    "task_id": result.task_id,
                    "saved_at": datetime.now().isoformat(),
                }

                # ── [팀 에이전트 핸드오프 검증] 저장 직후 디스크 존재 확인 ──
                # 코더가 아티팩트를 생성했다고 보고해도 실제 파일이 없으면
                # 다음 에이전트(테스터, 리뷰어)가 읽지 못한다.
                saved_path_obj = Path(saved_path)
                if saved_path_obj.exists():
                    file_size = saved_path_obj.stat().st_size
                    logger.info(
                        "[즉시 저장 + 핸드오프 확인] %s → %s (%d bytes, lang=%s)",
                        result.agent_name,
                        relative,
                        file_size,
                        artifact.language or "?",
                    )
                else:
                    logger.error(
                        "[핸드오프 검증 실패] 저장 직후 파일이 존재하지 않음: %s (workspace.save_project_file 반환값: %s)",
                        relative,
                        saved_path,
                    )

            except Exception as exc:
                logger.error(
                    "아티팩트 즉시 저장 실패 [%s] %s: %s",
                    result.agent_name,
                    target_path or artifact.name,
                    exc,
                    exc_info=True,
                )

        return saved_paths

    def _hydrate_result_content(self, state: OrchestrationState, result: AgentResult) -> str:
        """오프로드된 결과 본문이 있으면 디스크에서 다시 읽는다."""
        cache_path = result.metadata.get("content_cache_path")
        if cache_path and self.workspace and state.project_id:
            try:
                return self.workspace.load_cache_text(state.project_id, cache_path)
            except Exception as exc:
                logger.debug("Result cache hydrate failed: %s", exc)
        return result.metadata.get("content_preview") or result.content

    def _hydrate_artifact_content(self, state: OrchestrationState, artifact: Artifact) -> str:
        """오프로드된 아티팩트 본문이 있으면 디스크에서 다시 읽는다."""
        if artifact.content:
            return artifact.content

        cache_path = artifact.metadata.get("content_cache_path")
        if cache_path and self.workspace and state.project_id:
            try:
                restored = self.workspace.load_cache_text(state.project_id, cache_path)
                artifact.content = restored
                return restored
            except Exception as exc:
                logger.debug("Artifact cache hydrate failed: %s", exc)
        return artifact.metadata.get("content_preview", "")

    def run(
        self,
        user_task: str,
        project_id: Optional[str] = None,
        additional_context: Optional[dict] = None,
    ) -> OrchestrationState:
        """Run the full multi-agent workflow.

        ─── 재빌드 루프 ────────────────────────────────────────────────────
        검증 실패(phase=entrypoint, 실행 오류 등)가 발생하면 최대
        MAX_REBUILD_CYCLES 번까지 전체 계획-실행-검증 사이클을 재시작한다.
        재시작은 맹목적 재시도가 아니라 이전 실패 정보를 컨텍스트에 포함하여
        에이전트가 다른 접근법을 취하도록 유도한다.
        ────────────────────────────────────────────────────────────────────
        """
        # 최대 재빌드 사이클 (1 = 재시작 없음, 3 = 2회 재시작 허용)
        # config.py만 있는 워크스페이스에서: 1차 생성 실패 → 2차 재시도 → 3차 재시도
        MAX_REBUILD_CYCLES = 3
        start_time = time.time()

        # 워크스페이스 바인딩은 사이클 간 공유하기 위해 바깥에서 처리
        shared_project_id = project_id
        prior_failure_context: Optional[dict] = None
        final_state: Optional[OrchestrationState] = None

        for rebuild_cycle in range(1, MAX_REBUILD_CYCLES + 1):
            state = self._run_impl(
                user_task=user_task,
                project_id=shared_project_id,
                additional_context=additional_context,
                rebuild_cycle=rebuild_cycle,
                prior_failure=prior_failure_context,
                cycle_start_time=start_time if rebuild_cycle == 1 else time.time(),
            )
            final_state = state

            # 성공 → 종료
            if state.status == TaskStatus.COMPLETED:
                break

            # ── 재빌드 가능 여부 판단 ─────────────────────────────────────
            # blocking=True 인 검증 실패만 재시작 대상이다.
            # 그 외 오류(LLM 오류, 파싱 오류 등)는 재시작해도 의미가 없다.
            validation = state.metadata.get("project_validation") or {}
            if not validation.get("blocking"):
                logger.info("검증 실패 외 원인이므로 재빌드를 건너뜁니다.")
                break
            if rebuild_cycle >= MAX_REBUILD_CYCLES:
                logger.warning("최대 재빌드 사이클(%d)에 도달했습니다. 최종 실패 상태를 반환합니다.", MAX_REBUILD_CYCLES)
                break

            # 다음 사이클을 위해 실패 컨텍스트 보존
            shared_project_id = state.project_id  # 같은 워크스페이스 재사용
            prior_failure_context = {
                "cycle": rebuild_cycle,
                "failure_summary": validation.get("failure_summary", ""),
                "attempt_logs": validation.get("attempt_logs", []),
                "entrypoint": validation.get("entrypoint"),
            }
            self._notify(
                f"[재빌드 {rebuild_cycle + 1}/{MAX_REBUILD_CYCLES}] "
                f"검증 실패로 전체 워크플로우를 재시작합니다: "
                f"{prior_failure_context['failure_summary'][:200]}",
                "manager",
            )

        return final_state

    def _run_impl(
        self,
        user_task: str,
        project_id: Optional[str] = None,
        additional_context: Optional[dict] = None,
        rebuild_cycle: int = 1,
        prior_failure: Optional[dict] = None,
        cycle_start_time: Optional[float] = None,
    ) -> OrchestrationState:
        """내부 단일 오케스트레이션 사이클.

        ─── 핵심 개선 사항 ─────────────────────────────────────────────────
        A. 증분 아티팩트 지속성:
           - 각 태스크 완료 즉시 _save_step_artifacts() 로 디스크에 저장
           - 이후 에이전트는 해당 파일을 즉시 읽거나 의존할 수 있음
        E. 에이전트 간 통신:
           - OrchestrationState.get_context_summary() 에 저장된 파일 목록 포함
           - _execute_step() 프롬프트에 현재 워크스페이스 상태 삽입
        D. 실패 전파:
           - prior_failure 가 있으면 계획 단계부터 실패 컨텍스트를 주입
        ────────────────────────────────────────────────────────────────────
        """
        start_time = cycle_start_time or time.time()
        state = OrchestrationState(
            original_task=user_task,
            status=TaskStatus.IN_PROGRESS,
        )
        self._current_state = state

        # 재빌드 사이클 메타데이터 기록
        if rebuild_cycle > 1:
            state.metadata["rebuild_cycle"] = rebuild_cycle

        if project_id and self.workspace:
            try:
                project_meta = self.workspace.load_project(project_id)
                state.project_id = project_id
                self._notify(f"프로젝트 로드: {project_meta.name}", "info")
            except FileNotFoundError:
                state.project_id = self._create_project(user_task)
        elif self.workspace:
            state.project_id = self._create_project(user_task)

        if self.workspace:
            bound_root = self.bind_project_root(state.project_id)
            if bound_root:
                state.metadata["project_root"] = str(bound_root)
                state.metadata["workspace_mode"] = "attached" if self.workspace.is_attached_mode() else "managed"
                state.metadata["initial_visible_files"] = self._list_visible_project_files(bound_root, limit=200)

        if additional_context:
            state.metadata["additional_context"] = additional_context

        # 이전 실패 컨텍스트를 state에 주입하여 에이전트가 참조할 수 있게 한다.
        if prior_failure:
            state.metadata["prior_failure"] = prior_failure

        try:
            self._notify("매니저가 작업을 분석 중입니다...", "manager")
            project_guidance = self._get_project_guidance(state.project_id)
            if project_guidance:
                state.metadata["project_guidance"] = project_guidance

            git_context = self._get_git_context()
            if git_context:
                state.metadata["git_status"] = git_context

            workspace_ctx = self._get_workspace_context(
                state.project_id,
                additional_context=additional_context,
                project_guidance=project_guidance,
                git_context=git_context,
            )
            if workspace_ctx:
                state.metadata["workspace_context"] = workspace_ctx

            task_analysis = self.manager.analyze_task(user_task, workspace_ctx)
            task_analysis = self._normalize_task_analysis(state, user_task, task_analysis)
            state.metadata["task_analysis"] = task_analysis
            self._log_step(state, "task_analysis", str(task_analysis))

            python_project_workflow = self._is_python_project_workflow(state)

            self._notify("플래너가 실행 계획을 수립 중입니다...", "planner")
            plan_context = {"task_analysis": str(task_analysis)}
            if additional_context:
                plan_context.update(additional_context)
            # ── 재빌드 시: 이전 실패 정보를 플래너에게 전달 ─────────────────
            if prior_failure:
                plan_context["prior_failure_summary"] = (
                    f"[재빌드 사이클 {prior_failure.get('cycle', '?')}]\n"
                    f"이전 시도 실패: {prior_failure.get('failure_summary', '')[:800]}\n"
                    f"엔트리포인트: {prior_failure.get('entrypoint') or '미발견'}"
                )

            plan = self.planner.create_plan(user_task, plan_context)
            state.current_plan = plan
            self._notify(f"계획 수립 완료: {len(plan.steps)}단계", "planner")
            self._log_step(state, "planning", self.planner.format_plan_for_display(plan))

            if task_analysis.get("needs_file_access", True):
                self._notify("리서처가 관련 맥락을 수집 중입니다...", "researcher")
                self._store_result(state, self._run_researcher(state, user_task, task_analysis))

            if task_analysis.get("needs_image_analysis") and self.vision_agent:
                self._notify("비전 에이전트가 이미지를 분석 중입니다...", "vision")
                vision_result = self._run_vision_agent(state, task_analysis)
                if vision_result:
                    self._store_result(state, vision_result)

            for step in plan.steps:
                if state.status == TaskStatus.CANCELLED:
                    break

                agent_name = step.get("assigned_agent", "coder")
                step_num = step.get("step_num", "?")
                step_title = step.get("title", step.get("description", ""))
                self._notify(f"단계 {step_num}: {step_title} [{agent_name}]", agent_name)

                # ──────────────────────────────────────────────────────────
                # 스텝 재시도 루프 (Step Retry Loop)
                # 일시적 실패는 STEP_RETRY_POLICY 에 따라 재시도합니다.
                # 치명적 오류 또는 최대 시도 초과 시에만 전체 오케스트레이션을 중단합니다.
                # ──────────────────────────────────────────────────────────
                step_result = self._execute_step_with_retry(state, step, STEP_RETRY_POLICY)

                # ── [A] 태스크 완료 즉시 아티팩트 저장 ────────────────────
                # 태스크가 성공적으로 완료되는 즉시 디스크에 저장한다.
                # 메모리에만 존재하는 결과물은 완료된 태스크가 아니다.
                # 저장된 파일은 이후 에이전트의 컨텍스트(get_context_summary)에 반영된다.
                if self.workspace and state.project_id and step_result.success:
                    self._prepare_generated_project_artifacts(state)
                    saved_now = self._save_step_artifacts(state, step_result)
                    if saved_now:
                        self._notify(
                            f"[즉시 저장] {len(saved_now)}개 파일: {', '.join(saved_now[:3])}",
                            "system",
                        )

            code_results = state.get_results_by_role(AgentRole.CODER)
            if code_results:
                self._notify("리뷰어가 코드를 점검 중입니다...", "reviewer")
                review_result = self._run_reviewer(state, code_results)
                self._store_result(state, review_result)
                state.metadata["review"] = review_result.content

            if self.tester and code_results and task_analysis.get("needs_code_execution") and not python_project_workflow:
                self._notify("테스터가 테스트 코드를 작성 중입니다...", "tester")
                self._store_result(state, self._run_tester(state, code_results))

            if self.document_agent and task_analysis.get("task_type") in ["code", "mixed"] and not python_project_workflow:
                self._notify("문서 에이전트가 문서를 작성 중입니다...", "document")
                self._store_result(state, self._run_document_agent(state, user_task, code_results))

            if self.workspace and state.project_id:
                # _prepare + _save_artifacts: 아직 저장되지 않은 나머지 아티팩트를 최종 정리한다.
                # (증분 저장에서 처리되지 않은 리뷰어/문서 에이전트 산출물 등)
                self._prepare_generated_project_artifacts(state)
                self._save_artifacts(state)

                if python_project_workflow:
                    verification_result = self._run_python_project_validation_loop(state)
                else:
                    verification_result = self._run_post_change_validation(state)
                if verification_result:
                    self._store_result(state, verification_result)
                self._enforce_completion_criteria(state)

                if python_project_workflow and self.document_agent and task_analysis.get("task_type") in ["code", "mixed"]:
                    self._notify("문서 에이전트가 실행 검증을 마친 뒤 README를 정리 중입니다...", "document")
                    refreshed_code_results = state.get_results_by_role(AgentRole.CODER)
                    doc_result = self._run_document_agent(state, user_task, refreshed_code_results)
                    self._store_result(state, doc_result)
                    self._save_step_artifacts(state, doc_result)
                    self._save_artifacts(state)

            self._notify("매니저가 결과를 통합 중입니다...", "manager")
            state.final_output = self.manager.synthesize_results(
                user_task,
                state.results,
                state.project_id,
            )

            if self.workspace and state.project_id:
                self.workspace.save_report(state.project_id, state.final_output, "final_output.md")
                self._save_session_log(state)

            state.status = TaskStatus.COMPLETED
            state.completed_at = datetime.now().isoformat()
            self._notify(f"작업 완료 ({time.time() - start_time:.1f}초)", "system")

        except Exception as exc:
            logger.error("Orchestration error: %s", exc, exc_info=True)
            state.status = TaskStatus.FAILED
            state.final_output = (
                f"[FAILED] 실행이 완료되지 않았습니다 — 프로젝트 폴더에 실제 구현 파일이 생성되지 않았습니다.\n\n"
                f"실패 원인: {exc}"
            )
            self._notify(f"오류: {exc}", "error")

            # ── [D] 스텝 실패(RuntimeError 포함)도 재빌드 루프가 처리하도록 한다 ──
            # project_validation 이 설정되지 않으면 run() 의 재빌드 판단에서
            # blocking=False 로 간주되어 재빌드가 건너뛰어진다.
            # 스텝 실행 실패(코더 5회 실패 등)도 재빌드 가능 실패로 표시한다.
            if not state.metadata.get("project_validation"):
                state.metadata["project_validation"] = {
                    "success": False,
                    "blocking": True,
                    "failure_summary": str(exc)[:600],
                    "attempt_logs": [],
                }

        return state

    def run_quick(
        self,
        task: str,
        agent_role: str = "coder",
        additional_context: Optional[dict] = None,
    ) -> str:
        """Run a single agent with stronger project context."""
        agent = self._get_agent(agent_role)
        if not agent:
            return f"에이전트 '{agent_role}'를 찾을 수 없습니다."

        context_project_id = self._ensure_project_context(None, task)
        if context_project_id:
            self.bind_project_root(context_project_id)

        workspace_ctx = self._get_workspace_context(context_project_id, additional_context=additional_context)
        relevant_files = []
        if self.researcher:
            try:
                relevant_files = self.researcher.find_relevant_files(task)
            except Exception as exc:
                logger.debug("Quick relevant file lookup failed: %s", exc)

        prompt_parts = [task]
        if workspace_ctx:
            prompt_parts.append("\nProject context:\n" + workspace_ctx[:3000])
        if relevant_files:
            prompt_parts.append("\nRelevant files:\n" + "\n".join(f"- {path}" for path in relevant_files[:8]))

        return agent.generate("\n".join(prompt_parts))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 경량 채팅 모드 (Chat Mode) — 워크스페이스 인식 직접 LLM 호출
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 파일/폴더 참조를 감지하기 위한 정규식 패턴
    # 확장자가 있는 파일명, '/'로 끝나는 경로, 공통 디렉토리명을 감지한다.
    _CHAT_PATH_PATTERN = None  # 지연 컴파일 (모듈 로드 최적화)

    _COMMON_DIR_NAMES = {
        # 일반적인 프로젝트 디렉토리 이름
        "src", "data", "tests", "test", "docs", "doc", "config",
        "scripts", "notebooks", "models", "output", "outputs",
        "logs", "log", "assets", "static", "templates", "resources",
        "lib", "libs", "utils", "tools", "bin", "build", "dist",
        # 한국어 키워드 → 영어 디렉토리명으로 매핑은 불가능하므로
        # 메시지 내 '데이터 폴더', '소스 코드' 등은 별도 처리
    }

    def _get_active_workspace_root_for_chat(self) -> Optional[Path]:
        """
        채팅 모드에서 사용할 워크스페이스 루트를 결정합니다.

        우선순위:
        1. 이미 bind_project_root()가 호출된 경우 researcher.fs.workspace_root 사용
        2. attached 모드인 경우 workspace.workspace_root 사용
        3. 접근 불가: None 반환
        """
        # 1순위: researcher 에 이미 바인딩된 fs 사용 (가장 정확)
        if self.researcher and self.researcher.fs:
            root = self.researcher.fs.workspace_root
            if root and root.exists():
                return root

        # 2순위: workspace manager의 attached 모드 루트
        if self.workspace:
            if self.workspace.is_attached_mode():
                root = self.workspace.workspace_root
                if root and root.exists():
                    return root
            # 관리형 워크스페이스: 첫 번째 프로젝트 경로 사용
            try:
                projects = self.workspace.list_projects()
                if projects:
                    proj_path = self.workspace.get_project_path(projects[0].project_id)
                    if proj_path.exists():
                        return proj_path
            except Exception:
                pass

        return None

    def _extract_path_hints_from_message(self, message: str) -> list[str]:
        """
        사용자 메시지에서 파일/폴더 참조를 추출합니다.

        감지 대상:
        - 확장자가 있는 파일명: README.md, config.py, data.csv
        - 슬래시로 구분된 경로: src/, data/raw/
        - 공통 디렉토리 키워드: data, src, tests, config
        - 한국어 경로 표현: '데이터 폴더' → 'data', '소스' → 'src'
        """
        import re
        hints = set()

        # 패턴 1: 확장자가 있는 파일명 (예: README.md, config.yaml, main.py)
        for m in re.finditer(
            r'\b([\w./\-]+\.(?:py|js|ts|md|txt|yaml|yml|json|csv|toml|ini|cfg|sh|html|css|rst|ipynb|r|sql|log))\b',
            message, re.IGNORECASE
        ):
            hints.add(m.group(1).strip('./'))

        # 패턴 2: 경로 형태 (예: data/, src/utils/, ./config)
        for m in re.finditer(r'\b([\w.\-]+/[\w./\-]*)\b', message):
            candidate = m.group(1).strip('/')
            if candidate:
                hints.add(candidate)
                # 최상위 디렉토리도 추가
                top = candidate.split('/')[0]
                if top:
                    hints.add(top)

        # 패턴 3: 공통 디렉토리 키워드 (단독 단어로 등장할 때)
        lower = message.lower()
        for dirname in self._COMMON_DIR_NAMES:
            # 단어 경계로 매칭 (예: "data folder", "in src", "the tests directory")
            if re.search(rf'\b{re.escape(dirname)}\b', lower):
                hints.add(dirname)

        # 패턴 4: 한국어 경로 표현 매핑
        ko_mappings = {
            "데이터": "data", "소스": "src", "테스트": "tests",
            "설정": "config", "문서": "docs", "스크립트": "scripts",
            "로그": "logs", "모델": "models", "출력": "output",
        }
        for ko, en in ko_mappings.items():
            if ko in message:
                hints.add(en)

        return list(hints)

    def _is_path_within_workspace(self, path_hint: str, workspace_root: Path) -> Optional[Path]:
        """
        경로 힌트가 워크스페이스 내에 실제로 존재하는지 확인합니다.

        워크스페이스 경계 보안:
        - 경로 탈출(path traversal) 공격 방지
        - 워크스페이스 외부 경로는 None 반환
        """
        try:
            candidate = (workspace_root / path_hint).resolve()
            # 워크스페이스 경계 검사 — 경로 탈출 방지
            if not str(candidate).startswith(str(workspace_root)):
                return None
            if candidate.exists():
                return candidate
        except Exception:
            pass
        return None

    def _read_workspace_item_for_chat(
        self,
        path: Path,
        workspace_root: Path,
        max_file_chars: int = 4000,
        max_dir_entries: int = 50,
    ) -> str:
        """
        워크스페이스 내 파일 또는 디렉토리를 읽어 LLM 컨텍스트용 텍스트를 반환합니다.

        - 파일: 내용을 읽어 반환 (크기 제한 적용)
        - 디렉토리: 항목 목록을 반환 (재귀 없음)
        - 이진 파일 등 읽기 불가 항목: 메타정보만 반환
        """
        rel_path = path.relative_to(workspace_root)

        if path.is_dir():
            # 디렉토리: 내부 항목 나열
            lines = [f"[디렉토리] {rel_path}/"]
            skip_dirs = {".git", "__pycache__", ".pytest_cache", "venv", ".lta", "node_modules"}
            entries = sorted(path.iterdir())
            count = 0
            for entry in entries:
                if count >= max_dir_entries:
                    lines.append(f"  ... ({len(entries) - max_dir_entries}개 더)")
                    break
                if entry.name in skip_dirs or entry.name.startswith("."):
                    continue
                size_info = ""
                if entry.is_file():
                    try:
                        size_info = f" ({entry.stat().st_size:,} bytes)"
                    except OSError:
                        pass
                    lines.append(f"  {entry.name}{size_info}")
                else:
                    lines.append(f"  {entry.name}/")
                count += 1
            return "\n".join(lines)

        if path.is_file():
            # 파일: 내용 읽기
            # 허용되는 텍스트 확장자 목록
            text_exts = {
                ".py", ".js", ".ts", ".md", ".txt", ".yaml", ".yml",
                ".json", ".csv", ".toml", ".ini", ".cfg", ".sh",
                ".html", ".css", ".rst", ".r", ".sql", ".log", ".ipynb",
            }
            ext = path.suffix.lower()
            if ext not in text_exts:
                size = path.stat().st_size if path.exists() else 0
                return f"[파일] {rel_path} ({size:,} bytes, 텍스트가 아닌 파일)"

            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                if len(content) > max_file_chars:
                    content = content[:max_file_chars] + f"\n... (총 {len(content):,}자 중 {max_file_chars:,}자 표시)"
                return f"[파일: {rel_path}]\n```{ext.lstrip('.')}\n{content}\n```"
            except Exception as exc:
                return f"[파일] {rel_path} (읽기 실패: {exc})"

        return f"[존재하지 않음] {rel_path}"

    def _gather_workspace_context_for_chat(
        self,
        message: str,
        workspace_root: Path,
        max_total_chars: int = 10000,
    ) -> str:
        """
        사용자 메시지를 분석하여 워크스페이스 내 관련 파일/폴더 내용을 수집합니다.

        단계:
        1. 메시지에서 파일/폴더 힌트 추출
        2. 워크스페이스 내 존재 여부 확인 (경계 검사 포함)
        3. 파일/디렉토리 내용 수집
        4. researcher.find_relevant_files()로 추가 관련 파일 탐색
        5. 워크스페이스 최상위 파일 목록 (항상 포함)
        6. 전체 컨텍스트 예산 내에서 반환

        워크스페이스 경계 규칙:
        - workspace_root 하위 경로만 접근 허용
        - 경로 탈출(../등) 시도는 자동 차단
        - 워크스페이스 외부 경로는 "접근 불가" 안내
        """
        parts: list[str] = []
        total_chars = 0

        def _add(text: str) -> bool:
            nonlocal total_chars
            if total_chars >= max_total_chars:
                return False
            parts.append(text)
            total_chars += len(text)
            return True

        # ── 1단계: 워크스페이스 최상위 구조 항상 포함 ──────────────────────
        top_items = []
        skip_top = {".git", "__pycache__", ".pytest_cache", "venv", ".lta", "node_modules", ".env"}
        try:
            for item in sorted(workspace_root.iterdir()):
                if item.name in skip_top or item.name.startswith("."):
                    continue
                suffix = "/" if item.is_dir() else ""
                top_items.append(f"  {item.name}{suffix}")
        except Exception:
            pass

        if top_items:
            ws_summary = (
                f"[워크스페이스: {workspace_root}]\n"
                + "\n".join(top_items[:40])
            )
            _add(ws_summary)

        # ── 2단계: 메시지에서 경로 힌트 추출 후 내용 수집 ─────────────────
        hints = self._extract_path_hints_from_message(message)
        found_paths: set[str] = set()

        for hint in hints:
            if total_chars >= max_total_chars:
                break
            resolved = self._is_path_within_workspace(hint, workspace_root)
            if resolved and str(resolved) not in found_paths:
                found_paths.add(str(resolved))
                item_text = self._read_workspace_item_for_chat(resolved, workspace_root)
                _add("\n" + item_text)

                # 디렉토리인 경우 내부 파일도 일부 읽기
                # 우선순위: README/설정 파일 → 데이터 파일(csv/json/txt) → 코드 파일
                if resolved.is_dir() and total_chars < max_total_chars:
                    # 1순위: 설명/설정 파일
                    priority_names = [
                        "README.md", "README.rst", "__init__.py", "main.py",
                        "config.py", "config.yaml", "config.yml", "settings.py",
                    ]
                    # 2순위: 디렉토리 내 모든 텍스트 파일 (소용량 우선)
                    text_exts = {".csv", ".json", ".txt", ".py", ".yaml", ".yml",
                                 ".md", ".toml", ".ini", ".cfg", ".log", ".r", ".sql"}
                    try:
                        dir_files = sorted(
                            (f for f in resolved.iterdir()
                             if f.is_file() and f.suffix.lower() in text_exts),
                            key=lambda f: f.stat().st_size  # 작은 파일 먼저
                        )
                    except Exception:
                        dir_files = []

                    # 먼저 priority 파일 읽기
                    for fname in priority_names:
                        fpath = resolved / fname
                        if fpath.exists() and str(fpath) not in found_paths:
                            found_paths.add(str(fpath))
                            sub_text = self._read_workspace_item_for_chat(
                                fpath, workspace_root, max_file_chars=1500
                            )
                            if not _add("\n" + sub_text):
                                break

                    # 나머지 텍스트 파일도 읽기 (최대 5개, 각 1000자)
                    read_count = 0
                    for fpath in dir_files:
                        if total_chars >= max_total_chars or read_count >= 5:
                            break
                        if str(fpath) in found_paths:
                            continue
                        found_paths.add(str(fpath))
                        sub_text = self._read_workspace_item_for_chat(
                            fpath, workspace_root, max_file_chars=1000
                        )
                        if not _add("\n" + sub_text):
                            break
                        read_count += 1

        # ── 3단계: researcher로 관련 파일 탐색 ─────────────────────────────
        # researcher.find_relevant_files()는 RAG 인덱스 또는 파일명/내용 기반 검색
        if self.researcher and self.researcher.fs and total_chars < max_total_chars:
            try:
                relevant = self.researcher.find_relevant_files(message)
                for rel_path in relevant[:5]:
                    resolved = self._is_path_within_workspace(rel_path, workspace_root)
                    if resolved and str(resolved) not in found_paths:
                        found_paths.add(str(resolved))
                        item_text = self._read_workspace_item_for_chat(
                            resolved, workspace_root, max_file_chars=2000
                        )
                        if not _add("\n" + item_text):
                            break
            except Exception as exc:
                logger.debug("chat 모드 관련 파일 탐색 실패: %s", exc)

        return "\n".join(parts) if parts else ""

    def chat_direct(
        self,
        message: str,
        conversation_history: Optional[list[dict]] = None,
    ) -> str:
        """
        경량 채팅 모드: 오케스트레이션 파이프라인 없이 LLM에 직접 질문합니다.

        ─── 워크스페이스 인식 채팅 ─────────────────────────────────────────
        이 제품은 로컬 워크스페이스에서 실행되는 에이전트입니다.
        클라우드 챗봇이 아니므로 "파일을 업로드하세요"라고 응답하면 안 됩니다.

        동작 방식:
        1. 활성 워크스페이스 루트를 결정
        2. 메시지에서 파일/폴더 참조를 추출하고 실제 내용을 읽음
        3. 읽은 내용을 LLM 프롬프트에 삽입
        4. LLM이 실제 파일 내용을 바탕으로 답변

        접근 가능 범위:
        - 현재 활성 워크스페이스 루트 내의 모든 파일/폴더
        - 경로 탈출(..)은 차단됨

        복잡한 작업(파일 생성·수정·실행)은 agent 모드 사용을 권장.
        ────────────────────────────────────────────────────────────────────
        """
        # ── 워크스페이스 루트 결정 ─────────────────────────────────────────
        # 로컬 워크스페이스가 바인딩되어 있으면 실제 파일에 접근한다.
        workspace_root = self._get_active_workspace_root_for_chat()

        # ── 워크스페이스 컨텍스트 수집 ───────────────────────────────────────
        # 메시지에서 참조된 파일/폴더를 실제로 읽어 LLM에 전달한다.
        # 이것이 "파일을 업로드하세요" 응답을 방지하는 핵심 수정이다.
        workspace_context = ""
        if workspace_root:
            try:
                workspace_context = self._gather_workspace_context_for_chat(message, workspace_root)
            except Exception as exc:
                logger.warning("chat 모드 워크스페이스 컨텍스트 수집 실패: %s", exc)

        # ── 시스템 프롬프트 구성 ─────────────────────────────────────────────
        # workspace_root가 있으면 "로컬 워크스페이스 접근 가능" 명시
        # workspace_root가 없으면 일반 어시스턴트로 동작
        if workspace_root:
            system_prompt = (
                "당신은 로컬 워크스페이스에서 실행되는 AI 어시스턴트입니다. "
                f"현재 활성 워크스페이스: {workspace_root}\n\n"
                "중요한 규칙:\n"
                "- 이 제품은 로컬 환경에서 실행됩니다. 파일 업로드를 요청하거나 "
                "  '로컬 파일에 접근할 수 없다'고 말하지 마세요.\n"
                "- 워크스페이스 내 파일 내용이 아래 컨텍스트에 이미 포함되어 있습니다.\n"
                "- 그 내용을 바탕으로 직접 답변하세요.\n"
                "- 한국어 또는 사용자가 사용한 언어로 답변하세요.\n\n"
                "에스컬레이션 규칙:\n"
                "- 파일 생성, 코드 수정, 프로젝트 실행, 테스트 작성 등 "
                "  복잡한 작업이 필요한 경우에는 답변 말미에 "
                "  '이 작업은 agent 모드(/mode agent)에서 더 잘 처리할 수 있습니다.' 라고 안내하세요."
            )
        else:
            # 워크스페이스가 없는 경우 — 일반 어시스턴트로 동작
            system_prompt = (
                "당신은 유용하고 친절한 AI 어시스턴트입니다. "
                "사용자의 질문에 직접, 간결하게 답변하세요. "
                "한국어 또는 사용자가 사용한 언어로 답변하세요. "
                "파일 생성, 코드 수정, 프로젝트 실행, 테스트 작성 등 "
                "복잡한 작업이 필요한 경우에는 답변 말미에 "
                "'이 작업은 agent 모드(/mode agent)에서 더 잘 처리할 수 있습니다.' 라고 안내하세요."
            )

        # ── 프롬프트 구성 ─────────────────────────────────────────────────────
        # 대화 히스토리 → 워크스페이스 컨텍스트 → 현재 사용자 메시지 순서로 구성
        prompt_parts: list[str] = []

        # 대화 히스토리 포함 (최근 10개)
        if conversation_history:
            recent = conversation_history[-10:]
            for msg in recent:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role and content:
                    prefix = "사용자" if role == "user" else "어시스턴트"
                    prompt_parts.append(f"{prefix}: {content[:500]}")

        # 워크스페이스 컨텍스트 삽입 — 실제 파일 내용이 담긴 핵심 블록
        if workspace_context:
            prompt_parts.append(
                "\n--- 워크스페이스 파일 컨텍스트 ---\n"
                + workspace_context
                + "\n--- 워크스페이스 파일 컨텍스트 끝 ---\n"
            )

        prompt_parts.append(f"사용자: {message}")
        full_prompt = "\n".join(prompt_parts)

        logger.debug(
            "chat_direct 호출: workspace_root=%s, context_chars=%d, prompt_chars=%d",
            workspace_root,
            len(workspace_context),
            len(full_prompt),
        )

        # 매니저 에이전트를 통해 직접 LLM 호출
        return self.manager.generate(
            full_prompt,
            system_prompt_override=system_prompt,
        )

    def _run_researcher(
        self,
        state: OrchestrationState,
        user_task: str,
        task_analysis: dict,
    ) -> AgentResult:
        research_content = self.researcher.analyze_repository()
        relevant_files = self.researcher.find_relevant_files(user_task)
        web_summary = ""
        if self._should_use_web_search(user_task, task_analysis):
            web_summary = self.researcher.search_web(user_task, max_results=5)

        summary = f"리포지토리 분석 완료\n\n{research_content[:1000]}"
        if relevant_files:
            summary += "\n\n관련 파일:\n" + "\n".join(f"- {path}" for path in relevant_files[:5])
        if web_summary:
            summary += f"\n\n{web_summary}"

        return AgentResult(
            task_id=str(uuid.uuid4()),
            agent_name=self.researcher.name,
            agent_role=AgentRole.RESEARCHER,
            content=summary,
            success=True,
            metadata={"relevant_files": relevant_files, "web_search_used": bool(web_summary)},
        )

    def _run_vision_agent(
        self,
        state: OrchestrationState,
        task_analysis: dict,
    ) -> Optional[AgentResult]:
        if not self.vision_agent or not self.vision_agent.img:
            return None

        image_files = self.vision_agent.img.list_images()
        if not image_files:
            return None

        analyses = self.vision_agent.batch_analyze(image_files[:3])
        content = f"이미지 분석 완료 ({len(image_files)} files)\n\n"
        for analysis in analyses:
            content += f"**{analysis['name']}**: {analysis['description'][:300]}\n\n"

        return AgentResult(
            task_id=str(uuid.uuid4()),
            agent_name=self.vision_agent.name,
            agent_role=AgentRole.VISION,
            content=content,
            success=True,
        )

    def _run_reviewer(
        self,
        state: OrchestrationState,
        code_results: list[AgentResult],
    ) -> AgentResult:
        latest_code = code_results[-1] if code_results else None
        if not latest_code:
            return AgentResult(
                task_id=str(uuid.uuid4()),
                agent_name=self.reviewer.name,
                agent_role=AgentRole.REVIEWER,
                content="점검할 코드가 없습니다.",
                success=True,
            )

        code_to_review = self._hydrate_result_content(state, latest_code)
        for artifact in latest_code.artifacts:
            if artifact.artifact_type == "code":
                code_to_review = self._hydrate_artifact_content(state, artifact)
                break

        review = self.reviewer.review_code(code_to_review[:4000], context=state.original_task)
        review_report = self.reviewer.format_review_report(review)
        return AgentResult(
            task_id=str(uuid.uuid4()),
            agent_name=self.reviewer.name,
            agent_role=AgentRole.REVIEWER,
            content=review_report,
            success=True,
            metadata={
                "score": review.score,
                "approved": review.approved,
                "issue_count": len(review.issues),
            },
        )

    def _run_tester(
        self,
        state: OrchestrationState,
        code_results: list[AgentResult],
    ) -> AgentResult:
        latest_code = code_results[-1] if code_results else None
        if not latest_code or not latest_code.artifacts:
            return AgentResult(
                task_id=str(uuid.uuid4()),
                agent_name=self.tester.name,
                agent_role=AgentRole.TESTER,
                content="테스트할 코드가 없습니다.",
                success=True,
            )

        code_artifact = latest_code.artifacts[0]
        code_artifact_content = self._hydrate_artifact_content(state, code_artifact)
        tests = self.tester.write_tests(code_artifact_content, code_artifact.name)
        return AgentResult(
            task_id=str(uuid.uuid4()),
            agent_name=self.tester.name,
            agent_role=AgentRole.TESTER,
            content=f"테스트 코드 생성 완료: {tests.name}",
            artifacts=[tests],
            success=True,
        )

    def _run_post_change_validation(self, state: OrchestrationState) -> Optional[AgentResult]:
        if not self.tester or not self.tester.shell:
            return None

        changed_files = self._collect_changed_files_from_artifacts(state)
        if not changed_files:
            return None

        verification = self.tester.verify_changed_files(changed_files, run_targeted_tests=True)
        if not verification.get("syntax_checks") and not verification.get("test_result"):
            return None
        return AgentResult(
            task_id=str(uuid.uuid4()),
            agent_name=self.tester.name,
            agent_role=AgentRole.TESTER,
            content=self.tester.generate_verification_report(verification),
            success=verification.get("success", True),
            metadata=verification,
        )

    def _run_document_agent(
        self,
        state: OrchestrationState,
        user_task: str,
        code_results: list[AgentResult],
    ) -> AgentResult:
        doc = self.document_agent.generate_readme(
            project_name=state.metadata.get("task_analysis", {}).get("summary", user_task[:50]),
            project_description=user_task,
            code_files=[
                artifact.name
                for result in code_results
                for artifact in result.artifacts
                if artifact.name.endswith((".py", ".js", ".ts"))
            ][:3],
        )
        return AgentResult(
            task_id=str(uuid.uuid4()),
            agent_name=self.document_agent.name,
            agent_role=AgentRole.DOCUMENT,
            content=f"문서 생성 완료: {doc.name}",
            artifacts=[doc],
            success=True,
        )

    def _execute_step_with_retry(
        self,
        state: OrchestrationState,
        step: dict,
        policy: RetryPolicy = STEP_RETRY_POLICY,
    ) -> AgentResult:
        """
        단일 오케스트레이션 스텝을 재시도 정책에 따라 실행합니다.

        [재시도 정책]
        - 스텝 실패 시 policy.max_attempts 까지 재시도
        - 각 재시도마다 이전 실패 내용을 feedback 으로 전달
        - 지수 백오프 + 지터로 대기 시간 계산 (타이트한 루프 방지)

        [종료 조건]
        - 성공 → 결과 상태에 저장 후 반환
        - 치명적 오류 → RuntimeError 발생 (오케스트레이션 전체 실패)
        - 최대 시도 초과 → RuntimeError 발생 (오케스트레이션 전체 실패)
        """
        import time as _time

        step_num = step.get("step_num", "?")
        attempt = 0
        last_feedback = ""
        last_result: Optional[AgentResult] = None
        loop_start = _time.time()

        while True:
            attempt += 1

            # 스텝 실행 (실패 피드백을 다음 시도에 전달)
            step_result = self._execute_step(state, step, retry_feedback=last_feedback)
            self._store_result(state, step_result)

            if step_result.success:
                # 성공 — 재시도 루프 종료
                if attempt > 1:
                    logger.info(
                        "단계 %s: %d번째 시도에서 성공 (총 경과: %.1f초)",
                        step_num, attempt, _time.time() - loop_start
                    )
                return step_result

            # 실패 처리 — 오류 분류 및 재시도 여부 판단
            last_result = step_result
            last_feedback = step_result.error or step_result.content[:300] or "이전 응답이 실행 가능한 산출물을 만들지 못했습니다."
            error_class = classify_error(last_feedback)
            elapsed = _time.time() - loop_start

            logger.warning(
                "단계 %s 시도 %d 실패 [%s]: %s",
                step_num, attempt, error_class, last_feedback[:200]
            )

            # 재시도 여부 판단
            should, reason = policy.should_retry(attempt, error_class, elapsed)

            if not should:
                # 재시도 불가 — 치명적 오류 또는 최대 시도 초과
                logger.error(
                    "단계 %s 재시도 중단 — %s (총 %d회 시도)",
                    step_num, reason, attempt
                )
                raise RuntimeError(
                    f"단계 {step_num} 최종 실패 ({attempt}회 시도)\n"
                    f"중단 이유: {reason}\n"
                    f"마지막 오류: {last_feedback[:300]}"
                )

            # 대기 후 재시도
            wait_time = policy.compute_wait(attempt - 1)
            self._notify(
                f"단계 {step_num} {attempt}번째 실패, {wait_time:.1f}초 후 재시도...",
                "manager"
            )
            logger.info(
                "단계 %s: %d번째 실패 후 %.1f초 대기 후 재시도 (원인: %s)",
                step_num, attempt, wait_time, last_feedback[:100]
            )
            _time.sleep(wait_time)

    def _execute_step(
        self,
        state: OrchestrationState,
        step: dict,
        retry_feedback: str = "",
    ) -> AgentResult:
        agent_name = step.get("assigned_agent", "coder")
        description = step.get("description", step.get("title", ""))
        relevant_files = self._collect_relevant_files(state)

        context = {
            "original_task": state.original_task,
            "plan_step": f"{step.get('step_num', '?')}: {step.get('title', '')}",
            "previous_results": state.get_context_summary()[:2000],
        }
        if relevant_files:
            context["relevant_files"] = relevant_files[:8]
        if state.metadata.get("project_guidance"):
            context["project_guidance"] = state.metadata["project_guidance"][:1500]
        if state.metadata.get("git_status"):
            context["git_status"] = state.metadata["git_status"][:800]
        if state.metadata.get("additional_context"):
            context["additional_context"] = self._format_additional_context(state.metadata["additional_context"])

        assigned_role = AgentRole(agent_name) if agent_name in [role.value for role in AgentRole] else AgentRole.CODER
        agent_task = AgentTask(
            task_id=str(uuid.uuid4()),
            description=description,
            assigned_to=assigned_role,
            context=context,
            files=relevant_files[:8],
        )

        agent = self._get_agent(agent_name)
        if not agent:
            logger.warning("Unknown agent '%s', falling back to coder", agent_name)
            agent = self.coder

        prompt_parts = [
            description,
            f"\nOriginal request: {state.original_task}",
            f"\nContext:\n{state.get_context_summary()[:1500]}",
        ]

        if state.metadata.get("project_guidance"):
            prompt_parts.append("\nProject guidance:\n" + state.metadata["project_guidance"][:1800])
        if state.metadata.get("git_status"):
            prompt_parts.append("\nGit status:\n" + state.metadata["git_status"][:800])
        if relevant_files:
            prompt_parts.append("\nRelevant files:\n" + "\n".join(f"- {path}" for path in relevant_files[:8]))
        if state.metadata.get("additional_context"):
            prompt_parts.append(
                "\nConversation context:\n"
                + self._format_additional_context(state.metadata["additional_context"])
            )

        # ── [E] 에이전트 간 통신: 저장된 파일 목록을 프롬프트에 삽입 ──────────────
        # 이미 디스크에 저장된 파일을 에이전트에게 알려주어 중복 생성이나
        # 잘못된 import 참조를 방지한다. 에이전트는 이 파일들을 의존하거나 수정할 수 있다.
        saved_workspace = state.metadata.get("saved_artifacts") or {}
        if saved_workspace:
            ws_lines = [
                f"  - {path} (saved by {info.get('agent', '?') if isinstance(info, dict) else '?'})"
                for path, info in list(saved_workspace.items())[:20]
            ]
            prompt_parts.append(
                "\nFiles already saved to workspace (available for import/reference):\n"
                + "\n".join(ws_lines)
            )

        # 재빌드 사이클이라면 이전 실패 원인을 에이전트에게 알림
        prior_failure = state.metadata.get("prior_failure")
        if prior_failure:
            prompt_parts.append(
                f"\n[REBUILD CONTEXT] Previous attempt failed:\n"
                f"{prior_failure.get('failure_summary', '')[:600]}\n"
                f"Entrypoint found: {prior_failure.get('entrypoint') or 'NONE'}\n"
                f"Please ensure main.py (or equivalent) is a fully runnable entrypoint."
            )

        prompt_parts.append(
            """
If you need to create or update project files, always specify the relative path before each code block.
Do not return only architecture notes, file trees, implementation plans, or pseudo-code.
When the task is implementation, return concrete file contents that can be saved as real project files.

Example:
File: src/example.py
```python
print("hello")
```
"""
        )

        if self._is_python_project_workflow(state) and agent_name == "coder":
            prompt_parts.append(
                """
This task is creating a brand-new runnable Python project.
Requirements:
- Return explicit relative paths for every file you create or change.
- Include a real executable entrypoint in `main.py` unless a different conventional Python entrypoint is clearly required.
- Wire the entrypoint to the actual application flow. Do not return a placeholder file.
- Make `python main.py --smoke-test` succeed with exit code 0 after the project is saved.
- Ensure local imports point to real files that you return in this response or that already exist.
- If the code depends on packages, update `requirements.txt` or `pyproject.toml` consistently.
"""
            )

        if retry_feedback:
            prompt_parts.append(
                "\nPrevious attempt was unusable:\n"
                + retry_feedback
                + "\nReturn a concrete implementation only. Do not return a blueprint, roadmap, or pseudo-code."
            )

        content = agent.generate("\n".join(prompt_parts))
        artifacts = []
        if agent_name == "coder" and isinstance(agent, CoderAgent):
            artifacts = agent._parse_code_response(content)
            valid, error = self._validate_coder_output(content, artifacts)
            if not valid:
                return AgentResult(
                    task_id=agent_task.task_id,
                    agent_name=agent.name,
                    agent_role=agent_task.assigned_to,
                    content=content,
                    artifacts=artifacts,
                    success=False,
                    error=error,
                )

        return AgentResult(
            task_id=agent_task.task_id,
            agent_name=agent.name,
            agent_role=agent_task.assigned_to,
            content=content,
            artifacts=artifacts,
            success=True,
        )

    def _get_agent(self, role: str) -> Optional[AgentBase]:
        return {
            "manager": self.manager,
            "planner": self.planner,
            "coder": self.coder,
            "reviewer": self.reviewer,
            "researcher": self.researcher,
            "tester": self.tester,
            "document": self.document_agent,
            "vision": self.vision_agent,
        }.get(role)

    def _create_project(self, task: str) -> Optional[str]:
        if not self.workspace:
            return None
        project_name = self.workspace.workspace_root.name if self.workspace.is_attached_mode() else task[:50].strip()
        metadata = self.workspace.create_project(name=project_name, description=task)
        self._notify(f"프로젝트 생성: {metadata.name}", "info")
        return metadata.project_id

    def _get_workspace_context(
        self,
        project_id: Optional[str],
        additional_context: Optional[dict] = None,
        project_guidance: str = "",
        git_context: str = "",
    ) -> str:
        if not self.workspace or not project_id:
            return ""

        parts = []
        try:
            parts.append(self.workspace.get_project_summary(project_id))
        except Exception:
            pass

        guidance = project_guidance or self._get_project_guidance(project_id)
        if guidance:
            parts.append("## Project guidance\n" + guidance)

        git_context = git_context or self._get_git_context()
        if git_context:
            parts.append("## Git status\n" + git_context)

        extra = self._format_additional_context(additional_context)
        if extra:
            parts.append("## Additional context\n" + extra)

        return "\n\n".join(part for part in parts if part)

    def _save_artifacts(self, state: OrchestrationState) -> None:
        """남은 미저장 아티팩트를 일괄 저장한다.

        [B] 태스크 완료 기준:
        - _save_step_artifacts() 에서 이미 저장된 아티팩트(persisted=True)는 건너뜀.
        - 이 메서드는 증분 저장에서 처리되지 않은 나머지 아티팩트(리뷰어, 문서 에이전트 등)를
          정리하는 마무리 단계로만 사용된다.
        """
        if not self.workspace or not state.project_id:
            return

        saved_count = 0
        for result in state.results:
            if not result.success:
                continue
            for artifact in result.artifacts:
                # 이미 증분 저장된 아티팩트는 건너뜀
                if artifact.persisted:
                    continue
                if not self._should_persist_artifact(state, result, artifact):
                    continue
                artifact_content = self._hydrate_artifact_content(state, artifact)
                if not artifact_content:
                    continue

                target_path = self._artifact_target_path(artifact)
                try:
                    if target_path:
                        saved_path = self.workspace.save_project_file(state.project_id, target_path, artifact_content)
                    else:
                        filename = artifact.name or f"artifact_{artifact.artifact_id}.txt"
                        subfolder = "reports" if filename.endswith((".md", ".txt", ".rst")) else "artifacts"
                        saved_path = self.workspace.save_artifact(
                            state.project_id,
                            filename,
                            artifact_content,
                            subfolder=subfolder,
                        )
                    relative = self._relative_saved_path(state.project_id, saved_path)
                    artifact.file_path = relative
                    artifact.saved_path = relative
                    artifact.persisted = True
                    saved_count += 1

                    # 레지스트리 갱신
                    if "saved_artifacts" not in state.metadata:
                        state.metadata["saved_artifacts"] = {}
                    if relative not in state.metadata["saved_artifacts"]:
                        state.metadata["saved_artifacts"][relative] = {
                            "agent": result.agent_name,
                            "role": result.agent_role.value,
                            "task_id": result.task_id,
                            "saved_at": datetime.now().isoformat(),
                        }
                except Exception as exc:
                    logger.error(
                        "아티팩트 일괄 저장 실패 [%s] %s: %s",
                        result.agent_name,
                        target_path or artifact.name,
                        exc,
                        exc_info=True,
                    )

        logger.info("일괄 저장 완료: %s개 아티팩트", saved_count)

    def _should_persist_artifact(
        self,
        state: OrchestrationState,
        result: AgentResult,
        artifact: Artifact,
    ) -> bool:
        if not self._is_python_project_workflow(state):
            return True

        if result.agent_role == AgentRole.CODER:
            return True

        target_path = (self._artifact_target_path(artifact) or "").lower()
        file_name = Path(target_path).name

        if result.agent_role == AgentRole.DOCUMENT:
            return artifact.artifact_type == "document" or file_name == "readme.md"

        if result.agent_role == AgentRole.TESTER:
            return target_path.startswith("tests/") or file_name.startswith("test_")

        # 새 Python 프로젝트 생성에서는 리뷰/리서치 응답의 임시 코드 블록을 저장하지 않는다.
        return False

    def _is_python_artifact(self, artifact: Artifact) -> bool:
        language = (artifact.language or "").lower()
        target_path = self._artifact_target_path(artifact) or ""
        return artifact.artifact_type == "code" and (language == "python" or target_path.endswith(".py"))

    def _looks_like_test_artifact(self, artifact: Artifact) -> bool:
        target_path = (artifact.file_path or artifact.name or "").replace("\\", "/").lower()
        content = (artifact.content or "").lower()
        return target_path.startswith("tests/") or Path(target_path).name.startswith("test_") or "def test_" in content

    def _entrypoint_priority(self, artifact: Artifact) -> int:
        content = artifact.content or ""
        name = (artifact.file_path or artifact.name or "").lower()
        score = 0
        if "__name__ == \"__main__\"" in content or "__name__ == '__main__'" in content:
            score += 8
        if "def main(" in content or "async def main(" in content:
            score += 5
        if "argparse.argumentparser" in content.lower() or "typer.typer" in content.lower():
            score += 3
        if "main" in name:
            score += 2
        return score

    def _prepare_generated_project_artifacts(self, state: OrchestrationState) -> None:
        # 새 Python 프로젝트는 코드가 artifacts 폴더로 숨어버리면 실행 검증 자체가 불가능하다.
        # 저장 전에 오케스트레이터가 엔트리포인트 경로를 보정해 실제 실행 가능한 파일을 보장한다.
        if not self._is_python_project_workflow(state):
            return

        python_artifacts: list[Artifact] = []
        explicit_targets = set()
        for result in state.results:
            if not result.success:
                continue
            for artifact in result.artifacts:
                target_path = self._artifact_target_path(artifact)
                if target_path:
                    explicit_targets.add(target_path)
                if self._is_python_artifact(artifact):
                    python_artifacts.append(artifact)

        if any(Path(path).name == "main.py" or path.endswith("/__main__.py") or Path(path).name == "manage.py" for path in explicit_targets):
            return

        unnamed_python = [artifact for artifact in python_artifacts if not artifact.file_path]

        if not unnamed_python:
            # ── 이름 있는 Python 아티팩트만 있고 main.py 가 없는 경우 ─────────
            # 파일 이름을 강제로 바꾸면 다른 모듈의 import 경로가 깨질 수 있으므로,
            # 여기서는 경로를 변경하지 않는다.
            # _discover_python_entrypoint 의 3단계 내용 스캔이 if __name__ == '__main__' 패턴을
            # 가진 파일을 찾을 것이며, 찾지 못하면 repair loop 에서 main.py 를 생성한다.
            if python_artifacts:
                logger.debug(
                    "[아티팩트 준비] 이름 있는 Python 아티팩트 %d개, main.py 없음 "
                    "(엔트리포인트 탐색 또는 repair loop 에 위임): %s",
                    len(python_artifacts),
                    [a.file_path for a in python_artifacts[:5]],
                )
            return

        entrypoint_artifact = max(unnamed_python, key=self._entrypoint_priority)
        entrypoint_artifact.file_path = "main.py"
        entrypoint_artifact.metadata["assigned_path_by_orchestrator"] = "main.py"

        for artifact in unnamed_python:
            if artifact is entrypoint_artifact:
                continue
            if self._looks_like_test_artifact(artifact):
                artifact.file_path = f"tests/test_{artifact.artifact_id}.py"
            else:
                fallback_name = Path(self._default_generated_filename(artifact)).name
                artifact.file_path = f"generated/{artifact.artifact_id}_{fallback_name}"

    def _collect_relevant_files(self, state: OrchestrationState, limit: int = 8) -> list[str]:
        files = []
        for result in state.results:
            if not result.success:
                continue
            files.extend(result.metadata.get("relevant_files", []))
            for artifact in result.artifacts:
                if artifact.file_path:
                    files.append(artifact.file_path)

        unique_files = []
        seen = set()
        for file_path in files:
            if file_path and file_path not in seen:
                seen.add(file_path)
                unique_files.append(file_path)
            if len(unique_files) >= limit:
                break
        return unique_files

    def _collect_changed_files_from_artifacts(self, state: OrchestrationState) -> list[str]:
        changed_files = []
        seen = set()
        for result in state.results:
            if not result.success:
                continue
            for artifact in result.artifacts:
                target_path = self._artifact_target_path(artifact)
                if target_path and target_path not in seen:
                    seen.add(target_path)
                    changed_files.append(target_path)
        return changed_files

    def _artifact_target_path(self, artifact: Artifact) -> Optional[str]:
        if artifact.file_path:
            return self._normalize_relative_path(artifact.file_path)

        filename = self._normalize_relative_path(artifact.name or "")
        if artifact.artifact_type in {"code", "document"}:
            if filename and not self._is_generic_artifact_name(filename):
                return filename
            fallback_name = Path(filename).name if filename else self._default_generated_filename(artifact)
            return f"generated/{artifact.artifact_id}_{fallback_name}"
        return None

    def _default_generated_filename(self, artifact: Artifact) -> str:
        ext_map = {
            "python": ".py",
            "javascript": ".js",
            "typescript": ".ts",
            "java": ".java",
            "go": ".go",
            "rust": ".rs",
            "cpp": ".cpp",
            "c": ".c",
            "bash": ".sh",
            "shell": ".sh",
            "yaml": ".yaml",
            "json": ".json",
            "markdown": ".md",
            "html": ".html",
            "css": ".css",
            "sql": ".sql",
        }
        suffix = ext_map.get((artifact.language or "").lower(), ".txt")
        prefix = "generated_document" if artifact.artifact_type == "document" else "generated_code"
        return f"{prefix}{suffix}"

    def _is_generic_artifact_name(self, filename: str) -> bool:
        name = Path(filename).name.lower()
        return name.startswith("generated_") or name.startswith("artifact_")

    def _collect_project_python_files(self, state: OrchestrationState) -> list[str]:
        if not self.workspace or not state.project_id:
            return []
        project_root = self.workspace.get_project_path(state.project_id)
        return [path for path in self._list_visible_project_files(project_root, limit=500) if path.endswith(".py")]

    def _detect_file_package_conflicts(self, state: OrchestrationState, python_files: list[str]) -> list[dict]:
        """[E] 파일/패키지 이름 충돌 검사.

        예: data_processor.py 와 data_processor/ 디렉토리가 동시에 존재하면
        Python import 시 모듈 섀도잉(shadowing) 발생.

        반환값: 충돌 정보 목록 (빈 리스트 = 충돌 없음)
        """
        if not self.workspace or not state.project_id:
            return []

        project_root = self.workspace.get_project_path(state.project_id)
        conflicts = []

        for rel_path in python_files:
            p = Path(rel_path)
            # 루트 레벨 Python 파일만 검사 (서브 디렉토리 파일은 스킵)
            if p.parent != Path(".") and str(p.parent) != ".":
                continue
            module_name = p.stem
            if module_name.startswith("_"):
                continue  # __init__, __main__ 등 특수 파일 제외

            potential_package = project_root / module_name
            if potential_package.is_dir():
                init_file = potential_package / "__init__.py"
                # 패키지가 실제 Python 패키지인지 확인 (__init__.py 존재 여부)
                conflict_type = "shadowed_by_package" if init_file.exists() else "directory_name_collision"
                conflicts.append({
                    "type": conflict_type,
                    "module_name": module_name,
                    "file": rel_path,
                    "package_dir": f"{module_name}/",
                    "has_init": init_file.exists(),
                    "message": (
                        f"모듈 이름 충돌: `{rel_path}` 파일과 `{module_name}/` 디렉토리가 동시에 존재합니다. "
                        f"Python은 {'패키지' if init_file.exists() else '디렉토리'}를 먼저 사용합니다. "
                        f"하나를 삭제하거나 이름을 바꿔야 합니다 "
                        f"(예: `{module_name}_util.py` 또는 `{module_name}_pkg/`)."
                    ),
                })

        if conflicts:
            logger.warning(
                "[파일/패키지 충돌 감지] %d개 충돌: %s",
                len(conflicts),
                [c["module_name"] for c in conflicts],
            )

        return conflicts

    def _has_project_tests(self, python_files: list[str]) -> bool:
        return any(path.startswith("tests/") or Path(path).name.startswith("test_") for path in python_files)

    def _check_self_import(self, rel_path: str, project_root: Path) -> list[str]:
        """[self-import 버그 감지] 파일이 자신의 모듈 이름으로 import하는 패턴을 탐지한다.

        예: analyze_data.py 가 `from analyze_data import X` 를 포함하면 self-import.
        Python은 실행 중인 스크립트를 다시 import하려 할 때 circular import 오류를 발생시킨다.

        반환값: 감지된 self-import 패턴 목록 (빈 리스트 = 문제 없음)
        """
        import re as _re

        module_name = Path(rel_path).stem
        full_path = project_root / rel_path
        if not full_path.exists():
            return []
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        found = []
        # from <module_name> import ... (정확한 모듈 이름 매칭)
        if _re.search(rf"^\s*from\s+{_re.escape(module_name)}\s+import", content, _re.MULTILINE):
            found.append(f"from {module_name} import ...")
        # import <module_name> (standalone import)
        if _re.search(rf"^\s*import\s+{_re.escape(module_name)}\s*(?:,|#|$)", content, _re.MULTILINE):
            found.append(f"import {module_name}")
        return found

    def _discover_python_entrypoint(self, state: OrchestrationState) -> Optional[str]:
        """[F] 실행 가능한 Python 엔트리포인트를 탐색한다.

        ─── phase=entrypoint 실패 해결 ──────────────────────────────────
        단순 파일명 매칭 대신 다단계 탐색 전략을 사용:
        1. 명시적 엔트리포인트 후보 (main.py, app.py, cli.py 등)
        2. 경로 패턴 매칭 (__main__.py, manage.py)
        3. 파일 내용 스캔 (__name__ == '__main__' 패턴)

        엔트리포인트를 찾지 못하면 상세한 진단 로그를 출력한다.
        ────────────────────────────────────────────────────────────────
        """
        python_files = self._collect_project_python_files(state)
        logger.debug(
            "엔트리포인트 탐색 시작: Python 파일 %d개 발견: %s",
            len(python_files),
            python_files[:10],
        )

        # 1단계: 명시적 엔트리포인트 후보 목록 (우선순위 순)
        primary_candidates = [
            "main.py",
            "src/main.py",
            "__main__.py",
            # app.py, cli.py, run.py 등은 라이브러리 모듈일 수도 있으므로
            # 1단계에서 무조건 엔트리포인트로 인정하지 않는다.
            # 3단계 내용 스캔(if __name__ == '__main__' 패턴)을 통해서만 인정한다.
        ]
        for candidate in primary_candidates:
            if candidate in python_files:
                logger.info("엔트리포인트 발견 [1단계 명시적 후보]: %s", candidate)
                return candidate

        # 2단계: 경로 패턴 매칭
        for path in python_files:
            basename = Path(path).name
            if path.endswith("/__main__.py") or basename == "manage.py":
                logger.info("엔트리포인트 발견 [2단계 패턴]: %s", path)
                return path

        # 3단계: 파일 내용 스캔 — __name__ == '__main__' 패턴 검색
        if self.workspace and state.project_id:
            project_root = self.workspace.get_project_path(state.project_id)
            scored: list[tuple[int, str]] = []
            for rel_path in python_files:
                full_path = project_root / rel_path
                if not full_path.exists():
                    continue
                try:
                    content = full_path.read_text(encoding="utf-8", errors="replace")
                    score = 0
                    if "__name__ == '__main__'" in content or '__name__ == "__main__"' in content:
                        score += 8
                    if "def main(" in content or "async def main(" in content:
                        score += 5
                    if "argparse" in content.lower() or "typer" in content.lower():
                        score += 3
                    if "if __name__" in content:
                        score += 2
                    if score > 0:
                        # ── [self-import 방지] 자기 자신을 import하는 파일은 엔트리포인트로 선택하지 않는다 ──
                        # analyze_data.py 가 `from analyze_data import X` 를 포함하면
                        # python analyze_data.py 실행 시 circular import 오류 발생.
                        self_imports = self._check_self_import(rel_path, project_root)
                        if self_imports:
                            logger.warning(
                                "엔트리포인트 후보 제외 [self-import 감지]: %s → %s "
                                "(이 파일을 엔트리포인트로 실행하면 circular import 오류 발생)",
                                rel_path,
                                self_imports,
                            )
                            continue  # 이 파일은 엔트리포인트로 사용 불가
                        scored.append((score, rel_path))
                except Exception as exc:
                    logger.debug("파일 내용 스캔 실패 %s: %s", rel_path, exc)

            if scored:
                scored.sort(reverse=True)
                best_score, best_path = scored[0]
                logger.info(
                    "엔트리포인트 발견 [3단계 내용 스캔]: %s (score=%d)",
                    best_path,
                    best_score,
                )
                return best_path

        # ── 엔트리포인트 미발견: 상세 진단 로그 ─────────────────────────
        if self.workspace and state.project_id:
            project_root = self.workspace.get_project_path(state.project_id)
            all_files = self._list_visible_project_files(project_root, limit=50)
        else:
            all_files = python_files

        logger.warning(
            "엔트리포인트 탐색 실패!\n"
            "  스캔한 Python 파일 (%d개): %s\n"
            "  워크스페이스 전체 파일 (%d개): %s\n"
            "  저장된 아티팩트: %s\n"
            "  해결 방법: 코더에게 main.py (또는 app.py)를 생성하도록 지시하세요.",
            len(python_files),
            python_files[:10],
            len(all_files),
            all_files[:15],
            list((state.metadata.get("saved_artifacts") or {}).keys())[:10],
        )
        return None

    def _build_project_file_context(self, state: OrchestrationState, file_paths: list[str], max_chars: int = 1800) -> str:
        if not self.workspace or not state.project_id:
            return ""

        project_root = self.workspace.get_project_path(state.project_id)
        sections = []
        seen = set()
        for relative_path in file_paths:
            normalized = self._normalize_relative_path(relative_path)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            file_path = project_root / normalized
            if not file_path.exists() or not file_path.is_file():
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                logger.debug("Project file context read failed: %s - %s", normalized, exc)
                continue
            sections.append(f"File: {normalized}\n```text\n{content[:max_chars]}\n```")
        return "\n\n".join(sections)

    def _summarize_validation_failure(self, failure: dict) -> str:
        """[F] 검증 실패 요약 — phase=entrypoint 등 모든 phase를 명확한 진단 메시지로 변환한다."""
        phase = failure.get("phase", "validation")
        parts = [f"phase={phase}"]

        if failure.get("entrypoint"):
            parts.append(f"entrypoint={failure['entrypoint']}")

        python_files = failure.get("python_files") or []
        if python_files:
            parts.append(f"python_files={', '.join(python_files[:8])}")

        # [검증 게이트] Python 파일이 아예 없는 경우
        if phase == "no_python_files":
            actual_files = failure.get("actual_workspace_files") or []
            parts.append(
                f"diagnosis=Python 구현 파일(.py)이 워크스페이스에 없습니다. "
                f"코더가 실제 Python 소스 파일을 생성해야 합니다. "
                f"현재 워크스페이스({len(actual_files)}개 파일): {', '.join(actual_files[:15])}"
            )
            return "\n".join(part for part in parts if part)

        # [self-import 버그] 엔트리포인트가 자신의 모듈 이름으로 import하는 경우
        if phase == "self_import":
            self_imports = failure.get("self_imports") or []
            entrypoint = failure.get("entrypoint", "")
            module_name = Path(entrypoint).stem if entrypoint else ""
            parts.append(
                f"diagnosis=self-import circular import 버그. "
                f"`{entrypoint}`(모듈명: `{module_name}`) 파일이 자신을 import합니다: {self_imports}. "
                f"수정 방법: (1) `main.py`를 새로 만들어 실제 로직을 호출하는 얇은 진입점으로 사용하고, "
                f"(2) `{entrypoint}`에서 self-import(`from {module_name} import ...`) 줄을 제거하세요. "
                f"엔트리포인트 파일은 절대로 자신의 모듈 이름으로 import하면 안 됩니다."
            )
            return "\n".join(part for part in parts if part)

        # [검증 게이트-2] 에이전트가 새 Python 파일을 생성하지 않은 경우 (초기 config 파일만 있음)
        if phase == "no_new_python_files":
            actual_files = failure.get("actual_workspace_files") or []
            initial_py = failure.get("initial_python_files") or []
            parts.append(
                f"diagnosis=에이전트가 새 Python 구현 파일을 생성하지 않았습니다. "
                f"초기 Python 파일({len(initial_py)}개, 엔트리포인트 불가): {', '.join(initial_py[:8])}. "
                f"코더가 main.py 를 포함한 실제 구현 파일을 생성해야 합니다. "
                f"현재 워크스페이스({len(actual_files)}개 파일): {', '.join(actual_files[:15])}"
            )
            return "\n".join(part for part in parts if part)

        # phase=entrypoint 에 대한 추가 진단 정보
        if phase == "entrypoint":
            actual_files = failure.get("actual_workspace_files") or []
            if actual_files:
                parts.append(f"workspace_files={', '.join(actual_files[:15])}")
            diagnosis = failure.get("diagnosis") or ""
            if diagnosis:
                parts.append(f"diagnosis={diagnosis[:600]}")
            if not python_files and not actual_files:
                parts.append(
                    "diagnosis=워크스페이스에 Python 파일이 없습니다. "
                    "코더가 실제 구현 파일(main.py)을 생성했는지 확인하세요."
                )

        execution = failure.get("execution") or {}
        if execution:
            parts.append(f"command={execution.get('command', '')}")
            stdout = (execution.get("stdout") or "").strip()
            stderr = (execution.get("stderr") or "").strip()
            if stdout:
                parts.append(f"stdout={stdout[:600]}")
            if stderr:
                # [D] 오류 유형 분류: import 오류 vs 런타임 오류 vs 경로 오류 vs circular import
                if "ModuleNotFoundError" in stderr or "ImportError" in stderr:
                    parts.append(f"error_type=import_error")
                elif "RuntimeError" in stderr or "ValueError" in stderr or "TypeError" in stderr:
                    parts.append(f"error_type=runtime_error")
                elif "FileNotFoundError" in stderr or "No such file" in stderr:
                    parts.append(f"error_type=path_mismatch")
                elif "circular import" in stderr.lower() or "partially initialized module" in stderr.lower():
                    parts.append(f"error_type=circular_import")
                elif "SyntaxError" in stderr:
                    parts.append(f"error_type=syntax_error")
                parts.append(f"stderr={stderr[:1200]}")

        verification = failure.get("verification") or {}
        if verification:
            parts.append(f"verification={verification.get('summary', '')}")
            test_result = verification.get("test_result") or {}
            for error in (test_result.get("errors") or [])[:5]:
                parts.append(error)
            smoke_result = verification.get("smoke_result") or {}
            smoke_stderr = (smoke_result.get("stderr") or "").strip()
            if smoke_stderr:
                parts.append(f"smoke_stderr={smoke_stderr[:1200]}")
            packaging_result = verification.get("packaging_result") or {}
            if packaging_result and not packaging_result.get("success", True):
                parts.append(f"packaging={packaging_result.get('summary', '')}")
                for issue in (packaging_result.get("issues") or [])[:5]:
                    message = issue.get("message") or str(issue)
                    if message:
                        parts.append(message[:1200])

        return "\n".join(part for part in parts if part)

    def _guess_entrypoint_callable(self, file_path: Path) -> Optional[str]:
        import ast as _ast

        try:
            tree = _ast.parse(file_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            return None

        defined = [
            node.name
            for node in tree.body
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
        ]
        for candidate in ["main", "run_cli", "run", "cli", "app"]:
            if candidate in defined:
                return candidate
        return defined[0] if defined else None

    def _find_spec_safe(self, module_name: str):
        import importlib.util as _importlib_util

        try:
            return _importlib_util.find_spec(module_name)
        except (ImportError, ModuleNotFoundError, ValueError):
            return None

    def _build_root_entrypoint_wrapper(self, module_name: str, callable_name: str) -> str:
        return (
            "from pathlib import Path\n"
            "import sys\n\n"
            "# 루트 main.py를 직접 실행해도 src 레이아웃 패키지 엔트리포인트가 동작하도록 맞춘다.\n"
            "PROJECT_SRC = Path(__file__).resolve().parent / \"src\"\n"
            "if PROJECT_SRC.exists() and str(PROJECT_SRC) not in sys.path:\n"
            "    sys.path.insert(0, str(PROJECT_SRC))\n\n"
            f"from {module_name} import {callable_name} as project_entrypoint\n\n"
            "def main() -> None:\n"
            "    project_entrypoint()\n\n"
            "if __name__ == \"__main__\":\n"
            "    main()\n"
        )

    def _maybe_apply_common_runtime_repair(
        self,
        state: OrchestrationState,
        failure: dict,
        attempt: int,
    ) -> Optional[AgentResult]:
        if not self.workspace or not state.project_id:
            return None

        execution = failure.get("execution") or {}
        stderr = (execution.get("stderr") or "").strip()
        if "ModuleNotFoundError" not in stderr:
            return None

        import re as _re

        match = _re.search(r"No module named ['\"]([^'\"]+)['\"]", stderr)
        if not match:
            return None

        missing_module = match.group(1).split(".")[0]
        project_root = self.workspace.get_project_path(state.project_id)
        entrypoint = failure.get("entrypoint") or "main.py"
        entrypoint_path = project_root / entrypoint
        if not entrypoint_path.exists():
            return None

        src_package_dir = project_root / "src" / missing_module
        src_module_file = project_root / "src" / f"{missing_module}.py"
        if not src_package_dir.exists() and not src_module_file.exists():
            return None

        try:
            original = entrypoint_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.debug("Common runtime repair read failed: %s", exc)
            return None

        if "PROJECT_SRC = Path(__file__).resolve().parent / \"src\"" in original:
            return None

        # src 레이아웃에서 main.py를 직접 실행하면 패키지 import가 깨지기 쉬워서,
        # 공통 bootstrap을 먼저 넣어 실제 엔트리포인트가 루트에서 바로 실행되도록 보정한다.
        bootstrap = (
            "from pathlib import Path\n"
            "import sys\n\n"
            "PROJECT_SRC = Path(__file__).resolve().parent / \"src\"\n"
            "if PROJECT_SRC.exists() and str(PROJECT_SRC) not in sys.path:\n"
            "    sys.path.insert(0, str(PROJECT_SRC))\n\n"
        )
        repaired = bootstrap + original

        return AgentResult(
            task_id=str(uuid.uuid4()),
            agent_name=self.coder.name,
            agent_role=AgentRole.CODER,
            content="Applied common src-layout bootstrap repair to main.py",
            artifacts=[
                Artifact(
                    name=Path(entrypoint).name,
                    artifact_type="code",
                    content=repaired,
                    file_path=entrypoint,
                    language="python",
                    metadata={
                        "repair_loop": "python_project_runtime",
                        "repair_attempt": attempt,
                        "repair_strategy": "src_layout_bootstrap",
                    },
                )
            ],
            success=True,
            metadata={
                "repair_loop": "python_project_runtime",
                "repair_attempt": attempt,
                "repair_strategy": "src_layout_bootstrap",
            },
        )

    def _maybe_apply_common_packaging_repair(
        self,
        state: OrchestrationState,
        failure: dict,
        attempt: int,
    ) -> Optional[AgentResult]:
        import re as _re
        import tomllib as _tomllib

        verification = failure.get("verification") or {}
        packaging_result = verification.get("packaging_result") or {}
        if packaging_result.get("success", True):
            return None
        if not self.workspace or not state.project_id:
            return None

        project_root = self.workspace.get_project_path(state.project_id)
        pyproject_path = project_root / "pyproject.toml"
        root_main_path = project_root / "main.py"
        if not pyproject_path.exists() or not root_main_path.exists():
            return None

        try:
            pyproject_content = pyproject_path.read_text(encoding="utf-8", errors="replace")
            pyproject_data = _tomllib.loads(pyproject_content)
            root_main_content = root_main_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.debug("Common packaging repair read failed: %s", exc)
            return None

        artifacts: list[Artifact] = []
        changed_paths: set[str] = set()

        build_system = pyproject_data.get("build-system") or {}
        build_requires = [str(item).lower() for item in (build_system.get("requires") or [])]
        build_backend = (build_system.get("build-backend") or "").strip()
        backend_module = build_backend.split(":", 1)[0].strip() if build_backend else ""
        if (
            build_backend
            and backend_module
            and self._find_spec_safe(backend_module) is None
            and any("setuptools" in item for item in build_requires)
        ):
            replaced = _re.sub(
                r'(?m)^build-backend\s*=\s*".*"$',
                'build-backend = "setuptools.build_meta"',
                pyproject_content,
            )
            if replaced != pyproject_content:
                pyproject_content = replaced
                changed_paths.add("pyproject.toml")

        tool_data = pyproject_data.get("tool") or {}
        setuptools_data = tool_data.get("setuptools") or {}
        packages_data = setuptools_data.get("packages") or {}
        find_data = packages_data.get("find") or {}
        where_values = find_data.get("where") or []
        if isinstance(where_values, str):
            where_values = [where_values]

        module_root = project_root
        if where_values:
            module_root = (project_root / where_values[0]).resolve()

        scripts = (pyproject_data.get("project") or {}).get("scripts") or {}
        root_callable = self._guess_entrypoint_callable(root_main_path)
        for target in scripts.values():
            if not isinstance(target, str) or ":" not in target:
                continue

            module_name, callable_name = [item.strip() for item in target.split(":", 1)]
            module_parts = [part for part in module_name.split(".") if part]
            if not module_parts:
                continue

            module_file = module_root.joinpath(*module_parts).with_suffix(".py")
            package_module_init = module_root.joinpath(*module_parts, "__init__.py")
            package_init_paths = [
                module_root.joinpath(*module_parts[:index], "__init__.py")
                for index in range(1, len(module_parts))
            ]
            module_exists = module_file.exists() or package_module_init.exists()
            if module_exists:
                continue

            chosen_callable = callable_name if callable_name == root_callable else (root_callable or callable_name)
            if not chosen_callable:
                continue

            for init_path in package_init_paths:
                try:
                    relative_init = init_path.relative_to(project_root).as_posix()
                except ValueError:
                    continue
                if relative_init in changed_paths or init_path.exists():
                    continue
                artifacts.append(
                    Artifact(
                        name=init_path.name,
                        artifact_type="code",
                        content='"""Generated package for the executable entrypoint."""\n',
                        file_path=relative_init,
                        language="python",
                        metadata={
                            "repair_loop": "python_project_runtime",
                            "repair_attempt": attempt,
                            "repair_strategy": "packaging_package_init",
                        },
                    )
                )
                changed_paths.add(relative_init)

            try:
                relative_module = module_file.relative_to(project_root).as_posix()
            except ValueError:
                continue

            artifacts.append(
                Artifact(
                    name=module_file.name,
                    artifact_type="code",
                    content=root_main_content,
                    file_path=relative_module,
                    language="python",
                    metadata={
                        "repair_loop": "python_project_runtime",
                        "repair_attempt": attempt,
                        "repair_strategy": "packaging_module_alignment",
                    },
                )
            )
            changed_paths.add(relative_module)

            wrapper_content = self._build_root_entrypoint_wrapper(module_name, chosen_callable)
            artifacts.append(
                Artifact(
                    name="main.py",
                    artifact_type="code",
                    content=wrapper_content,
                    file_path="main.py",
                    language="python",
                    metadata={
                        "repair_loop": "python_project_runtime",
                        "repair_attempt": attempt,
                        "repair_strategy": "root_entrypoint_wrapper",
                    },
                )
            )
            changed_paths.add("main.py")

        if "pyproject.toml" in changed_paths:
            artifacts.append(
                Artifact(
                    name="pyproject.toml",
                    artifact_type="code",
                    content=pyproject_content,
                    file_path="pyproject.toml",
                    language="toml",
                    metadata={
                        "repair_loop": "python_project_runtime",
                        "repair_attempt": attempt,
                        "repair_strategy": "packaging_backend_fix",
                    },
                )
            )

        if not artifacts:
            return None

        return AgentResult(
            task_id=str(uuid.uuid4()),
            agent_name=self.coder.name,
            agent_role=AgentRole.CODER,
            content="Applied common packaging repair for generated Python project",
            artifacts=artifacts,
            success=True,
            metadata={
                "repair_loop": "python_project_runtime",
                "repair_attempt": attempt,
                "repair_strategy": "common_packaging_repair",
            },
        )

    def _repair_python_project_from_validation(
        self,
        state: OrchestrationState,
        failure: dict,
        attempt: int,
    ) -> AgentResult:
        python_files = self._collect_project_python_files(state)
        context_files = []
        if failure.get("entrypoint"):
            context_files.append(failure["entrypoint"])
        context_files.extend(python_files[:6])
        for config_path in ["requirements.txt", "pyproject.toml", "README.md"]:
            context_files.append(config_path)

        project_root = Path(state.metadata.get("project_root", "."))
        phase = failure.get("phase", "")

        prompt_lines = [
            "The generated Python project is not yet runnable.",
            f"Repair attempt: {attempt}",
            "",
            "Validation failure:",
            self._summarize_validation_failure(failure),
            "",
            "Current project files:",
            "\n".join(f"- {path}" for path in self._list_visible_project_files(project_root, limit=40)),
            "",
            "Current file contents:",
            self._build_project_file_context(state, context_files),
            "",
        ]

        # ── phase=entrypoint 전용 명시적 지시 ──────────────────────────────
        # 단순히 "프로젝트가 실행되지 않는다"고만 말하면 코더가 기존 파일을
        # 수정하려 시도할 수 있다. 엔트리포인트가 없는 경우에는 명시적으로
        # main.py 생성을 요구한다.
        if phase == "entrypoint":
            existing_py = failure.get("python_files") or python_files
            prompt_lines.extend([
                "*** CRITICAL: No executable Python entrypoint was found in the workspace. ***",
                "The existing Python files are helper/library/config files that CANNOT be entrypoints:",
                "\n".join(f"  - {f}" for f in existing_py[:8]),
                "",
                "You MUST create `main.py` at the project root with:",
                "  1. A `def main():` function that runs the actual program logic.",
                "  2. An `if __name__ == '__main__':` block at the bottom.",
                "  3. A `--smoke-test` argument that runs a quick self-test and exits with code 0.",
                "  4. Imports from the existing helper files listed above.",
                "",
                "Do NOT modify the existing helper files as the only output.",
                "Do NOT return only a plan or description.",
                "Return complete file contents starting with `File: main.py`.",
                "",
            ])

        prompt_lines.extend([
            "Return only corrected file contents with explicit relative paths.",
            "Requirements:",
            "- Guarantee a real executable Python entrypoint in main.py unless __main__.py/manage.py is clearly more conventional.",
            "- `python main.py --smoke-test` must exit with code 0.",
            "- The smoke-test path must exercise the real application wiring and then exit cleanly.",
            "- Fix missing local modules, imports, and runtime errors shown above.",
            "- Make package/module layout and pyproject/setup entrypoints resolve to real files and callables.",
            "- If you introduce or keep external dependencies, update requirements.txt or pyproject.toml consistently.",
            "- CRITICAL — Prevent self-import circular imports:",
            "  * `main.py` must NEVER contain `from main import ...` or `import main`.",
            "  * No file should import from its own module name (e.g. `analyze_data.py` must not contain `from analyze_data import ...`).",
            "  * Design: main.py = thin runner that imports from library modules; library modules have no __main__ blocks.",
        ])
        prompt = "\n".join(prompt_lines)

        content = self.coder.generate(prompt)
        artifacts = self.coder._parse_code_response(content)
        valid, error = self._validate_coder_output(content, artifacts)
        return AgentResult(
            task_id=str(uuid.uuid4()),
            agent_name=self.coder.name,
            agent_role=AgentRole.CODER,
            content=content,
            artifacts=artifacts,
            success=valid,
            error=error,
            metadata={
                "repair_loop": "python_project_runtime",
                "repair_attempt": attempt,
            },
        )

    def _request_missing_python_implementation(
        self,
        state: OrchestrationState,
        attempt: int,
        workspace_files: list[str],
    ) -> AgentResult:
        """[검증 게이트] Python 구현 파일이 없을 때 코더에게 직접 생성을 요청한다.

        ─── 0 Python files 특수 처리 ────────────────────────────────────────
        일반 repair 프롬프트와 달리, 이 메서드는:
        1. 코더가 "설명" 이나 "계획" 이 아닌 실제 .py 파일을 즉시 생성해야 함을 명시한다.
        2. main.py 의 최소 요건(실행 가능한 엔트리포인트, smoke-test 통과)을 구체적으로 요청한다.
        3. 워크스페이스의 현재 상태(비 Python 파일 목록)를 컨텍스트로 전달한다.
        ────────────────────────────────────────────────────────────────────
        """
        original_task = state.original_task
        project_root = Path(state.metadata.get("project_root", "."))

        # 워크스페이스에 있는 비-Python 파일을 컨텍스트로 제공
        existing_context = self._build_project_file_context(
            state,
            [f for f in workspace_files if not f.endswith(".py")][:5],
        )

        prompt_parts = [
            f"CRITICAL: The project workspace has NO Python (.py) source files.",
            f"This is generation attempt {attempt}.",
            "",
            f"Original task: {original_task}",
            "",
            "The workspace currently contains only these files (no Python source):",
            "\n".join(f"  - {f}" for f in workspace_files[:20]) or "  (empty)",
            "",
        ]
        if existing_context:
            prompt_parts.extend([
                "Existing non-Python file contents for reference:",
                existing_context[:1500],
                "",
            ])
        prompt_parts.extend([
            "YOU MUST NOW generate actual Python source files.",
            "Requirements (MANDATORY):",
            "  1. Create `main.py` as the executable entrypoint.",
            "  2. `main.py` must contain `if __name__ == '__main__':` block.",
            "  3. `python main.py --smoke-test` must exit with code 0.",
            "  4. Add a `--smoke-test` argument that runs a quick self-test and exits cleanly.",
            "  5. Return complete file contents with explicit relative paths (e.g. `File: main.py`).",
            "  6. Do NOT return only documentation, plans, file trees, or markdown without code.",
            "  7. Every file you return must be complete Python source that can be saved and run.",
            "  8. CRITICAL — Avoid self-import circular imports:",
            "     - `main.py` must NEVER contain `from main import ...` or `import main`.",
            "     - No implementation file (e.g. `analyze_data.py`) should import from its own module name.",
            "     - Separate concerns: main.py = thin runner, implementation files = library modules.",
            "     - If analyze_data.py defines classes, main.py imports them: `from analyze_data import MyClass`.",
            "     - analyze_data.py must NOT contain `from analyze_data import ...`.",
            "",
            "Minimum required files to return:",
            "  - main.py  (must be the primary executable entrypoint, no self-imports)",
            "  - Any supporting modules as needed (library modules, no __main__ blocks)",
            "",
            "Output format (repeat for each file):",
            "File: <relative_path>",
            "```python",
            "# complete file contents here",
            "```",
        ])

        prompt = "\n".join(prompt_parts)
        content = self.coder.generate(prompt)
        artifacts = self.coder._parse_code_response(content)

        # 이 경우에는 엄격한 blueprint 검사를 적용하되,
        # 결과가 없어도 soft-fail (success=False) 만 반환하고 예외는 내지 않는다.
        valid, error = self._validate_coder_output(content, artifacts)

        logger.info(
            "[0 Python files 수정 요청] attempt=%d, artifacts=%d, valid=%s",
            attempt,
            len(artifacts),
            valid,
        )

        return AgentResult(
            task_id=str(uuid.uuid4()),
            agent_name=self.coder.name,
            agent_role=AgentRole.CODER,
            content=content,
            artifacts=artifacts,
            success=valid,
            error=error if not valid else None,
            metadata={
                "repair_loop": "missing_python_implementation",
                "repair_attempt": attempt,
                "repair_strategy": "create_from_scratch",
            },
        )

    def _repair_self_import_entrypoint(
        self,
        state: OrchestrationState,
        failure: dict,
        attempt: int,
    ) -> AgentResult:
        """[self-import 수정] 엔트리포인트가 자신을 import할 때 main.py 구조 분리를 요청한다.

        ─── 문제 패턴 ────────────────────────────────────────────────────────────
        analyze_data.py:
            from analyze_data import AnalysisEngine   # ← self-import!
            if __name__ == '__main__': ...

        실행: python analyze_data.py
            → Python이 `analyze_data` 모듈을 import 시도
            → 이미 실행 중인 analyze_data.py를 다시 로드
            → circular import 오류

        ─── 수정 전략 ────────────────────────────────────────────────────────────
        1. 기존 파일(analyze_data.py)에서 self-import 줄 제거
        2. 새로운 main.py를 작성해 analyze_data 모듈을 올바르게 import
        3. main.py가 --smoke-test 지원하는 진입점이 됨
        ────────────────────────────────────────────────────────────────────────
        """
        entrypoint = failure.get("entrypoint", "")
        module_name = Path(entrypoint).stem if entrypoint else "module"
        self_imports = failure.get("self_imports") or []

        # 현재 엔트리포인트 파일 내용 읽기 (컨텍스트 제공용)
        entrypoint_content = ""
        if self.workspace and state.project_id and entrypoint:
            project_root = self.workspace.get_project_path(state.project_id)
            try:
                entrypoint_content = (project_root / entrypoint).read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

        python_files = self._collect_project_python_files(state)
        file_context = self._build_project_file_context(state, python_files[:5])

        prompt_parts = [
            f"CRITICAL BUG: Self-import circular import detected in the project entrypoint.",
            f"",
            f"Problem:",
            f"  File `{entrypoint}` (Python module name: `{module_name}`) contains these self-import lines:",
            "\n".join(f"    {s}" for s in self_imports),
            f"",
            f"  When Python executes `python {entrypoint}`, it encounters `from {module_name} import ...`",
            f"  and tries to import the `{module_name}` module — which IS the file being run.",
            f"  This causes: ImportError: cannot import name '...' from partially initialized module",
            f"",
            f"REQUIRED FIX (two files must be returned):",
            f"",
            f"  1. Fix `{entrypoint}` — REMOVE all self-import lines (`from {module_name} import ...`).",
            f"     The file should contain only the class/function definitions, NO `if __name__` block.",
            f"     It is a LIBRARY MODULE, not a runner.",
            f"",
            f"  2. Create a NEW `main.py` at the project root that:",
            f"     - imports from `{module_name}` (the fixed library module)",
            f"     - has `def main():` function that runs the program",
            f"     - has `if __name__ == '__main__': raise SystemExit(main())`",
            f"     - supports `--smoke-test` argument that exits with code 0",
            f"",
            f"  RULE: `main.py` must NEVER contain `from main import ...` or `import main`.",
            f"  RULE: No file should import from its own module name.",
            f"",
            f"Original content of `{entrypoint}` (repair attempt {attempt}):",
            entrypoint_content[:2000],
            f"",
            f"Other project files for context:",
            file_context[:1500],
            f"",
            f"Return format (one block per file):",
            f"File: <relative_path>",
            f"```python",
            f"# complete file contents",
            f"```",
        ]

        prompt = "\n".join(prompt_parts)
        content = self.coder.generate(prompt)
        artifacts = self.coder._parse_code_response(content)
        valid, error = self._validate_coder_output(content, artifacts)

        logger.info(
            "[self-import 수정 요청] attempt=%d, artifacts=%d, valid=%s, entrypoint=%s",
            attempt,
            len(artifacts),
            valid,
            entrypoint,
        )

        return AgentResult(
            task_id=str(uuid.uuid4()),
            agent_name=self.coder.name,
            agent_role=AgentRole.CODER,
            content=content,
            artifacts=artifacts,
            success=valid,
            error=error if not valid else None,
            metadata={
                "repair_loop": "self_import_fix",
                "repair_attempt": attempt,
                "repair_strategy": "separate_main_from_module",
                "original_entrypoint": entrypoint,
                "self_imports": self_imports,
            },
        )

    def _format_python_project_validation_report(self, metadata: dict) -> str:
        lines = [
            "## Python Project Validation",
            f"- success: {metadata.get('success')}",
            f"- entrypoint: {metadata.get('entrypoint') or 'missing'}",
            f"- attempts: {metadata.get('attempt_count', 0)}",
            f"- final verification: {metadata.get('final_summary') or 'not completed'}",
        ]
        failure_summary = metadata.get("failure_summary")
        if failure_summary:
            lines.extend(["", "### Failure", failure_summary[:3000]])

        attempt_logs = metadata.get("attempt_logs") or []
        if attempt_logs:
            lines.extend(["", "### Attempts"])
            for item in attempt_logs:
                status = "ok" if item.get("success") else "failed"
                detail = item.get("detail") or ""
                lines.append(f"- attempt {item.get('attempt')}: {item.get('phase')} [{status}] {detail}")
        return "\n".join(lines)

    def _run_python_project_validation_loop(self, state: OrchestrationState) -> AgentResult:
        # 생성 완료 조건은 "파일이 생겼는가"가 아니라 "실제로 실행되고 최종 검증을 통과했는가"다.
        # 실패하면 stdout/stderr를 다시 코더에게 넘겨 고치고, 성공할 때까지 실행-수정-재실행을 반복한다.
        if not self.tester:
            metadata = {
                "success": False,
                "entrypoint": None,
                "attempt_count": 0,
                "attempt_logs": [],
                "final_summary": "tester agent unavailable",
                "failure_summary": "tester agent unavailable",
                "blocking": True,
            }
            state.metadata["project_validation"] = metadata
            return AgentResult(
                task_id=str(uuid.uuid4()),
                agent_name="Tester",
                agent_role=AgentRole.TESTER,
                content=self._format_python_project_validation_report(metadata),
                success=False,
                error="tester agent unavailable",
                metadata=metadata,
            )

        attempt_logs = []
        max_attempts = max(2, min(self.max_iterations, 5))
        last_failure_summary = ""
        last_entrypoint: Optional[str] = None
        last_failure: Optional[dict] = None

        for attempt in range(1, max_attempts + 1):
            self._prepare_generated_project_artifacts(state)
            self._save_artifacts(state)

            python_files = self._collect_project_python_files(state)

            # ── [E] 파일/패키지 이름 충돌 검사 ────────────────────────────────
            # data_processor.py 와 data_processor/ 디렉토리 동시 존재 등을 감지해
            # 실행 전에 경고한다. 충돌이 있으면 이후 repair 단계에서 컨텍스트로 제공한다.
            file_pkg_conflicts = self._detect_file_package_conflicts(state, python_files)
            if file_pkg_conflicts and attempt == 1:
                for conflict in file_pkg_conflicts[:3]:
                    self._notify(f"[파일/패키지 충돌] {conflict['message']}", "tester")

            # ── [검증 게이트] Python 파일 존재 사전 검사 ──────────────────────
            # 엔트리포인트 탐색 전에 Python 파일이 하나라도 있는지 확인한다.
            # 없으면 이미 엔트리포인트 탐색이 실패할 것이 자명하므로, 즉시 코더에게
            # Python 소스 파일 생성을 명시적으로 요청한다.
            # "README.md 만 있는 워크스페이스"를 완료 상태로 잘못 처리하는 것을 방지한다.
            if not python_files:
                if self.workspace and state.project_id:
                    project_root_path = self.workspace.get_project_path(state.project_id)
                    all_workspace_files = self._list_visible_project_files(project_root_path, limit=50)
                else:
                    all_workspace_files = []

                diagnosis = (
                    f"Python 구현 파일(.py)이 워크스페이스에 없습니다 (attempt {attempt}). "
                    f"워크스페이스 전체 파일 {len(all_workspace_files)}개: {all_workspace_files[:20]}. "
                    f"저장된 아티팩트: {list((state.metadata.get('saved_artifacts') or {}).keys())[:10]}."
                )
                logger.warning("[검증 게이트] 0 Python files — %s", diagnosis)
                self._notify(
                    f"[검증 게이트] Python 파일 없음 (attempt {attempt}/{max_attempts}) "
                    f"— 코더에게 구현 파일 생성 요청",
                    "tester",
                )

                failure = {
                    "phase": "no_python_files",
                    "python_files": [],
                    "actual_workspace_files": all_workspace_files,
                    "diagnosis": diagnosis,
                }
                last_failure = failure
                last_failure_summary = self._summarize_validation_failure(failure)
                attempt_logs.append({
                    "attempt": attempt,
                    "phase": "no_python_files",
                    "success": False,
                    "detail": diagnosis[:300],
                })

                if attempt >= max_attempts:
                    break

                # 코더에게 Python 구현 파일 생성을 명시적으로 요청
                repair_result = self._request_missing_python_implementation(
                    state, attempt, all_workspace_files
                )
                self._store_result(state, repair_result)
                attempt_logs.append({
                    "attempt": attempt,
                    "phase": "repair_missing_python",
                    "success": repair_result.success,
                    "detail": repair_result.error or ", ".join(
                        a.file_path or a.name for a in repair_result.artifacts[:5]
                    ),
                })
                if repair_result.success:
                    # 아티팩트 즉시 저장 후 다음 iteration 에서 다시 탐색
                    self._prepare_generated_project_artifacts(state)
                    saved_now = self._save_step_artifacts(state, repair_result)
                    if saved_now:
                        self._notify(
                            f"[즉시 저장] Python 파일 {len(saved_now)}개: {', '.join(saved_now[:3])}",
                            "system",
                        )
                else:
                    last_failure_summary = repair_result.error or "Python 구현 파일 생성 응답이 유효하지 않습니다"
                continue

            # ── [검증 게이트-2] 에이전트가 새 Python 구현 파일을 생성하지 않은 경우 ─────
            # 초기 워크스페이스에 이미 있던 Python 파일(config.py 등)만 남아있고
            # 에이전트가 새로운 Python 파일을 전혀 생성하지 않은 경우.
            # 예: 워크스페이스에 config.py + 데이터 파일만 있고 에이전트가 main.py를 만들지 않은 경우.
            # 이 경우는 0-Python-files와 사실상 동일하므로 "처음부터 생성" 경로를 사용한다.
            initial_python_files_set = set(
                f for f in (state.metadata.get("initial_visible_files") or [])
                if f.endswith(".py")
            )
            new_python_files = [f for f in python_files if f not in initial_python_files_set]
            if not new_python_files and initial_python_files_set:
                if self.workspace and state.project_id:
                    project_root_path = self.workspace.get_project_path(state.project_id)
                    all_workspace_files = self._list_visible_project_files(project_root_path, limit=50)
                else:
                    all_workspace_files = list(python_files)

                diagnosis = (
                    f"에이전트가 새 Python 구현 파일을 생성하지 않았습니다 (attempt {attempt}). "
                    f"초기 Python 파일({len(initial_python_files_set)}개): "
                    f"{sorted(initial_python_files_set)[:8]}. "
                    f"이 파일들은 config/helper 파일로 엔트리포인트가 될 수 없습니다. "
                    f"코더가 main.py 를 포함한 실제 구현 파일을 생성해야 합니다."
                )
                logger.warning("[검증 게이트-2] 새 Python 파일 없음 (초기 파일만 있음) — %s", diagnosis)
                self._notify(
                    f"[검증 게이트-2] 새 구현 파일 없음 (attempt {attempt}/{max_attempts}) "
                    f"— 코더에게 main.py 생성 요청",
                    "tester",
                )

                failure = {
                    "phase": "no_new_python_files",
                    "python_files": python_files,
                    "initial_python_files": sorted(initial_python_files_set),
                    "actual_workspace_files": all_workspace_files,
                    "diagnosis": diagnosis,
                }
                last_failure = failure
                last_failure_summary = self._summarize_validation_failure(failure)
                attempt_logs.append({
                    "attempt": attempt,
                    "phase": "no_new_python_files",
                    "success": False,
                    "detail": diagnosis[:300],
                })

                if attempt >= max_attempts:
                    break

                repair_result = self._request_missing_python_implementation(
                    state, attempt, all_workspace_files
                )
                self._store_result(state, repair_result)
                attempt_logs.append({
                    "attempt": attempt,
                    "phase": "repair_missing_python",
                    "success": repair_result.success,
                    "detail": repair_result.error or ", ".join(
                        a.file_path or a.name for a in repair_result.artifacts[:5]
                    ),
                })
                if repair_result.success:
                    self._prepare_generated_project_artifacts(state)
                    saved_now = self._save_step_artifacts(state, repair_result)
                    if saved_now:
                        self._notify(
                            f"[즉시 저장] Python 파일 {len(saved_now)}개: {', '.join(saved_now[:3])}",
                            "system",
                        )
                else:
                    last_failure_summary = repair_result.error or "Python 구현 파일 생성 응답이 유효하지 않습니다"
                continue

            entrypoint = self._discover_python_entrypoint(state)
            last_entrypoint = entrypoint

            if not entrypoint:
                # ── [F] phase=entrypoint 실패: 상세 진단 정보 수집 ──────────
                # 단순한 "엔트리포인트 없음" 메시지 대신, 실제 워크스페이스 상태와
                # 가능한 원인을 포함하여 코더가 정확히 무엇을 고쳐야 하는지 알 수 있게 한다.
                if self.workspace and state.project_id:
                    project_root = self.workspace.get_project_path(state.project_id)
                    actual_workspace_files = self._list_visible_project_files(project_root, limit=50)
                else:
                    actual_workspace_files = python_files

                diagnosis = (
                    f"엔트리포인트 미발견. "
                    f"스캔한 Python 파일 {len(python_files)}개: {python_files[:8]}. "
                    f"워크스페이스 전체 파일 {len(actual_workspace_files)}개: {actual_workspace_files[:15]}. "
                    f"저장된 아티팩트: {list((state.metadata.get('saved_artifacts') or {}).keys())[:10]}."
                )
                logger.warning("phase=entrypoint 실패: %s", diagnosis)

                # ── [self-import 원인 탐색] 엔트리포인트 탐색 실패가 self-import 때문인지 확인 ──
                # 스테이지 3에서 모든 후보가 self-import로 제외되면 entrypoint=None이 반환된다.
                # 이 경우 generic entrypoint 수정 대신 self-import 전용 수리를 사용한다.
                self_import_culprits: list[tuple[str, list[str]]] = []
                if self.workspace and state.project_id and python_files:
                    _proj_root_for_check = self.workspace.get_project_path(state.project_id)
                    for _py_file in python_files:
                        _si = self._check_self_import(_py_file, _proj_root_for_check)
                        if _si:
                            self_import_culprits.append((_py_file, _si))

                if self_import_culprits:
                    # 엔트리포인트가 없는 이유가 self-import 때문 → 전용 수리 경로 사용
                    culprit_path, culprit_imports = self_import_culprits[0]
                    _si_diagnosis = (
                        f"self-import 버그로 인해 엔트리포인트를 선택할 수 없습니다. "
                        f"`{culprit_path}`이 자신의 모듈 이름으로 import합니다: {culprit_imports}. "
                        f"수정: main.py를 분리된 진입점으로 생성하고, {culprit_path}에서 self-import를 제거하세요."
                    )
                    logger.warning("[self-import 원인] entrypoint=None 이유: %s", _si_diagnosis)
                    self._notify(
                        f"[self-import 버그] {culprit_path} → {culprit_imports} — main.py 분리 필요",
                        "tester",
                    )
                    failure = {
                        "phase": "self_import",
                        "entrypoint": culprit_path,
                        "python_files": python_files,
                        "self_imports": culprit_imports,
                        "diagnosis": _si_diagnosis,
                    }
                    last_failure = failure
                    last_failure_summary = self._summarize_validation_failure(failure)
                    attempt_logs.append({
                        "attempt": attempt,
                        "phase": "self_import",
                        "success": False,
                        "detail": _si_diagnosis[:300],
                    })
                    if attempt < max_attempts:
                        repair_result = self._repair_self_import_entrypoint(state, failure, attempt)
                        self._store_result(state, repair_result)
                        attempt_logs.append({
                            "attempt": attempt,
                            "phase": "repair_self_import",
                            "success": repair_result.success,
                            "detail": repair_result.error or ", ".join(
                                a.file_path or a.name for a in repair_result.artifacts[:5]
                            ),
                        })
                        if repair_result.success:
                            self._prepare_generated_project_artifacts(state)
                            saved_now = self._save_step_artifacts(state, repair_result)
                            if saved_now:
                                self._notify(
                                    f"[self-import 수정] {len(saved_now)}개 파일: {', '.join(saved_now[:3])}",
                                    "system",
                                )
                        else:
                            last_failure_summary = repair_result.error or "self-import 수정 실패"
                    continue

                failure = {
                    "phase": "entrypoint",
                    "python_files": python_files,
                    "actual_workspace_files": actual_workspace_files,
                    "diagnosis": diagnosis,
                }
                last_failure = failure
                last_failure_summary = self._summarize_validation_failure(failure)
                attempt_logs.append(
                    {
                        "attempt": attempt,
                        "phase": "entrypoint",
                        "success": False,
                        "detail": diagnosis[:300],
                    }
                )
            else:
                # ── [self-import 사전 검사] 실행 전에 엔트리포인트가 자신을 import하는지 확인 ──
                # 스테이지 3에서 걸러지더라도, main.py 같은 1단계 후보가 self-import를 가질 수 있다.
                # 예: main.py 가 `from main import X` 를 포함하는 경우.
                if self.workspace and state.project_id:
                    _proj_root = self.workspace.get_project_path(state.project_id)
                    _self_imports = self._check_self_import(entrypoint, _proj_root)
                    if _self_imports:
                        _diagnosis = (
                            f"self-import 버그: `{entrypoint}` 파일이 자신의 모듈 이름으로 import합니다: "
                            f"{_self_imports}. "
                            f"python {entrypoint} 실행 시 circular import 오류가 발생합니다. "
                            f"엔트리포인트는 자신의 모듈 이름('{Path(entrypoint).stem}')으로 import하면 안 됩니다."
                        )
                        logger.warning("[self-import 사전 검사] %s", _diagnosis)
                        self._notify(
                            f"[self-import 버그] {entrypoint} → {_self_imports} — main.py 구조 분리 필요",
                            "tester",
                        )
                        failure = {
                            "phase": "self_import",
                            "entrypoint": entrypoint,
                            "python_files": python_files,
                            "self_imports": _self_imports,
                            "diagnosis": _diagnosis,
                        }
                        last_failure = failure
                        last_failure_summary = self._summarize_validation_failure(failure)
                        attempt_logs.append({
                            "attempt": attempt,
                            "phase": "self_import",
                            "success": False,
                            "detail": _diagnosis[:300],
                        })
                        if attempt < max_attempts:
                            repair_result = self._repair_self_import_entrypoint(state, failure, attempt)
                            self._store_result(state, repair_result)
                            attempt_logs.append({
                                "attempt": attempt,
                                "phase": "repair_self_import",
                                "success": repair_result.success,
                                "detail": repair_result.error or ", ".join(
                                    a.file_path or a.name for a in repair_result.artifacts[:5]
                                ),
                            })
                            if repair_result.success:
                                self._prepare_generated_project_artifacts(state)
                                saved_now = self._save_step_artifacts(state, repair_result)
                                if saved_now:
                                    self._notify(
                                        f"[self-import 수정] {len(saved_now)}개 파일: {', '.join(saved_now[:3])}",
                                        "system",
                                    )
                            else:
                                last_failure_summary = repair_result.error or "self-import 수정 실패"
                        continue

                execution = self.tester.run_python_smoke_test(
                    entrypoint,
                    timeout=30,
                    smoke_args=["--smoke-test"],
                )
                attempt_logs.append(
                    {
                        "attempt": attempt,
                        "phase": "smoke-run",
                        "success": execution.get("success", False),
                        "detail": execution.get("command", ""),
                    }
                )
                if execution.get("success", False):
                    verification = self.tester.final_verify_python_project(
                        entrypoint,
                        python_files,
                        has_tests=self._has_project_tests(python_files),
                        smoke_args=["--smoke-test"],
                    )
                    attempt_logs.append(
                        {
                            "attempt": attempt,
                            "phase": "final-verification",
                            "success": verification.get("success", False),
                            "detail": verification.get("summary", ""),
                        }
                    )
                    if verification.get("success", False):
                        metadata = {
                            "success": True,
                            "entrypoint": entrypoint,
                            "attempt_count": attempt,
                            "attempt_logs": attempt_logs,
                            "final_summary": verification.get("summary", ""),
                            "verification": verification,
                            "blocking": True,
                        }
                        state.metadata["project_validation"] = metadata
                        return AgentResult(
                            task_id=str(uuid.uuid4()),
                            agent_name=self.tester.name,
                            agent_role=AgentRole.TESTER,
                            content=self._format_python_project_validation_report(metadata),
                            success=True,
                            metadata=metadata,
                        )

                    failure = {
                        "phase": "final_verification",
                        "entrypoint": entrypoint,
                        "python_files": python_files,
                        "verification": verification,
                    }
                else:
                    failure = {
                        "phase": "execution",
                        "entrypoint": entrypoint,
                        "python_files": python_files,
                        "execution": execution,
                    }

                last_failure = failure
                last_failure_summary = self._summarize_validation_failure(failure)

            # 재시도는 실제 실행 실패를 근거로 한 코더 수정 요청이어야 한다.
            if attempt >= max_attempts:
                break

            repair_input = last_failure or {
                "phase": "repair_input",
                "entrypoint": last_entrypoint,
                "python_files": self._collect_project_python_files(state),
                "execution": {"stderr": last_failure_summary},
            }
            repair_result = self._maybe_apply_common_runtime_repair(state, repair_input, attempt)
            if not repair_result:
                repair_result = self._maybe_apply_common_packaging_repair(state, repair_input, attempt)
            if not repair_result:
                repair_result = self._repair_python_project_from_validation(
                    state,
                    repair_input,
                    attempt,
                )
            self._store_result(state, repair_result)
            attempt_logs.append(
                {
                    "attempt": attempt,
                    "phase": "repair",
                    "success": repair_result.success,
                    "detail": repair_result.error or ", ".join(
                        artifact.file_path or artifact.name for artifact in repair_result.artifacts[:5]
                    ),
                }
            )
            if not repair_result.success:
                last_failure_summary = repair_result.error or "repair response was not usable"

        metadata = {
            "success": False,
            "entrypoint": last_entrypoint,
            "attempt_count": max_attempts,
            "attempt_logs": attempt_logs,
            "final_summary": "runtime validation failed",
            "failure_summary": last_failure_summary,
            "blocking": True,
        }
        state.metadata["project_validation"] = metadata
        return AgentResult(
            task_id=str(uuid.uuid4()),
            agent_name=self.tester.name if self.tester else "Tester",
            agent_role=AgentRole.TESTER,
            content=self._format_python_project_validation_report(metadata),
            success=False,
            error=last_failure_summary,
            metadata=metadata,
        )

    def _enforce_completion_criteria(self, state: OrchestrationState) -> None:
        # 새 Python 프로젝트는 실제 실행 검증이 성공해야만 생성 완료로 간주한다.
        if not self._is_python_project_workflow(state):
            return

        validation = state.metadata.get("project_validation") or {}
        if not validation:
            raise RuntimeError("generated Python project was not validated")
        if not validation.get("success"):
            raise RuntimeError(validation.get("failure_summary") or "generated Python project failed validation")

    def _validate_coder_output(self, content: str, artifacts: list[Artifact]) -> tuple[bool, str]:
        if not artifacts:
            return False, "코더가 저장 가능한 코드 산출물을 만들지 못했습니다."

        if self._looks_like_blueprint_response(content, artifacts):
            return False, "코더가 실제 구현 대신 설계 문서/의사코드를 반환했습니다."

        return True, ""

    def _looks_like_blueprint_response(self, content: str, artifacts: list[Artifact]) -> bool:
        import re as _re

        lowered = (content or "").lower()
        blueprint_terms = [
            # English terms
            "pseudo-code",
            "pseudo code",
            "project blueprint",
            "executive summary",
            "action plan",
            "phase 1",
            "phase 2",
            "deliverable",
            "architecture",
            "roadmap",
            "implementation plan",
            "project structure",
            "directory structure",
            "file structure",
            # Korean terms
            "의사코드",
            "청사진",
            "아키텍처 설계",
            "구현 계획",
            "단계별 계획",
            "실행 계획",
            "파일 구조",
            "디렉토리 구조",
            "프로젝트 구조",
            "폴더 구조",
        ]
        tree_markers = [
            "├──", "└──", "│",   # Unicode box-drawing (UTF-8 encoded)
            "|--", "+--", "`--",  # ASCII tree alternatives
            # "### " 와 "## " 는 여기서 제거함 — 정상적인 코드 응답에도 마크다운 헤더가 포함될 수 있으며,
            # 이들이 tree_markers 에 있으면 유효한 코드 블록이 청사진으로 잘못 분류된다.
            # 대신 blueprint_terms 리스트에 구체적인 청사진 표현을 추가한다.
        ]
        has_blueprint_markers = any(term in lowered for term in blueprint_terms) or any(
            marker in content for marker in tree_markers
        )

        # Detect directory-tree lines: lines that consist of only a name ending with "/"
        # (e.g. "src/" or "  data/") — encoding-safe, no Unicode box chars needed
        if not has_blueprint_markers:
            dir_line_count = sum(
                1
                for line in (content or "").splitlines()
                if _re.match(r"^\s*[\w\-\.]+/\s*$", line)
            )
            if dir_line_count >= 3:
                has_blueprint_markers = True

        total_artifact_chars = sum(len(artifact.content or "") for artifact in artifacts)
        explicit_paths = any(
            artifact.metadata.get("has_explicit_path") or artifact.file_path for artifact in artifacts
        )
        generic_only = all(self._is_generic_artifact_name(artifact.name or "") for artifact in artifacts)

        if has_blueprint_markers and generic_only and not explicit_paths:
            return True

        # Only apply the size-ratio check when no explicit file paths are provided.
        # Legitimate code files with explicit paths are always trusted regardless of size.
        if has_blueprint_markers and not explicit_paths and total_artifact_chars < max(1200, int(len(content or "") * 0.6)):
            return True

        # Detect raw-response artifacts (no code blocks were found in the LLM output).
        # When the entire response is dumped as a single artifact the size-ratio check
        # above never fires (ratio = 100 %).  Apply a separate heuristic: if there are no
        # code-syntax indicators the response is prose/blueprint, not implementation.
        all_raw = all(artifact.metadata.get("source") == "raw_response" for artifact in artifacts)
        if all_raw and generic_only and not explicit_paths:
            real_code_patterns = [
                "def ", "class ", "import ", "from ",          # Python
                "function ", "const ", "var ", "let ", "=>",   # JS/TS
                "public ", "private ", "void ", "#include",    # Java / C / C++
                "func ", "package ",                            # Go
                "fn ", "use ",                                  # Rust
            ]
            has_real_code = any(p in content for p in real_code_patterns)
            if not has_real_code or has_blueprint_markers:
                return True

        return False

    def _normalize_relative_path(self, path_value: str) -> str:
        normalized = (path_value or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    def _relative_saved_path(self, project_id: str, saved_path: str) -> str:
        if not self.workspace:
            return saved_path

        try:
            project_root = self.workspace.get_project_path(project_id).resolve()
            return Path(saved_path).resolve().relative_to(project_root).as_posix()
        except Exception:
            return str(Path(saved_path))

    def _should_use_web_search(self, user_task: str, task_analysis: dict) -> bool:
        if task_analysis.get("needs_internet_search"):
            return True

        keywords = [
            "최신",
            "최근",
            "인터넷",
            "검색",
            "공식 문서",
            "업데이트",
            "release",
            "latest",
            "news",
            "search the web",
        ]
        lowered = user_task.lower()
        return any(keyword in user_task or keyword in lowered for keyword in keywords)

    def _save_session_log(self, state: OrchestrationState) -> None:
        if not self.workspace or not state.project_id:
            return

        lines = [
            "# Session log",
            f"session_id: {state.session_id}",
            f"task: {state.original_task}",
            f"status: {state.status.value}",
            f"started_at: {state.started_at}",
            f"completed_at: {state.completed_at or 'N/A'}",
            "",
            "## Agent activity",
        ]
        for result in state.results:
            lines.append(f"\n### {result.agent_role.value}")
            lines.append(f"- success: {result.success}")
            lines.append(f"- duration_ms: {result.duration_ms:.0f}")
            lines.append(f"- content: {result.content[:200]}...")
            if result.artifacts:
                lines.append(f"- artifacts: {[artifact.name for artifact in result.artifacts]}")

        self.workspace.save_log(state.project_id, "\n".join(lines))

    def _log_step(self, state: OrchestrationState, step_name: str, content: str) -> None:
        record = TaskRecord(
            task_id=str(uuid.uuid4()),
            session_id=state.session_id,
            project_id=state.project_id or "",
            title=step_name,
            description=state.original_task[:200],
            assigned_to="system",
            status="completed",
            result_summary=content[:500],
            artifacts=[],
            duration_ms=0.0,
            created_at=datetime.now().isoformat(),
        )
        self.history.add(record)

    def _notify(self, message: str, agent: str = "system") -> None:
        logger.info("[%s] %s", agent, message)
        if self._on_status:
            self._on_status(agent, message)

    def get_current_state(self) -> Optional[OrchestrationState]:
        return self._current_state
