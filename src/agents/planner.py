"""
플래너 에이전트
사용자의 복잡한 요청을 구체적인 실행 가능한 단계로 분해합니다.
경량 모델로도 충분히 동작합니다.
"""

import json
from .base import AgentBase
from ..orchestration.messages import AgentTask, AgentResult, AgentRole, ProjectPlan


class PlannerAgent(AgentBase):
    """
    플래너 에이전트
    - 작업을 세분화하고 단계별 계획을 수립합니다.
    - 각 단계에 적합한 에이전트를 할당합니다.
    - 의존성과 우선순위를 파악합니다.
    """

    @property
    def system_prompt(self) -> str:
        return """당신은 소프트웨어 개발 프로젝트의 기술 플래너입니다.

역할:
- 복잡한 작업을 명확하고 실행 가능한 단계로 분해합니다.
- 각 단계의 의존성과 순서를 파악합니다.
- 적절한 에이전트에게 작업을 배분합니다.

사용 가능한 에이전트:
- researcher: 정보 수집, 코드 분석, 문서 검색
- coder: 코드 작성, 구현, 파일 생성
- reviewer: 코드 리뷰, 품질 검사, 버그 발견
- tester: 테스트 작성, 코드 실행, 검증
- document: 문서 생성, 보고서 작성
- vision: 이미지 분석 (필요한 경우)

계획 원칙:
- 각 단계는 하나의 에이전트가 담당합니다.
- 단계는 실행 가능하고 측정 가능해야 합니다.
- 의존성을 고려한 순서로 배열하세요.
- 한국어로 응답하세요."""

    def run(self, task: AgentTask) -> AgentResult:
        """플래닝 태스크 실행"""
        return self._timed_run(task)

    def create_plan(self, task_description: str, context: dict = None) -> ProjectPlan:
        """
        작업 설명으로부터 실행 계획 생성
        Args:
            task_description: 수행할 작업 설명
            context: 추가 컨텍스트 (파일 목록, 기존 코드 등)
        Returns: ProjectPlan
        """
        context_str = ""
        if context:
            context_str = "\n컨텍스트:\n"
            for key, value in context.items():
                context_str += f"- {key}: {str(value)[:300]}\n"

        prompt = f"""다음 작업에 대한 실행 계획을 JSON 형식으로 작성하세요:

작업: {task_description}{context_str}

다음 JSON 형식으로 계획을 반환하세요:
{{
  "title": "계획 제목",
  "objective": "목표 설명",
  "estimated_complexity": "low|medium|high",
  "steps": [
    {{
      "step_num": 1,
      "title": "단계 제목",
      "description": "상세 설명",
      "assigned_agent": "researcher|coder|reviewer|tester|document|vision",
      "expected_output": "예상 산출물",
      "depends_on": []
    }}
  ],
  "artifacts_expected": ["예상 파일1.py", "예상 파일2.md"],
  "success_criteria": ["완료 기준1", "완료 기준2"]
}}

JSON만 반환하세요:"""

        response = self.generate(prompt)

        try:
            # 코드 블록 정리
            clean = response.strip()
            if "```" in clean:
                parts = clean.split("```")
                for part in parts:
                    if "{" in part:
                        clean = part
                        if clean.startswith("json"):
                            clean = clean[4:]
                        break

            data = json.loads(clean.strip())

            # AgentRole 변환
            required_agents = list(set(
                step.get("assigned_agent", "coder")
                for step in data.get("steps", [])
            ))

            plan = ProjectPlan(
                title=data.get("title", "작업 계획"),
                objective=data.get("objective", task_description),
                steps=data.get("steps", []),
                estimated_complexity=data.get("estimated_complexity", "medium"),
                artifacts_expected=data.get("artifacts_expected", []),
            )
            return plan

        except json.JSONDecodeError:
            self.logger.warning("계획 JSON 파싱 실패, 기본 계획 생성")
            return self._create_default_plan(task_description)

    def _create_default_plan(self, task_description: str) -> ProjectPlan:
        """파싱 실패 시 기본 계획 생성"""
        return ProjectPlan(
            title="기본 실행 계획",
            objective=task_description,
            steps=[
                {
                    "step_num": 1,
                    "title": "요구사항 분석",
                    "description": "작업 요구사항을 분석하고 관련 파일을 조사합니다.",
                    "assigned_agent": "researcher",
                    "expected_output": "분석 보고서",
                    "depends_on": [],
                },
                {
                    "step_num": 2,
                    "title": "구현",
                    "description": task_description,
                    "assigned_agent": "coder",
                    "expected_output": "구현된 코드",
                    "depends_on": [1],
                },
                {
                    "step_num": 3,
                    "title": "검토",
                    "description": "구현된 코드를 검토하고 개선합니다.",
                    "assigned_agent": "reviewer",
                    "expected_output": "리뷰 보고서",
                    "depends_on": [2],
                },
            ],
            estimated_complexity="medium",
        )

    def refine_plan(self, plan: ProjectPlan, feedback: str) -> ProjectPlan:
        """피드백을 반영하여 계획 개선"""
        current_plan_str = json.dumps({
            "title": plan.title,
            "objective": plan.objective,
            "steps": plan.steps,
        }, ensure_ascii=False, indent=2)

        prompt = f"""현재 계획을 피드백을 반영하여 개선하세요.

현재 계획:
{current_plan_str}

피드백:
{feedback}

개선된 계획을 동일한 JSON 형식으로 반환하세요:"""

        response = self.generate(prompt)

        try:
            clean = response.strip()
            if "```" in clean:
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            data = json.loads(clean.strip())

            return ProjectPlan(
                title=data.get("title", plan.title),
                objective=data.get("objective", plan.objective),
                steps=data.get("steps", plan.steps),
                estimated_complexity=data.get("estimated_complexity", plan.estimated_complexity),
                artifacts_expected=data.get("artifacts_expected", plan.artifacts_expected),
            )
        except json.JSONDecodeError:
            self.logger.warning("계획 개선 파싱 실패, 원본 계획 유지")
            return plan

    def format_plan_for_display(self, plan: ProjectPlan) -> str:
        """계획을 사람이 읽기 좋은 형식으로 변환"""
        lines = [
            f"## 📋 {plan.title}",
            f"**목표**: {plan.objective}",
            f"**복잡도**: {plan.estimated_complexity}",
            "",
            "### 실행 단계:",
        ]

        for step in plan.steps:
            step_num = step.get("step_num", "?")
            title = step.get("title", "")
            description = step.get("description", "")
            agent = step.get("assigned_agent", "")
            output = step.get("expected_output", "")

            lines.append(f"\n**{step_num}단계: {title}** [{agent}]")
            lines.append(f"  - 설명: {description}")
            lines.append(f"  - 예상 산출물: {output}")

        if plan.artifacts_expected:
            lines.extend(["", "### 예상 생성 파일:"])
            for artifact in plan.artifacts_expected:
                lines.append(f"  - {artifact}")

        return "\n".join(lines)
