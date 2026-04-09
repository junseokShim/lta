"""
연구자 에이전트
로컬 파일, 코드베이스, 문서를 분석하고 관련 정보를 수집합니다.
RAG(검색 증강 생성)를 활용하여 컨텍스트를 제공합니다.
"""

from pathlib import Path
from typing import Optional

from .base import AgentBase
from ..orchestration.messages import AgentTask, AgentResult, AgentRole
from ..memory.retrieval import LocalRetrieval


class ResearcherAgent(AgentBase):
    """
    연구자 에이전트
    - 프로젝트 파일과 코드를 분석합니다.
    - RAG를 통해 관련 문서를 검색합니다.
    - 코드베이스의 구조와 패턴을 파악합니다.
    - 기술적 배경 정보를 수집합니다.
    """

    def __init__(self, *args, retrieval: Optional[LocalRetrieval] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.retrieval = retrieval

    @property
    def system_prompt(self) -> str:
        return """당신은 기술 연구 전문가입니다.

역할:
- 코드베이스와 문서를 분석하여 핵심 정보를 추출합니다.
- 관련 파일과 코드를 찾아 컨텍스트를 제공합니다.
- 기술적인 요구사항을 명확히 이해합니다.
- 구현에 필요한 배경 정보를 수집합니다.

분석 원칙:
- 핵심 정보를 간결하게 정리합니다.
- 코드 구조와 패턴을 파악합니다.
- 잠재적 문제와 의존성을 파악합니다.
- 구체적인 근거와 함께 정보를 제공합니다.

한국어로 응답하세요."""

    def run(self, task: AgentTask) -> AgentResult:
        """연구 태스크 실행"""
        return self._timed_run(task)

    def _ensure_retrieval_index(self) -> None:
        """검색 인덱스가 비어 있으면 필요할 때만 초기화한다."""
        if not self.retrieval:
            return
        stats = self.retrieval.get_stats()
        if stats.get("total_chunks", 0) > 0:
            return
        self.logger.info("로컬 검색 인덱스를 초기화합니다...")
        self.retrieval.index_directory()

    def analyze_repository(self, directory: str = ".") -> str:
        """
        레포지토리 구조 분석
        Args:
            directory: 분석할 디렉토리 경로
        Returns: 분석 결과 텍스트
        """
        if not self.fs:
            return "파일시스템 도구가 없습니다."

        retrieval_stats = self.retrieval.get_stats() if self.retrieval else {}

        # 디렉토리 트리 가져오기
        try:
            tree = self.fs.get_directory_tree(directory, max_depth=2, max_entries_per_dir=20)
        except Exception as e:
            tree = f"디렉토리 트리 생성 실패: {e}"

        # 주요 파일 내용 읽기
        key_files_content = ""
        key_files = ["README.md", "requirements.txt", "setup.py", "pyproject.toml",
                     "package.json", "Dockerfile", "docker-compose.yml"]

        for key_file in key_files:
            if self.fs.file_exists(key_file):
                content = self._read_file_for_context(key_file, 1000)
                key_files_content += f"\n--- {key_file} ---\n{content}\n"

        if retrieval_stats.get("total_chunks", 0) >= 10000 or retrieval_stats.get("indexed_files", 0) >= 800:
            return (
                "대규모 저장소로 판단되어 researcher 1차 분석을 경량 모드로 수행했습니다.\n\n"
                f"- indexed_files: {retrieval_stats.get('indexed_files', 0)}\n"
                f"- total_chunks: {retrieval_stats.get('total_chunks', 0)}\n"
                f"- indexing_cap: {retrieval_stats.get('max_total_chunks', 0)}\n\n"
                "상위 구조:\n"
                f"{tree[:2500]}\n\n"
                "주요 파일 발췌:\n"
                f"{key_files_content[:2000] if key_files_content else '없음'}"
            )

        prompt = f"""다음 프로젝트를 분석하고 요약하세요:

프로젝트 구조:
{tree}

주요 파일 내용:
{key_files_content[:3000] if key_files_content else '없음'}

분석 항목:
1. 프로젝트 목적과 기능
2. 기술 스택과 주요 의존성
3. 프로젝트 구조 설명
4. 코딩 패턴과 규칙 (발견된 경우)
5. 주목할 만한 특징이나 문제점"""

        return self.generate(prompt)

    def find_relevant_files(self, query: str, directory: str = ".") -> list[str]:
        """
        쿼리와 관련된 파일 찾기
        Args:
            query: 검색 쿼리
            directory: 검색 디렉토리
        Returns: 관련 파일 경로 목록
        """
        if not self.fs:
            return []

        # RAG 기반 검색 시도
        if self.retrieval:
            self._ensure_retrieval_index()
            results = self.retrieval.search(query, top_k=5)
            if results:
                return list(set(r.file_path for r in results))

        # 파일명/확장자 기반 검색 (폴백)
        relevant = []
        try:
            # Python 파일 검색
            py_files = self.fs.search_files("**/*.py", directory)
            # 내용 기반 필터링
            for file_path in py_files[:20]:
                content = self._read_file_for_context(file_path, 500)
                query_words = query.lower().split()
                matches = sum(1 for word in query_words if word in content.lower())
                if matches > 0:
                    relevant.append((matches, file_path))
            relevant.sort(reverse=True)
            return [f for _, f in relevant[:5]]
        except Exception as e:
            self.logger.error(f"파일 검색 오류: {e}")
            return []

    def analyze_code_file(self, file_path: str) -> str:
        """
        특정 코드 파일 분석
        Args:
            file_path: 분석할 파일 경로
        Returns: 분석 결과
        """
        code = self._read_file_for_context(file_path, 5000)
        ext = Path(file_path).suffix.lower()

        prompt = f"""다음 코드 파일을 분석하세요:

파일: {file_path}

코드:
```{ext.lstrip('.')}
{code}
```

다음을 분석하세요:
1. 파일의 목적과 역할
2. 주요 클래스/함수 목록과 기능
3. 입력/출력 파라미터
4. 의존성 (import/require)
5. 개선 가능한 부분
6. 다른 파일과의 관계"""

        return self.generate(prompt)

    def extract_requirements(self, description: str, context_files: Optional[list[str]] = None) -> dict:
        """
        설명에서 기술 요구사항 추출
        Args:
            description: 작업 설명
            context_files: 참고 파일 목록
        Returns: {"functional": [...], "technical": [...], "constraints": [...]}
        """
        import json

        context_str = ""
        if context_files and self.fs:
            for f in context_files[:3]:
                content = self._read_file_for_context(f, 1000)
                context_str += f"\n--- {f} ---\n{content}\n"

        prompt = f"""다음 설명에서 기술 요구사항을 추출하고 JSON으로 반환하세요:

설명: {description}

{context_str}

JSON 형식:
{{
  "functional": ["기능 요구사항1", "기능 요구사항2"],
  "technical": ["기술 요구사항1", "기술 요구사항2"],
  "constraints": ["제약사항1", "제약사항2"],
  "assumptions": ["가정사항1", "가정사항2"]
}}

JSON만 반환하세요:"""

        response = self.generate(prompt)

        try:
            clean = response.strip()
            if "```" in clean:
                for part in clean.split("```"):
                    if "{" in part:
                        clean = part.lstrip("json").strip()
                        break
            return json.loads(clean)
        except json.JSONDecodeError:
            return {
                "functional": [description],
                "technical": [],
                "constraints": [],
                "assumptions": [],
            }

    def search_local_docs(self, query: str, max_results: int = 3) -> str:
        """
        로컬 문서에서 관련 내용 검색
        Args:
            query: 검색 쿼리
            max_results: 최대 결과 수
        Returns: 검색 결과 텍스트
        """
        if not self.retrieval:
            return f"검색 인덱스가 초기화되지 않았습니다. (쿼리: {query})"

        self._ensure_retrieval_index()
        results = self.retrieval.search(query, top_k=max_results)

        if not results:
            return f"'{query}'에 관련된 문서를 찾을 수 없습니다."

        lines = [f"'{query}' 관련 문서 검색 결과:"]
        for r in results:
            lines.append(f"\n--- {r.file_path} (관련도: {r.score:.2f}) ---")
            lines.append(r.content[:500])

        return "\n".join(lines)

    def search_web(self, query: str, max_results: int = 5) -> str:
        """
        웹 검색 결과를 수집한다.
        """
        if not self.web:
            return "웹 검색 도구가 초기화되지 않았습니다."

        try:
            return self.web.search_as_markdown(query, max_results=max_results)
        except Exception as exc:
            self.logger.error(f"웹 검색 오류: {exc}")
            return f"웹 검색 실패: {exc}"

    def summarize_findings(self, research_data: list[str], question: str) -> str:
        """
        수집된 연구 데이터를 질문에 맞게 요약
        Args:
            research_data: 수집된 정보 목록
            question: 답해야 할 질문
        Returns: 요약된 답변
        """
        data_str = "\n\n".join(f"[출처 {i+1}]\n{data[:800]}"
                               for i, data in enumerate(research_data[:5]))

        prompt = f"""다음 연구 데이터를 바탕으로 질문에 답하세요:

질문: {question}

수집된 데이터:
{data_str}

핵심 내용을 간결하게 요약하여 답변하세요."""

        return self.generate(prompt)
