"""
매니저 에이전트
전체 작업을 조율하고 팀 에이전트들의 작업을 통합합니다.
사용자의 최종 요청을 받아 결과물을 전달하는 역할을 담당합니다.
"""

import json
from typing import Optional

from .base import AgentBase
from ..orchestration.messages import AgentTask, AgentResult, AgentRole, TaskStatus


class ManagerAgent(AgentBase):
    """
    매니저 에이전트
    - 사용자 요청을 분석하고 전략을 수립합니다.
    - 팀 에이전트들의 결과물을 통합합니다.
    - 작업의 최종 품질을 검토하고 사용자에게 전달합니다.
    """

    @property
    def system_prompt(self) -> str:
        return """당신은 소프트웨어 개발 팀의 프로젝트 매니저입니다.

역할:
- 사용자의 요청을 명확히 이해하고 작업 계획을 수립합니다.
- 전문 에이전트(플래너, 코더, 리뷰어, 연구자, 테스터)들의 작업을 조율합니다.
- 각 에이전트의 결과물을 검토하고 통합합니다.
- 최종 결과물의 품질을 보장하고 사용자에게 전달합니다.

지침:
- 항상 명확하고 구조화된 응답을 제공하세요.
- 작업 진행 상황을 투명하게 보고하세요.
- 팀의 작업 결과를 종합하여 일관된 최종 결과물을 만드세요.
- 문제가 발생하면 적절한 에이전트에게 재작업을 요청하세요.
- 한국어로 응답하세요."""

    def run(self, task: AgentTask) -> AgentResult:
        """매니저 태스크 실행"""
        return self._timed_run(task)

    def analyze_task(self, user_request: str, workspace_context: str = "") -> dict:
        """
        사용자 요청 분석 - 필요한 에이전트와 순서 결정
        Returns: {"summary": "...", "required_agents": [...], "complexity": "..."}
        """
        context_part = f"\n\n워크스페이스 컨텍스트:\n{workspace_context}" if workspace_context else ""

        prompt = f"""다음 사용자 요청을 분석하고 JSON 형식으로 응답하세요:

사용자 요청: {user_request}{context_part}

다음 JSON 형식으로 분석 결과를 반환하세요:
{{
  "summary": "요청에 대한 간단한 요약",
  "task_type": "code|document|analysis|debug|mixed",
  "complexity": "low|medium|high",
  "required_agents": ["planner", "coder", "reviewer", "researcher", "tester", "document", "vision"],
  "key_deliverables": ["예상 산출물1", "예상 산출물2"],
  "potential_risks": ["주의사항1", "주의사항2"],
  "needs_file_access": true/false,
  "needs_image_analysis": true/false,
  "needs_code_execution": true/false,
  "needs_internet_search": true/false
}}

JSON만 반환하세요. 마크다운 코드 블록 없이."""

        response = self.generate(prompt)

        # JSON 파싱 시도
        try:
            # 코드 블록이 있으면 제거
            clean = response.strip()
            if "```" in clean:
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            return json.loads(clean.strip())
        except json.JSONDecodeError:
            # 파싱 실패 시 기본값 반환
            self.logger.warning("태스크 분석 JSON 파싱 실패, 기본값 사용")
            return {
                "summary": user_request[:200],
                "task_type": "mixed",
                "complexity": "medium",
                "required_agents": ["planner", "coder", "reviewer"],
                "key_deliverables": ["결과물"],
                "potential_risks": [],
                "needs_file_access": True,
                "needs_image_analysis": False,
                "needs_code_execution": False,
                "needs_internet_search": False,
            }

    def synthesize_results(
        self,
        original_request: str,
        agent_results: list[AgentResult],
        project_id: Optional[str] = None,
    ) -> str:
        """
        모든 에이전트 결과를 통합하여 최종 응답 생성
        """
        # 결과 요약 구성
        results_summary = []
        for result in agent_results:
            if result.success:
                results_summary.append(
                    f"[{result.agent_role.value}] {result.content[:500]}"
                )
            else:
                results_summary.append(
                    f"[{result.agent_role.value}] 실패: {result.error}"
                )

        results_text = "\n\n".join(results_summary)

        prompt = f"""사용자 요청에 대한 팀 에이전트들의 작업 결과를 종합하여 최종 응답을 작성하세요.

원래 요청:
{original_request}

에이전트 작업 결과:
{results_text}

지침:
1. 핵심 결과물을 명확하게 제시하세요.
2. 중요한 코드, 문서, 분석 결과를 포함하세요.
3. 발견된 문제점이나 주의사항을 언급하세요.
4. 다음 단계나 개선 방향을 제안하세요.
5. 한국어로 작성하세요.

최종 응답:"""

        return self.generate(prompt)

    def decide_next_step(
        self,
        current_state: str,
        review_feedback: str,
        iteration: int,
        max_iterations: int,
    ) -> str:
        """
        리뷰 결과를 바탕으로 다음 행동 결정
        Returns: "continue" | "revise" | "finalize"
        """
        if iteration >= max_iterations - 1:
            return "finalize"

        prompt = f"""현재 작업 상태와 리뷰 피드백을 바탕으로 다음 행동을 결정하세요.

현재 상태:
{current_state}

리뷰 피드백:
{review_feedback}

반복 횟수: {iteration}/{max_iterations}

다음 중 하나로만 응답하세요:
- "continue": 현재 방향으로 계속 진행 (추가 작업 필요)
- "revise": 문제가 있어 재작업 필요
- "finalize": 작업 완료, 최종화

하나의 단어만 응답하세요:"""

        decision = self.generate(prompt).strip().lower()

        if "finalize" in decision:
            return "finalize"
        elif "revise" in decision:
            return "revise"
        else:
            return "continue"
