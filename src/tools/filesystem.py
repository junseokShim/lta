"""
파일 시스템 도구
에이전트가 파일을 읽고 쓰고 탐색할 수 있도록 지원합니다.
보안을 위해 허용된 경로 내에서만 작동합니다.
"""

import os
import shutil
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from ..logging_utils import get_logger

logger = get_logger("tools.filesystem")

# 기본 허용 확장자
DEFAULT_ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".json",
    ".yaml", ".yml", ".csv", ".html", ".css",
    ".sh", ".toml", ".ini", ".cfg", ".rst",
    ".java", ".cpp", ".c", ".h", ".go", ".rs",
    ".rb", ".php", ".sql", ".r", ".swift",
    ".pdf", ".docx", ".pptx",
}


@dataclass
class FileInfo:
    """파일 메타데이터"""
    path: str
    name: str
    size_bytes: int
    extension: str
    is_directory: bool
    modified_at: str
    encoding: Optional[str] = None


class FilesystemTool:
    """
    파일 시스템 작업 도구
    워크스페이스 루트 디렉토리 내에서만 작동합니다.
    """

    def __init__(
        self,
        workspace_root: str,
        max_file_size_mb: float = 50.0,
        allowed_extensions: Optional[set[str]] = None,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.max_file_size_bytes = int(max_file_size_mb * 1024 * 1024)
        self.allowed_extensions = allowed_extensions or DEFAULT_ALLOWED_EXTENSIONS

    def set_workspace_root(self, workspace_root: str) -> None:
        """작업 루트를 동적으로 바꾼다."""
        self.workspace_root = Path(workspace_root).resolve()

    def _validate_path(self, path: str) -> Path:
        """
        경로가 워크스페이스 내에 있는지 검증
        경로 탈출(path traversal) 공격을 방지합니다.
        """
        resolved = (self.workspace_root / path).resolve()
        if not str(resolved).startswith(str(self.workspace_root)):
            raise ValueError(f"경로가 워크스페이스 외부입니다: {path}")
        return resolved

    def read_file(self, path: str, encoding: str = "utf-8") -> str:
        """
        파일 읽기
        Args:
            path: 워크스페이스 기준 상대 경로
            encoding: 파일 인코딩
        Returns: 파일 내용
        """
        resolved = self._validate_path(path)

        if not resolved.exists():
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")
        if not resolved.is_file():
            raise IsADirectoryError(f"디렉토리입니다: {path}")
        if resolved.stat().st_size > self.max_file_size_bytes:
            raise ValueError(f"파일이 너무 큽니다: {resolved.stat().st_size / 1024 / 1024:.1f}MB")

        # 확장자 확인
        ext = resolved.suffix.lower()
        if ext and ext not in self.allowed_extensions:
            # 바이너리 파일인 경우 경고
            logger.warning(f"허용되지 않은 확장자: {ext}")

        with open(resolved, "r", encoding=encoding, errors="replace") as f:
            content = f.read()

        logger.debug(f"파일 읽기: {path} ({len(content)} 문자)")
        return content

    def write_file(self, path: str, content: str, encoding: str = "utf-8", overwrite: bool = True) -> str:
        """
        파일 쓰기
        Args:
            path: 워크스페이스 기준 상대 경로
            content: 파일 내용
            encoding: 파일 인코딩
            overwrite: 기존 파일 덮어쓰기 여부
        Returns: 실제 저장된 경로
        """
        resolved = self._validate_path(path)

        if resolved.exists() and not overwrite:
            raise FileExistsError(f"파일이 이미 존재합니다: {path}")

        # 부모 디렉토리 생성
        resolved.parent.mkdir(parents=True, exist_ok=True)

        with open(resolved, "w", encoding=encoding) as f:
            f.write(content)

        logger.debug(f"파일 쓰기: {path} ({len(content)} 문자)")
        return str(resolved)

    def append_file(self, path: str, content: str, encoding: str = "utf-8") -> str:
        """파일에 내용 추가"""
        resolved = self._validate_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)

        with open(resolved, "a", encoding=encoding) as f:
            f.write(content)

        return str(resolved)

    def list_directory(self, path: str = ".", recursive: bool = False) -> list[FileInfo]:
        """
        디렉토리 목록 조회
        Args:
            path: 워크스페이스 기준 상대 경로
            recursive: 재귀적 탐색 여부
        Returns: FileInfo 목록
        """
        resolved = self._validate_path(path)

        if not resolved.exists():
            raise FileNotFoundError(f"디렉토리를 찾을 수 없습니다: {path}")

        results = []
        import datetime

        if recursive:
            items = resolved.rglob("*")
        else:
            items = resolved.iterdir()

        for item in sorted(items):
            try:
                stat = item.stat()
                results.append(FileInfo(
                    path=str(item.relative_to(self.workspace_root)),
                    name=item.name,
                    size_bytes=stat.st_size if item.is_file() else 0,
                    extension=item.suffix.lower() if item.is_file() else "",
                    is_directory=item.is_dir(),
                    modified_at=datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                ))
            except (PermissionError, OSError):
                continue

        return results

    def create_directory(self, path: str) -> str:
        """디렉토리 생성"""
        resolved = self._validate_path(path)
        resolved.mkdir(parents=True, exist_ok=True)
        logger.debug(f"디렉토리 생성: {path}")
        return str(resolved)

    def delete_file(self, path: str) -> bool:
        """파일 삭제 (디렉토리는 삭제 불가)"""
        resolved = self._validate_path(path)
        if resolved.is_file():
            resolved.unlink()
            logger.info(f"파일 삭제: {path}")
            return True
        return False

    def copy_file(self, src: str, dst: str) -> str:
        """파일 복사"""
        src_resolved = self._validate_path(src)
        dst_resolved = self._validate_path(dst)
        dst_resolved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_resolved, dst_resolved)
        return str(dst_resolved)

    def move_file(self, src: str, dst: str) -> str:
        """파일 이동/이름 변경"""
        src_resolved = self._validate_path(src)
        dst_resolved = self._validate_path(dst)
        dst_resolved.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_resolved), str(dst_resolved))
        return str(dst_resolved)

    def file_exists(self, path: str) -> bool:
        """파일 존재 여부 확인"""
        try:
            resolved = self._validate_path(path)
            return resolved.exists()
        except ValueError:
            return False

    def get_file_info(self, path: str) -> FileInfo:
        """파일 메타데이터 조회"""
        import datetime
        resolved = self._validate_path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")

        stat = resolved.stat()
        return FileInfo(
            path=str(resolved.relative_to(self.workspace_root)),
            name=resolved.name,
            size_bytes=stat.st_size,
            extension=resolved.suffix.lower(),
            is_directory=resolved.is_dir(),
            modified_at=datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
        )

    def search_files(self, pattern: str, directory: str = ".") -> list[str]:
        """
        파일 패턴 검색
        Args:
            pattern: glob 패턴 (예: "**/*.py")
            directory: 검색 시작 디렉토리
        Returns: 일치하는 파일 경로 목록
        """
        resolved = self._validate_path(directory)
        matches = []
        for match in resolved.glob(pattern):
            try:
                rel_path = str(match.relative_to(self.workspace_root))
                matches.append(rel_path)
            except ValueError:
                continue
        return sorted(matches)

    def grep_files(self, search_text: str, file_pattern: str = "**/*.py", case_sensitive: bool = True) -> list[dict]:
        """
        파일 내용 검색
        Args:
            search_text: 검색할 텍스트
            file_pattern: 검색 대상 파일 패턴
            case_sensitive: 대소문자 구분 여부
        Returns: 검색 결과 목록 [{file, line_num, content}]
        """
        results = []
        search = search_text if case_sensitive else search_text.lower()

        for file_path in self.search_files(file_pattern):
            try:
                content = self.read_file(file_path)
                lines = content.splitlines()
                for i, line in enumerate(lines, 1):
                    check_line = line if case_sensitive else line.lower()
                    if search in check_line:
                        results.append({
                            "file": file_path,
                            "line_num": i,
                            "content": line.strip(),
                        })
            except Exception:
                continue

        return results

    def get_directory_tree(
        self,
        path: str = ".",
        max_depth: int = 4,
        prefix: str = "",
        max_entries_per_dir: int = 40,
    ) -> str:
        """
        디렉토리 트리 문자열 반환
        Args:
            path: 시작 경로
            max_depth: 최대 깊이
            prefix: 현재 줄 앞에 추가할 문자열
        Returns: 트리 문자열
        """
        if max_depth == 0:
            return ""

        resolved = self._validate_path(path)
        if not resolved.is_dir():
            return ""

        lines = []
        items = sorted(resolved.iterdir(), key=lambda x: (x.is_file(), x.name))[:max_entries_per_dir]

        for i, item in enumerate(items):
            # 숨김 파일 제외
            if item.name.startswith(".") and item.name not in [".env", ".gitignore"]:
                continue
            # __pycache__ 제외
            if item.name in ["__pycache__", "node_modules", ".git"]:
                continue

            is_last = i == len(items) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{item.name}")

            if item.is_dir() and max_depth > 1:
                extension = "    " if is_last else "│   "
                try:
                    sub_rel = str(item.relative_to(self.workspace_root))
                    subtree = self.get_directory_tree(
                        sub_rel,
                        max_depth - 1,
                        prefix + extension,
                        max_entries_per_dir=max_entries_per_dir,
                    )
                    if subtree:
                        lines.append(subtree)
                except ValueError:
                    pass

        try:
            total_items = len(list(resolved.iterdir()))
            if total_items > max_entries_per_dir:
                lines.append(f"{prefix}... ({total_items - max_entries_per_dir} more entries omitted)")
        except Exception:
            pass

        return "\n".join(lines)
