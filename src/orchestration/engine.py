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

        initial_files = state.metadata.get("initial_visible_files") or []
        changed_files = self._collect_changed_files_from_artifacts(state)
        return not initial_files and any(path.endswith(".py") for path in changed_files)

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
        """Run the full multi-agent workflow."""
        start_time = time.time()
        state = OrchestrationState(
            original_task=user_task,
            status=TaskStatus.IN_PROGRESS,
        )
        self._current_state = state

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

                # ──────────────────────────────────────────
                # 스텝 재시도 루프 (Step Retry Loop)
                # 일시적 실패는 STEP_RETRY_POLICY 에 따라 재시도합니다.
                # 치명적 오류 또는 최대 시도 초과 시에만 전체 오케스트레이션을 중단합니다.
                # ──────────────────────────────────────────
                step_result = self._execute_step_with_retry(state, step, STEP_RETRY_POLICY)

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
                    self._store_result(state, self._run_document_agent(state, user_task, refreshed_code_results))
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
        if not self.workspace or not state.project_id:
            return

        saved_count = 0
        for result in state.results:
            if not result.success:
                continue
            for artifact in result.artifacts:
                if not self._should_persist_artifact(state, result, artifact):
                    continue
                artifact_content = self._hydrate_artifact_content(state, artifact)
                if not artifact_content:
                    continue

                target_path = self._artifact_target_path(artifact)
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
                artifact.file_path = self._relative_saved_path(state.project_id, saved_path)
                saved_count += 1

        logger.info("Saved %s artifacts", saved_count)

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

    def _has_project_tests(self, python_files: list[str]) -> bool:
        return any(path.startswith("tests/") or Path(path).name.startswith("test_") for path in python_files)

    def _discover_python_entrypoint(self, state: OrchestrationState) -> Optional[str]:
        python_files = self._collect_project_python_files(state)
        for candidate in ["main.py", "src/main.py", "__main__.py"]:
            if candidate in python_files:
                return candidate
        for path in python_files:
            if path.endswith("/__main__.py") or Path(path).name == "manage.py":
                return path
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
        phase = failure.get("phase", "validation")
        parts = [f"phase={phase}"]

        if failure.get("entrypoint"):
            parts.append(f"entrypoint={failure['entrypoint']}")

        python_files = failure.get("python_files") or []
        if python_files:
            parts.append(f"python_files={', '.join(python_files[:8])}")

        execution = failure.get("execution") or {}
        if execution:
            parts.append(f"command={execution.get('command', '')}")
            stdout = (execution.get("stdout") or "").strip()
            stderr = (execution.get("stderr") or "").strip()
            if stdout:
                parts.append(f"stdout={stdout[:600]}")
            if stderr:
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
        prompt = "\n".join(
            [
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
                "Return only corrected file contents with explicit relative paths.",
                "Requirements:",
                "- Guarantee a real executable Python entrypoint in main.py unless __main__.py/manage.py is clearly more conventional.",
                "- `python main.py --smoke-test` must exit with code 0.",
                "- The smoke-test path must exercise the real application wiring and then exit cleanly.",
                "- Fix missing local modules, imports, and runtime errors shown above.",
                "- Make package/module layout and pyproject/setup entrypoints resolve to real files and callables.",
                "- If you introduce or keep external dependencies, update requirements.txt or pyproject.toml consistently.",
            ]
        )

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
            entrypoint = self._discover_python_entrypoint(state)
            last_entrypoint = entrypoint

            if not entrypoint:
                failure = {
                    "phase": "entrypoint",
                    "python_files": python_files,
                }
                last_failure = failure
                last_failure_summary = self._summarize_validation_failure(failure)
                attempt_logs.append(
                    {
                        "attempt": attempt,
                        "phase": "entrypoint",
                        "success": False,
                        "detail": "main.py or equivalent entrypoint is missing",
                    }
                )
            else:
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
            "### ", "## ",
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
