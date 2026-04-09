"""
코더 에이전트
코드 작성, 구현, 수정을 담당합니다.
강력한 LLM 모델을 사용하여 고품질 코드를 생성합니다.
"""

import re
from pathlib import Path
from typing import Optional

from .base import AgentBase
from ..orchestration.messages import AgentTask, AgentResult, AgentRole, Artifact


class CoderAgent(AgentBase):
    """
    코더 에이전트
    - 요구사항에 맞는 코드를 작성합니다.
    - 기존 코드를 수정하고 개선합니다.
    - 코드 패치를 적용합니다.
    - 코드 설명을 제공합니다.
    """

    @property
    def system_prompt(self) -> str:
        return """당신은 숙련된 소프트웨어 엔지니어입니다.

역할:
- 요구사항에 맞는 고품질 코드를 작성합니다.
- 기존 코드를 분석하고 개선합니다.
- 명확하고 유지보수가 쉬운 코드를 작성합니다.
- 적절한 에러 처리와 로깅을 포함합니다.

코딩 원칙:
- PEP8 및 언어별 스타일 가이드를 따릅니다.
- 타입 힌트를 사용합니다 (Python의 경우).
- 의미 있는 변수명과 함수명을 사용합니다.
- 복잡한 로직에는 주석을 달아 이해를 돕습니다.
- 테스트 가능한 코드 구조를 유지합니다.

응답 형식:
- 코드는 항상 ```language 코드 블록으로 감싸세요.
- 코드 전후에 간략한 설명을 추가하세요.
- 파일명이 있으면 명시하세요.
- 한국어로 설명하세요."""

    def run(self, task: AgentTask) -> AgentResult:
        """코딩 태스크 실행"""
        return self._timed_run(task)

    def write_code(
        self,
        description: str,
        language: str = "python",
        context_files: Optional[list[str]] = None,
        requirements: Optional[list[str]] = None,
    ) -> list[Artifact]:
        """
        새 코드 작성
        Args:
            description: 구현할 내용 설명
            language: 프로그래밍 언어
            context_files: 참고할 기존 파일 목록
            requirements: 요구사항 목록
        Returns: 생성된 코드 Artifact 목록
        """
        # 컨텍스트 파일 읽기
        context_str = ""
        if context_files and self.fs:
            context_str = "\n\n참고 파일들:\n"
            for f in context_files[:3]:  # 최대 3개 파일
                content = self._read_file_for_context(f, 2000)
                context_str += f"\n--- {f} ---\n{content}\n"

        # 요구사항 목록
        req_str = ""
        if requirements:
            req_str = "\n\n요구사항:\n" + "\n".join(f"- {r}" for r in requirements)

        prompt = f"""다음 요구사항에 맞는 {language} 코드를 작성하세요:

{description}{context_str}{req_str}

다음 형식으로 응답하세요:
1. 파일명 및 용도 설명
2. 전체 코드 (```{language} 블록)
3. 주요 기능 설명
4. 사용 방법

여러 파일이 필요한 경우 각각을 별도 코드 블록으로 작성하고 파일명을 명시하세요."""

        response = self.generate(prompt)
        return self._parse_code_response(response, language)

    def modify_code(
        self,
        file_path: str,
        modification_description: str,
        target_functions: Optional[list[str]] = None,
    ) -> Artifact:
        """
        기존 코드 수정
        Args:
            file_path: 수정할 파일 경로
            modification_description: 수정 내용 설명
            target_functions: 수정할 특정 함수/클래스 이름
        Returns: 수정된 코드 Artifact
        """
        original_code = self._read_file_for_context(file_path, 5000)

        targets_str = ""
        if target_functions:
            targets_str = f"\n수정 대상: {', '.join(target_functions)}"

        prompt = f"""다음 파일의 코드를 수정하세요:

파일: {file_path}

원본 코드:
```
{original_code}
```

수정 내용: {modification_description}{targets_str}

수정된 전체 파일 코드를 반환하세요. 수정된 부분에 주석으로 변경 이유를 표시하세요."""

        response = self.generate(prompt)
        artifacts = self._parse_code_response(response, Path(file_path).suffix.lstrip("."))

        if artifacts:
            artifacts[0].name = Path(file_path).name
            artifacts[0].file_path = file_path
            return artifacts[0]

        # 파싱 실패 시 응답 전체를 반환
        return Artifact(
            name=Path(file_path).name,
            artifact_type="code",
            content=response,
            file_path=file_path,
        )

    def generate_patch(self, original: str, modified: str) -> str:
        """원본과 수정본 사이의 diff 생성"""
        import difflib
        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            lineterm="",
        )
        return "".join(diff)

    def fix_bug(
        self,
        file_path: str,
        error_message: str,
        error_traceback: Optional[str] = None,
    ) -> Artifact:
        """
        버그 수정
        Args:
            file_path: 오류가 발생한 파일
            error_message: 오류 메시지
            error_traceback: 전체 스택 트레이스
        Returns: 수정된 코드 Artifact
        """
        code = self._read_file_for_context(file_path, 5000)
        traceback_str = f"\n\n스택 트레이스:\n{error_traceback}" if error_traceback else ""

        prompt = f"""다음 코드의 버그를 수정하세요:

파일: {file_path}

오류 메시지: {error_message}{traceback_str}

오류가 발생한 코드:
```
{code}
```

1. 오류 원인을 설명하세요.
2. 수정된 전체 코드를 제공하세요.
3. 수정 내용을 설명하세요."""

        response = self.generate(prompt)
        artifacts = self._parse_code_response(response, Path(file_path).suffix.lstrip("."))

        if artifacts:
            artifacts[0].name = Path(file_path).name
            artifacts[0].file_path = file_path
            return artifacts[0]

        return Artifact(
            name=Path(file_path).name,
            artifact_type="code",
            content=response,
            file_path=file_path,
        )

    def explain_code(self, file_path: str, specific_function: Optional[str] = None) -> str:
        """
        코드 설명 생성
        Args:
            file_path: 설명할 파일 경로
            specific_function: 특정 함수/클래스 (None이면 전체)
        Returns: 코드 설명
        """
        code = self._read_file_for_context(file_path, 4000)

        focus_str = f"\n특히 '{specific_function}' 부분을 중심으로 설명하세요." if specific_function else ""

        prompt = f"""다음 코드를 상세히 설명하세요:

파일: {file_path}

코드:
```
{code}
```

설명 항목:
1. 코드의 전반적인 목적
2. 주요 함수/클래스의 역할
3. 중요한 로직 흐름
4. 사용된 라이브러리/패턴
5. 잠재적 문제점 또는 개선 기회{focus_str}"""

        return self.generate(prompt)

    def _parse_code_response(self, response: str, default_language: str = "python") -> list[Artifact]:
        """
        LLM 응답에서 코드 블록 추출
        Returns: Artifact 목록
        """
        artifacts = []

        # 코드 블록 패턴: ```language\n코드\n```
        pattern = r"```(\w*)\n(.*?)```"
        matches = re.findall(pattern, response, re.DOTALL)

        for lang, code in matches:
            if not code.strip():
                continue

            language = lang or default_language
            # 파일명 추출 시도 (코드 직전 줄에서)
            filename = self._extract_filename_from_response(response, code, language)

            artifacts.append(Artifact(
                name=filename or f"generated_code.{self._lang_to_ext(language)}",
                artifact_type="code",
                content=code.strip(),
                file_path=filename,
                language=language,
                metadata={
                    "description": "코더 에이전트가 생성한 코드",
                    "has_explicit_path": bool(filename),
                    "source": "code_block",
                },
            ))

        # 코드 블록이 없으면 전체 응답을 코드로 간주
        if not artifacts and response.strip():
            artifacts.append(Artifact(
                name=f"generated_code.{self._lang_to_ext(default_language)}",
                artifact_type="code",
                content=response.strip(),
                file_path=None,
                language=default_language,
                metadata={
                    "description": "코더 에이전트 원문 응답",
                    "has_explicit_path": False,
                    "source": "raw_response",
                },
            ))

        return artifacts

    def _extract_filename_from_response(self, full_response: str, code_block: str, language: str) -> Optional[str]:
        """응답 텍스트에서 파일명 추출 시도"""
        # 코드 블록 직전 텍스트에서 파일명 패턴 검색
        code_pos = full_response.find(code_block[:50])
        if code_pos > 0:
            preceding_text = full_response[max(0, code_pos - 200):code_pos]
            # 파일명 패턴: `filename.ext` 또는 ** filename.ext **
            patterns = [
                r"`([a-zA-Z0-9_\-/]+\.[a-z]+)`",
                r"\*\*([a-zA-Z0-9_\-/]+\.[a-z]+)\*\*",
                r"파일[명:]?\s*`?([a-zA-Z0-9_\-/]+\.[a-z]+)`?",
                r"File:\s*`?([a-zA-Z0-9_\-/]+\.[a-z]+)`?",
            ]
            for pattern in patterns:
                match = re.search(pattern, preceding_text, re.IGNORECASE)
                if match:
                    return match.group(1)
        return None

    def _lang_to_ext(self, language: str) -> str:
        """언어명을 파일 확장자로 변환"""
        mapping = {
            "python": "py",
            "javascript": "js",
            "typescript": "ts",
            "java": "java",
            "go": "go",
            "rust": "rs",
            "cpp": "cpp",
            "c": "c",
            "bash": "sh",
            "shell": "sh",
            "yaml": "yaml",
            "json": "json",
            "markdown": "md",
            "html": "html",
            "css": "css",
            "sql": "sql",
        }
        return mapping.get(language.lower(), "txt")
