"""
리뷰어 에이전트
코드와 문서의 품질을 검토하고 개선 사항을 제안합니다.
"""

import re
from typing import Optional
from dataclasses import dataclass, field

from .base import AgentBase
from ..orchestration.messages import AgentTask, AgentResult, AgentRole


@dataclass
class ReviewResult:
    """리뷰 결과"""
    score: int  # 1-10
    summary: str
    issues: list[dict]  # [{"severity": "high|medium|low", "description": "...", "suggestion": "..."}]
    strengths: list[str]
    recommendations: list[str]
    approved: bool  # 승인 여부


class ReviewerAgent(AgentBase):
    """
    리뷰어 에이전트
    - 코드 품질, 보안, 성능을 검토합니다.
    - 버그와 잠재적 문제점을 찾습니다.
    - 개선 사항을 구체적으로 제안합니다.
    """

    @property
    def system_prompt(self) -> str:
        return """당신은 경험 많은 시니어 소프트웨어 엔지니어입니다.

역할:
- 코드 품질, 구조, 가독성을 객관적으로 평가합니다.
- 버그, 보안 취약점, 성능 문제를 발견합니다.
- 구체적이고 실행 가능한 개선 사항을 제안합니다.

리뷰 기준:
- 정확성: 요구사항을 올바르게 구현했는가?
- 품질: 코드가 명확하고 유지보수가 쉬운가?
- 보안: 보안 취약점이 없는가?
- 성능: 성능 문제가 없는가?
- 테스트: 테스트 가능한 구조인가?

비판적이되 건설적인 피드백을 제공하세요.
한국어로 응답하세요."""

    def run(self, task: AgentTask) -> AgentResult:
        """리뷰 태스크 실행"""
        return self._timed_run(task)

    def review_code(
        self,
        code: str,
        language: str = "python",
        file_path: Optional[str] = None,
        context: Optional[str] = None,
    ) -> ReviewResult:
        """
        코드 리뷰 수행
        Args:
            code: 리뷰할 코드
            language: 프로그래밍 언어
            file_path: 파일 경로 (선택적)
            context: 추가 컨텍스트
        Returns: ReviewResult
        """
        import json

        file_info = f"파일: {file_path}\n" if file_path else ""
        context_str = f"\n컨텍스트:\n{context}" if context else ""

        prompt = f"""다음 {language} 코드를 상세히 리뷰하고 JSON 형식으로 결과를 반환하세요:

{file_info}코드:
```{language}
{code[:4000]}
```{context_str}

다음 JSON 형식으로 리뷰 결과를 반환하세요:
{{
  "score": 7,
  "summary": "전반적인 평가 요약",
  "issues": [
    {{
      "severity": "high|medium|low",
      "line": "관련 코드 라인 (선택적)",
      "description": "문제 설명",
      "suggestion": "개선 방법"
    }}
  ],
  "strengths": ["잘된 점1", "잘된 점2"],
  "recommendations": ["개선 권장사항1", "개선 권장사항2"],
  "approved": true
}}

점수 기준: 1(매우 나쁨) ~ 10(완벽)
approved: 7점 이상이면 true

JSON만 반환하세요:"""

        response = self.generate(prompt)

        try:
            clean = response.strip()
            if "```" in clean:
                for part in clean.split("```"):
                    if "{" in part:
                        clean = part.lstrip("json").strip()
                        break

            data = json.loads(clean)
            return ReviewResult(
                score=data.get("score", 5),
                summary=data.get("summary", ""),
                issues=data.get("issues", []),
                strengths=data.get("strengths", []),
                recommendations=data.get("recommendations", []),
                approved=data.get("approved", False),
            )
        except json.JSONDecodeError:
            # JSON 파싱 실패 시 텍스트 분석
            return self._parse_text_review(response)

    def review_plan(self, plan_description: str, requirements: str) -> str:
        """작업 계획 검토"""
        prompt = f"""다음 작업 계획을 검토하고 개선 사항을 제안하세요:

요구사항:
{requirements}

계획:
{plan_description}

다음을 평가하세요:
1. 계획이 요구사항을 완전히 충족하는가?
2. 단계 순서가 적절한가?
3. 누락된 단계가 있는가?
4. 위험 요소가 있는가?

간결하게 피드백을 제공하세요."""

        return self.generate(prompt)

    def review_architecture(self, project_structure: str, description: str) -> str:
        """프로젝트 구조/아키텍처 검토"""
        prompt = f"""다음 프로젝트 구조를 아키텍처 관점에서 검토하세요:

프로젝트 설명: {description}

프로젝트 구조:
{project_structure}

다음을 평가하세요:
1. 모듈 분리가 적절한가?
2. 의존성 관리가 올바른가?
3. 확장성을 고려했는가?
4. 개선 가능한 부분은?"""

        return self.generate(prompt)

    def compare_implementations(self, impl1: str, impl2: str, criteria: Optional[list[str]] = None) -> str:
        """두 구현 방식 비교"""
        criteria_str = ""
        if criteria:
            criteria_str = "\n평가 기준:\n" + "\n".join(f"- {c}" for c in criteria)

        prompt = f"""두 가지 구현 방식을 비교 분석하세요:

구현 1:
```
{impl1[:2000]}
```

구현 2:
```
{impl2[:2000]}
```{criteria_str}

성능, 가독성, 유지보수성 측면에서 비교하고 어느 쪽이 더 나은지 권장하세요."""

        return self.generate(prompt)

    def format_review_report(self, review: ReviewResult, file_path: Optional[str] = None) -> str:
        """리뷰 결과를 마크다운 보고서로 변환"""
        score_emoji = "🟢" if review.score >= 8 else "🟡" if review.score >= 6 else "🔴"
        approved_str = "✅ 승인" if review.approved else "❌ 수정 필요"

        lines = [
            f"## 코드 리뷰 결과 {score_emoji}",
            f"**파일**: {file_path or '알 수 없음'}",
            f"**점수**: {review.score}/10",
            f"**상태**: {approved_str}",
            "",
            f"### 요약\n{review.summary}",
        ]

        if review.strengths:
            lines.extend(["", "### 잘된 점"])
            for s in review.strengths:
                lines.append(f"- ✓ {s}")

        if review.issues:
            lines.extend(["", "### 발견된 문제"])
            severity_icons = {"high": "🔴", "medium": "🟡", "low": "🔵"}
            for issue in review.issues:
                icon = severity_icons.get(issue.get("severity", "low"), "⚪")
                lines.append(f"\n{icon} **{issue.get('severity', 'low').upper()}**: {issue.get('description', '')}")
                if issue.get("suggestion"):
                    lines.append(f"  → 제안: {issue['suggestion']}")

        if review.recommendations:
            lines.extend(["", "### 권장사항"])
            for rec in review.recommendations:
                lines.append(f"- {rec}")

        return "\n".join(lines)

    def _parse_text_review(self, text: str) -> ReviewResult:
        """JSON 파싱 실패 시 텍스트에서 리뷰 정보 추출"""
        # 점수 추출 시도
        score = 5
        score_match = re.search(r"(\d+)\s*/\s*10", text)
        if score_match:
            score = int(score_match.group(1))

        return ReviewResult(
            score=score,
            summary=text[:500],
            issues=[],
            strengths=[],
            recommendations=[text[:500]],
            approved=score >= 7,
        )
