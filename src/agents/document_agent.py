"""
문서 에이전트
프로젝트 문서 생성, 편집, 요약을 담당합니다.
README, API 문서, 사용 설명서 등을 작성합니다.
"""

from pathlib import Path
from typing import Optional

from .base import AgentBase
from ..orchestration.messages import AgentTask, AgentResult, AgentRole, Artifact
from ..tools.document import DocumentTool, ParsedDocument


class DocumentAgent(AgentBase):
    """
    문서 에이전트
    - 코드에서 문서를 자동으로 생성합니다.
    - 기존 문서를 편집하고 개선합니다.
    - 프로젝트 문서를 정리하고 요약합니다.
    - 보고서와 사용 설명서를 작성합니다.
    """

    @property
    def system_prompt(self) -> str:
        return """당신은 기술 문서 전문가입니다.

역할:
- 명확하고 읽기 쉬운 기술 문서를 작성합니다.
- 코드를 분석하여 자동으로 문서를 생성합니다.
- 기존 문서를 개선하고 최신 상태로 유지합니다.
- 다양한 독자를 위한 문서 수준을 조절합니다.

문서 원칙:
- 간결하고 명확한 언어를 사용합니다.
- 예제 코드를 포함합니다.
- 구조화된 형식(마크다운)을 사용합니다.
- 독자가 무엇을 해야 하는지 명확히 안내합니다.

주로 한국어로 작성하되, 코드 식별자는 영어로 유지합니다."""

    def run(self, task: AgentTask) -> AgentResult:
        """문서화 태스크 실행"""
        return self._timed_run(task)

    def generate_readme(
        self,
        project_name: str,
        project_description: str,
        features: Optional[list[str]] = None,
        code_files: Optional[list[str]] = None,
    ) -> Artifact:
        """
        README.md 생성
        Args:
            project_name: 프로젝트 이름
            project_description: 프로젝트 설명
            features: 주요 기능 목록
            code_files: 참고할 코드 파일 목록
        Returns: README Artifact
        """
        # 코드 파일 분석
        code_context = ""
        if code_files and self.fs:
            for f in code_files[:3]:
                content = self._read_file_for_context(f, 1000)
                code_context += f"\n--- {f} ---\n{content}\n"

        features_str = ""
        if features:
            features_str = "\n주요 기능:\n" + "\n".join(f"- {f}" for f in features)

        prompt = f"""다음 프로젝트의 README.md를 작성하세요:

프로젝트명: {project_name}
설명: {project_description}
{features_str}

코드 참고:
{code_context[:2000] if code_context else '없음'}

README 포함 내용:
1. 프로젝트 소개 및 목적
2. 주요 기능 (Features)
3. 설치 방법 (Installation)
4. 사용 방법 (Usage) - 예제 코드 포함
5. 프로젝트 구조 (Project Structure)
6. 기여 방법 (Contributing) - 간략히
7. 라이선스 (License)

마크다운 형식으로 작성하세요. 이모지를 적절히 사용하세요."""

        content = self.generate(prompt)

        return Artifact(
            name="README.md",
            artifact_type="document",
            content=content,
            file_path="README.md",
            metadata={"doc_type": "readme"},
        )

    def generate_api_docs(self, code: str, file_path: str) -> Artifact:
        """
        코드에서 API 문서 생성
        Args:
            code: 문서화할 코드
            file_path: 코드 파일 경로
        Returns: API 문서 Artifact
        """
        prompt = f"""다음 코드의 API 문서를 마크다운으로 작성하세요:

파일: {file_path}

코드:
```python
{code[:5000]}
```

문서 포함 내용 (각 공개 함수/클래스에 대해):
1. 기능 설명
2. 파라미터 (이름, 타입, 설명, 기본값)
3. 반환값 (타입, 설명)
4. 예외 (발생 가능한 예외)
5. 사용 예제

마크다운 형식으로 작성하세요."""

        content = self.generate(prompt)
        doc_name = f"docs/{Path(file_path).stem}_api.md"

        return Artifact(
            name=doc_name,
            artifact_type="document",
            content=content,
            file_path=doc_name,
            metadata={"doc_type": "api", "source_file": file_path},
        )

    def summarize_documents(
        self,
        documents: list[ParsedDocument],
        output_title: str = "문서 요약",
    ) -> Artifact:
        """
        여러 문서를 읽고 종합 요약 생성
        Args:
            documents: ParsedDocument 목록
            output_title: 출력 제목
        Returns: 요약 Artifact
        """
        # 각 문서 요약 준비
        doc_summaries = []
        for doc in documents[:5]:  # 최대 5개 문서
            if self.doc:
                summary = self.doc.summarize_document(doc, max_chars=1000)
            else:
                summary = doc.content[:1000]
            doc_summaries.append(f"### {doc.title or doc.file_path}\n{summary}")

        combined = "\n\n".join(doc_summaries)

        prompt = f"""다음 문서들을 읽고 종합 요약을 작성하세요:

{combined}

종합 요약에 포함할 내용:
1. 각 문서의 핵심 내용
2. 문서들 간의 관계와 공통점
3. 전체 결론 또는 주요 발견
4. 추가로 확인할 사항

마크다운 형식으로 작성하세요."""

        content = self.generate(prompt)

        return Artifact(
            name=f"summary_{output_title.replace(' ', '_')}.md",
            artifact_type="document",
            content=content,
            metadata={"doc_type": "summary", "source_count": len(documents)},
        )

    def create_user_guide(
        self,
        tool_or_api_name: str,
        features: list[str],
        examples: Optional[list[dict]] = None,
    ) -> Artifact:
        """
        사용자 가이드 생성
        Args:
            tool_or_api_name: 도구/API 이름
            features: 기능 목록
            examples: 사용 예제 [{"title": "...", "code": "..."}]
        Returns: 사용자 가이드 Artifact
        """
        features_str = "\n".join(f"- {f}" for f in features)
        examples_str = ""
        if examples:
            for ex in examples[:5]:
                examples_str += f"\n\n#### {ex.get('title', '예제')}\n```\n{ex.get('code', '')}\n```"

        prompt = f"""{tool_or_api_name}의 사용자 가이드를 작성하세요:

주요 기능:
{features_str}

{f'사용 예제:{examples_str}' if examples_str else ''}

가이드 구성:
1. 소개 (이 도구가 무엇인지, 왜 사용하는지)
2. 빠른 시작 (5분 안에 시작하기)
3. 기본 사용법 (step-by-step)
4. 주요 기능 상세 설명
5. 고급 사용법
6. 자주 묻는 질문 (FAQ)
7. 문제 해결 (Troubleshooting)

한국어로 작성하고 마크다운 형식을 사용하세요."""

        content = self.generate(prompt)

        return Artifact(
            name=f"{tool_or_api_name.lower().replace(' ', '_')}_user_guide.md",
            artifact_type="document",
            content=content,
            metadata={"doc_type": "user_guide"},
        )

    def edit_document(
        self,
        file_path: str,
        edit_instructions: str,
    ) -> Artifact:
        """
        기존 문서 편집
        Args:
            file_path: 편집할 파일 경로
            edit_instructions: 편집 지침
        Returns: 편집된 문서 Artifact
        """
        original = self._read_file_for_context(file_path, 5000)

        prompt = f"""다음 문서를 지침에 따라 편집하세요:

파일: {file_path}

원본:
{original}

편집 지침: {edit_instructions}

편집된 전체 문서를 반환하세요."""

        content = self.generate(prompt)

        return Artifact(
            name=Path(file_path).name,
            artifact_type="document",
            content=content,
            file_path=file_path,
            metadata={"doc_type": "edited", "original_path": file_path},
        )

    def generate_changelog(
        self,
        changes: list[dict],
        version: str,
        project_name: str = "",
    ) -> Artifact:
        """
        변경 로그 생성
        Args:
            changes: [{"type": "feat|fix|docs", "description": "..."}]
            version: 버전 번호
            project_name: 프로젝트 이름
        Returns: CHANGELOG Artifact
        """
        from datetime import datetime

        changes_str = ""
        grouped = {}
        type_labels = {
            "feat": "새 기능",
            "fix": "버그 수정",
            "docs": "문서",
            "refactor": "리팩토링",
            "test": "테스트",
            "chore": "유지보수",
        }
        for change in changes:
            t = change.get("type", "chore")
            if t not in grouped:
                grouped[t] = []
            grouped[t].append(change.get("description", ""))

        for t, descs in grouped.items():
            label = type_labels.get(t, t)
            changes_str += f"\n### {label}\n"
            changes_str += "\n".join(f"- {d}" for d in descs)

        content = f"""# Changelog

## [{version}] - {datetime.now().strftime('%Y-%m-%d')}
{changes_str}
"""

        return Artifact(
            name="CHANGELOG.md",
            artifact_type="document",
            content=content,
            file_path="CHANGELOG.md",
        )
