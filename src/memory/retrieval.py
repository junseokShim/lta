"""
로컬 문서 검색(RAG) 모듈
프로젝트 내 문서를 인덱싱하고 관련 내용을 검색합니다.
BM25 + 선택적 임베딩 기반 하이브리드 검색을 지원합니다.
"""

import json
import re
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from ..logging_utils import get_logger

logger = get_logger("memory.retrieval")


@dataclass
class RetrievalResult:
    """검색 결과"""
    file_path: str
    content: str
    score: float
    chunk_index: int
    metadata: dict


class LocalRetrieval:
    """
    로컬 문서 검색 엔진
    BM25 기반 키워드 검색 + 선택적 FAISS 벡터 검색

    GTX 5070 환경에서 메모리 효율적으로 동작합니다.
    임베딩 모델은 CPU에서도 실행 가능한 경량 모델을 사용합니다.
    """

    def __init__(
        self,
        workspace_root: str,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        top_k: int = 5,
        use_embeddings: bool = False,  # True면 sentence-transformers 필요
        max_indexed_files: int = 2000,
        max_total_chunks: int = 25000,
        max_chunks_per_file: int = 400,
        max_file_size_mb: float = 2.5,
    ):
        self.workspace_root = Path(workspace_root)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.top_k = top_k
        self.use_embeddings = use_embeddings
        self.max_indexed_files = max_indexed_files
        self.max_total_chunks = max_total_chunks
        self.max_chunks_per_file = max_chunks_per_file
        self.max_file_size_bytes = int(max_file_size_mb * 1024 * 1024)

        # 인덱스: {file_path: [chunks]}
        self._index: dict[str, list[str]] = {}
        self._chunks: list[dict] = []  # [{content, file_path, chunk_index}]

        # BM25 인덱스 (rank_bm25 사용)
        self._bm25 = None
        self._bm25_corpus = []

        # 임베딩 인덱스 (선택적)
        self._embedder = None
        self._faiss_index = None
        self._faiss_metadata = []

    def set_workspace_root(self, workspace_root: str, clear_index: bool = True) -> None:
        """검색 대상 루트를 동적으로 바꾼다."""
        self.workspace_root = Path(workspace_root).resolve()
        if clear_index:
            self.clear()

    def index_directory(self, directory: str = ".", file_extensions: Optional[list[str]] = None) -> int:
        """
        디렉토리의 모든 텍스트 파일을 인덱싱
        Args:
            directory: 인덱싱할 디렉토리
            file_extensions: 인덱싱할 확장자 목록 (None이면 기본값 사용)
        Returns: 인덱싱된 청크 수
        """
        extensions = file_extensions or [".txt", ".md", ".py", ".json", ".yaml", ".yml", ".rst"]
        dir_path = self.workspace_root / directory

        new_chunks = []
        indexed_files = 0
        skipped_large_files = 0
        skipped_limit_files = 0
        skip_parts = {
            "__pycache__",
            ".git",
            "node_modules",
            ".lta",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".venv",
            "venv",
            "dist",
            "build",
            ".idea",
            ".vscode",
            ".cache",
            ".next",
            "coverage",
            "target",
            ".tox",
            ".ipynb_checkpoints",
        }

        for ext in extensions:
            for file_path in dir_path.rglob(f"*{ext}"):
                if any(part in skip_parts for part in file_path.parts):
                    continue
                if indexed_files >= self.max_indexed_files or len(self._chunks) + len(new_chunks) >= self.max_total_chunks:
                    skipped_limit_files += 1
                    continue
                try:
                    if file_path.stat().st_size > self.max_file_size_bytes:
                        skipped_large_files += 1
                        continue
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                    chunks = self._split_text(content, str(file_path))[: self.max_chunks_per_file]
                    remaining_budget = self.max_total_chunks - (len(self._chunks) + len(new_chunks))
                    if remaining_budget <= 0:
                        skipped_limit_files += 1
                        continue
                    chunks = chunks[:remaining_budget]
                    if not chunks:
                        continue
                    self._index[str(file_path)] = chunks
                    for i, chunk in enumerate(chunks):
                        new_chunks.append({
                            "content": chunk,
                            "file_path": str(file_path.relative_to(self.workspace_root)),
                            "chunk_index": i,
                        })
                    indexed_files += 1
                except Exception as e:
                    logger.warning(f"파일 인덱싱 실패: {file_path} - {e}")

        self._chunks.extend(new_chunks)

        # BM25 재구성
        self._rebuild_bm25()

        # 임베딩 인덱스 재구성 (활성화된 경우)
        if self.use_embeddings:
            self._rebuild_embeddings()

        logger.info(
            "인덱싱 완료: %s개 청크, 총 %s개, 파일 %s개, 대형 파일 스킵 %s개, 제한 스킵 %s개",
            len(new_chunks),
            len(self._chunks),
            indexed_files,
            skipped_large_files,
            skipped_limit_files,
        )
        return len(new_chunks)

    def index_file(self, file_path: str) -> int:
        """단일 파일 인덱싱"""
        path = self.workspace_root / file_path
        if not path.exists():
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {file_path}")

        content = path.read_text(encoding="utf-8", errors="replace")
        chunks = self._split_text(content, str(path))

        # 기존 파일의 청크 제거
        self._chunks = [c for c in self._chunks if c["file_path"] != file_path]

        for i, chunk in enumerate(chunks):
            self._chunks.append({
                "content": chunk,
                "file_path": file_path,
                "chunk_index": i,
            })

        self._index[str(path)] = chunks
        self._rebuild_bm25()

        return len(chunks)

    def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        file_filter: Optional[str] = None,
    ) -> list[RetrievalResult]:
        """
        쿼리와 관련된 문서 청크 검색
        Args:
            query: 검색 쿼리
            top_k: 반환할 최대 결과 수
            file_filter: 특정 파일만 검색 (glob 패턴)
        Returns: RetrievalResult 목록
        """
        k = top_k or self.top_k

        if not self._chunks:
            logger.warning("인덱싱된 문서가 없습니다.")
            return []

        results = []

        if self.use_embeddings and self._faiss_index is not None:
            results = self._search_embeddings(query, k)

        if not results and self._bm25 is not None:
            # Small corpora can yield only zero BM25 scores even for exact matches.
            results = self._search_bm25(query, k)

        if not results:
            results = self._search_simple(query, k)

        # 파일 필터 적용
        if file_filter:
            import fnmatch
            results = [r for r in results if fnmatch.fnmatch(r.file_path, file_filter)]

        return results[:k]

    def get_context_for_query(self, query: str, max_chars: int = 3000) -> str:
        """
        쿼리에 관련된 컨텍스트 문자열 반환 (LLM 프롬프트용)
        """
        results = self.search(query)
        if not results:
            return "관련 문서를 찾을 수 없습니다."

        context_parts = ["[관련 문서 컨텍스트]"]
        total_chars = 0

        for result in results:
            header = f"\n--- {result.file_path} (관련도: {result.score:.2f}) ---\n"
            chunk_content = result.content

            if total_chars + len(header) + len(chunk_content) > max_chars:
                remaining = max_chars - total_chars - len(header)
                if remaining > 100:
                    chunk_content = chunk_content[:remaining] + "..."
                else:
                    break

            context_parts.append(header + chunk_content)
            total_chars += len(header) + len(chunk_content)

        return "\n".join(context_parts)

    def clear(self) -> None:
        """인덱스 초기화"""
        self._index.clear()
        self._chunks.clear()
        self._bm25 = None
        self._bm25_corpus.clear()
        self._faiss_index = None
        self._faiss_metadata.clear()
        logger.info("검색 인덱스 초기화")

    def get_stats(self) -> dict:
        """인덱스 통계"""
        file_count = len(set(c["file_path"] for c in self._chunks))
        return {
            "total_chunks": len(self._chunks),
            "indexed_files": file_count,
            "embeddings_enabled": self.use_embeddings and self._faiss_index is not None,
            "max_indexed_files": self.max_indexed_files,
            "max_total_chunks": self.max_total_chunks,
        }

    def _split_text(self, text: str, source: str) -> list[str]:
        """
        텍스트를 청크로 분할
        문단 경계를 우선 고려하고, 그 다음 줄 경계, 마지막으로 문자 경계 사용
        """
        # 빈 텍스트 처리
        if not text.strip():
            return []

        chunks = []
        start = 0

        while start < len(text):
            end = start + self.chunk_size

            if end >= len(text):
                chunks.append(text[start:].strip())
                break

            # 청크 경계 찾기 (문단 > 줄 > 문자 순)
            chunk_text = text[start:end]

            # 마지막 문단 경계 찾기
            para_break = chunk_text.rfind("\n\n")
            if para_break > self.chunk_size // 2:
                end = start + para_break
            else:
                # 마지막 줄 경계 찾기
                line_break = chunk_text.rfind("\n")
                if line_break > self.chunk_size // 2:
                    end = start + line_break

            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)

            # 오버랩 적용
            start = end - self.chunk_overlap

        return chunks

    def _tokenize(self, text: str) -> list[str]:
        """BM25용 텍스트 토크나이즈"""
        # 한국어 + 영어 간단 토크나이즈
        text = text.lower()
        # 특수문자를 공백으로 대체
        text = re.sub(r"[^\w가-힣\s]", " ", text)
        # 공백으로 분리
        tokens = text.split()
        # 불필요한 단어 제거 (2글자 미만)
        tokens = [t for t in tokens if len(t) > 1]
        return tokens

    def _rebuild_bm25(self) -> None:
        """BM25 인덱스 재구성"""
        try:
            from rank_bm25 import BM25Okapi
            corpus = [self._tokenize(c["content"]) for c in self._chunks]
            if corpus:
                self._bm25 = BM25Okapi(corpus)
                self._bm25_corpus = corpus
        except ImportError:
            logger.warning("rank_bm25가 설치되지 않았습니다. 단순 검색을 사용합니다.")

    def _search_bm25(self, query: str, k: int) -> list[RetrievalResult]:
        """BM25 기반 검색"""
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)

        # 상위 k개 인덱스
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]

        results = []
        for idx in top_indices:
            if scores[idx] > 0 and idx < len(self._chunks):
                chunk = self._chunks[idx]
                results.append(RetrievalResult(
                    file_path=chunk["file_path"],
                    content=chunk["content"],
                    score=float(scores[idx]),
                    chunk_index=chunk["chunk_index"],
                    metadata={},
                ))

        return results

    def _search_simple(self, query: str, k: int) -> list[RetrievalResult]:
        """단순 키워드 검색 (BM25 없을 때 폴백)"""
        query_words = set(self._tokenize(query))
        if not query_words:
            return []

        scored = []

        for chunk in self._chunks:
            chunk_words = set(self._tokenize(chunk["content"]))
            overlap = len(query_words & chunk_words)
            if overlap > 0:
                score = overlap / max(len(query_words), 1)
                scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)

        return [
            RetrievalResult(
                file_path=chunk["file_path"],
                content=chunk["content"],
                score=score,
                chunk_index=chunk["chunk_index"],
                metadata={},
            )
            for score, chunk in scored[:k]
        ]

    def _rebuild_embeddings(self) -> None:
        """임베딩 인덱스 재구성 (선택적)"""
        try:
            from sentence_transformers import SentenceTransformer
            import numpy as np
            import faiss

            if self._embedder is None:
                # 경량 한국어+영어 지원 모델 사용
                self._embedder = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

            texts = [c["content"] for c in self._chunks]
            if not texts:
                return

            logger.info("임베딩 생성 중...")
            embeddings = self._embedder.encode(texts, batch_size=32, show_progress_bar=False)
            embeddings = embeddings.astype(np.float32)

            # FAISS 인덱스 생성
            dim = embeddings.shape[1]
            self._faiss_index = faiss.IndexFlatIP(dim)  # 내적 유사도
            faiss.normalize_L2(embeddings)
            self._faiss_index.add(embeddings)
            self._faiss_metadata = self._chunks.copy()

            logger.info(f"임베딩 인덱스 완료: {len(texts)}개 벡터")

        except ImportError as e:
            logger.warning(f"임베딩 라이브러리 없음: {e}. BM25로 대체합니다.")
            self.use_embeddings = False

    def _search_embeddings(self, query: str, k: int) -> list[RetrievalResult]:
        """FAISS 임베딩 검색"""
        try:
            import numpy as np
            import faiss

            query_emb = self._embedder.encode([query]).astype(np.float32)
            faiss.normalize_L2(query_emb)

            scores, indices = self._faiss_index.search(query_emb, k)

            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx >= 0 and idx < len(self._faiss_metadata):
                    chunk = self._faiss_metadata[idx]
                    results.append(RetrievalResult(
                        file_path=chunk["file_path"],
                        content=chunk["content"],
                        score=float(score),
                        chunk_index=chunk["chunk_index"],
                        metadata={},
                    ))
            return results
        except Exception as e:
            logger.error(f"임베딩 검색 오류: {e}")
            return self._search_bm25(query, k)
