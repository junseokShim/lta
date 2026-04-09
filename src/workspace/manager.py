"""
워크스페이스(프로젝트) 관리 모듈.

두 가지 모드를 지원한다.
1. managed: workspace_root 아래에 여러 project_id 디렉터리를 만든다.
2. attached: 기존 프로젝트 폴더 자체를 작업 루트로 사용하고, 메타데이터는 숨김 디렉터리에 저장한다.
"""

import json
import re
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..logging_utils import get_logger

logger = get_logger("workspace.manager")


@dataclass
class ProjectMetadata:
    """프로젝트 메타데이터."""

    project_id: str
    name: str
    description: str
    created_at: str
    updated_at: str
    task_count: int = 0
    generated_files: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    config: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectMetadata":
        return cls(**data)


class WorkspaceManager:
    """
    워크스페이스 관리자.

    managed 모드:
      workspace_root/project_id/ 아래에 프로젝트를 생성한다.

    attached 모드:
      workspace_root 자체가 실제 프로젝트 루트이며,
      메타데이터와 산출물은 workspace_root/.lta/ 아래에 저장한다.
    """

    METADATA_FILE = ".project.json"
    DB_FILE = ".history.db"
    METADATA_DIR = ".lta"
    CHAT_HISTORY_FILE = "chat_history.jsonl"
    GUIDANCE_PATTERNS = [
        "AGENTS.md",
        "CLAUDE.md",
        ".cursorrules",
        ".github/copilot-instructions.md",
        ".cursor/rules/*.mdc",
        "README.md",
        "CONTRIBUTING.md",
        "pyproject.toml",
        "package.json",
        "requirements.txt",
    ]

    def __init__(
        self,
        workspace_root: str,
        attached: bool = False,
        metadata_dir_name: str = METADATA_DIR,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.attached = attached
        self.metadata_dir_name = metadata_dir_name

        if self.attached:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            self.storage_root = self.workspace_root / self.metadata_dir_name
            self.storage_root.mkdir(parents=True, exist_ok=True)
            logger.info("Attached workspace 초기화: %s", self.workspace_root)
        else:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            self.storage_root = self.workspace_root
            logger.info("Managed workspace 초기화: %s", self.workspace_root)

    def is_attached_mode(self) -> bool:
        """현재 모드가 attached 인지 반환한다."""
        return self.attached

    def get_storage_path(self) -> Path:
        """에이전트 메타데이터/로그/산출물 저장 루트."""
        return self.storage_root

    def get_session_db_path(self) -> Path:
        """세션 전역 히스토리 DB 경로."""
        return self.storage_root / "task_history.db"

    def create_project(
        self,
        name: str,
        description: str = "",
        tags: Optional[list[str]] = None,
    ) -> ProjectMetadata:
        """
        프로젝트를 생성하거나 attached 모드에서는 메타데이터를 초기화한다.
        """
        if self.attached:
            project_id = self._attached_project_id()
            self._ensure_storage_subdirs()

            metadata_path = self._metadata_path(project_id)
            if metadata_path.exists():
                metadata = self._load_metadata(project_id)
                metadata.name = name or metadata.name
                if description:
                    metadata.description = description
                if tags:
                    metadata.tags = tags
                metadata.updated_at = datetime.now().isoformat()
            else:
                now = datetime.now().isoformat()
                metadata = ProjectMetadata(
                    project_id=project_id,
                    name=name or self.workspace_root.name,
                    description=description,
                    created_at=now,
                    updated_at=now,
                    tags=tags or [],
                    config={"mode": "attached", "project_root": str(self.workspace_root)},
                )

            self._save_metadata(project_id, metadata)
            self._init_database(project_id)
            logger.info("Attached project 준비 완료: %s (%s)", metadata.name, metadata.project_id)
            return metadata

        project_id = self._name_to_id(name)
        base_id = project_id
        counter = 1
        while self._project_dir(project_id).exists():
            project_id = f"{base_id}_{counter}"
            counter += 1

        project_dir = self._project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)

        for subdir in ["artifacts", "inputs", "logs", "reports", "cache"]:
            (project_dir / subdir).mkdir(exist_ok=True)

        now = datetime.now().isoformat()
        metadata = ProjectMetadata(
            project_id=project_id,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
            tags=tags or [],
            config={"mode": "managed"},
        )

        self._save_metadata(project_id, metadata)
        self._init_database(project_id)
        logger.info("프로젝트 생성: %s (%s)", name, project_id)
        return metadata

    def load_project(self, project_id: str) -> ProjectMetadata:
        """프로젝트 메타데이터를 로드한다."""
        if self.attached:
            attached_id = self._attached_project_id()
            if project_id not in {attached_id, self.workspace_root.name, ".", ""}:
                raise FileNotFoundError(f"Attached project 를 찾을 수 없습니다: {project_id}")
            if not self._metadata_path(attached_id).exists():
                raise FileNotFoundError(f"Attached project 메타데이터가 없습니다: {attached_id}")
            return self._load_metadata(attached_id)

        project_dir = self._project_dir(project_id)
        if not project_dir.exists():
            raise FileNotFoundError(f"프로젝트를 찾을 수 없습니다: {project_id}")
        return self._load_metadata(project_id)

    def list_projects(self) -> list[ProjectMetadata]:
        """프로젝트 목록을 반환한다."""
        if self.attached:
            attached_id = self._attached_project_id()
            if self._metadata_path(attached_id).exists():
                return [self._load_metadata(attached_id)]
            return []

        projects = []
        for project_dir in sorted(self.workspace_root.iterdir()):
            if project_dir.is_dir() and (project_dir / self.METADATA_FILE).exists():
                try:
                    projects.append(self._load_metadata(project_dir.name))
                except Exception as exc:
                    logger.warning("프로젝트 로드 실패: %s - %s", project_dir.name, exc)
        return projects

    def delete_project(self, project_id: str, confirm: bool = False) -> bool:
        """프로젝트를 삭제한다. attached 모드에서는 보호를 위해 비활성화한다."""
        if not confirm:
            logger.warning("프로젝트 삭제 취소됨 (confirm=False)")
            return False

        if self.attached:
            logger.warning("Attached project 는 삭제하지 않습니다.")
            return False

        project_dir = self._project_dir(project_id)
        if project_dir.exists():
            shutil.rmtree(project_dir)
            logger.info("프로젝트 삭제: %s", project_id)
            return True
        return False

    def save_artifact(
        self,
        project_id: str,
        filename: str,
        content: str,
        subfolder: str = "artifacts",
        encoding: str = "utf-8",
    ) -> str:
        """에이전트 산출물을 저장한다."""
        storage_dir = self._storage_dir(project_id) / subfolder
        storage_dir.mkdir(parents=True, exist_ok=True)

        file_path = storage_dir / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding=encoding)

        self._record_generated_file(project_id, file_path)
        logger.info("산출물 저장: %s", file_path)
        return str(file_path)

    def save_project_file(
        self,
        project_id: str,
        relative_path: str,
        content: str,
        encoding: str = "utf-8",
    ) -> str:
        """실제 프로젝트 루트 안에 파일을 저장한다."""
        file_path = self._validate_project_relative_path(project_id, relative_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding=encoding)

        self._record_generated_file(project_id, file_path)
        logger.info("프로젝트 파일 저장: %s", file_path)
        return str(file_path)

    def save_log(self, project_id: str, log_content: str, log_name: Optional[str] = None) -> str:
        """실행 로그 저장."""
        if not log_name:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_name = f"run_{timestamp}.log"
        return self.save_artifact(project_id, log_name, log_content, "logs")

    def save_report(self, project_id: str, report_content: str, report_name: Optional[str] = None) -> str:
        """리포트 저장."""
        if not report_name:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_name = f"report_{timestamp}.md"
        return self.save_artifact(project_id, report_name, report_content, "reports")

    def save_cache_text(
        self,
        project_id: str,
        relative_path: str,
        content: str,
        encoding: str = "utf-8",
    ) -> str:
        """메모리 절약용 캐시 텍스트를 저장한다. generated_files 에는 기록하지 않는다."""
        cache_dir = self._storage_dir(project_id) / "cache"
        file_path = cache_dir / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding=encoding)
        return str(file_path)

    def load_cache_text(
        self,
        project_id: str,
        relative_path: str,
        encoding: str = "utf-8",
    ) -> str:
        """저장된 캐시 텍스트를 다시 읽는다."""
        file_path = self._storage_dir(project_id) / "cache" / relative_path
        return file_path.read_text(encoding=encoding, errors="replace")

    def record_task(
        self,
        project_id: str,
        task_id: str,
        description: str,
        result: str,
        status: str = "completed",
        agent_name: str = "",
        duration_ms: float = 0.0,
    ) -> None:
        """프로젝트 로컬 히스토리 DB 에 작업 기록을 남긴다."""
        db_path = self._db_path(project_id)
        conn = sqlite3.connect(str(db_path))

        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO task_history
                (task_id, description, result, status, agent_name, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    description,
                    result[:5000],
                    status,
                    agent_name,
                    duration_ms,
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        metadata = self._load_metadata(project_id)
        metadata.task_count += 1
        metadata.updated_at = datetime.now().isoformat()
        self._save_metadata(project_id, metadata)

    def get_task_history(self, project_id: str, limit: int = 50) -> list[dict]:
        """프로젝트 로컬 히스토리 조회."""
        db_path = self._db_path(project_id)
        conn = sqlite3.connect(str(db_path))

        try:
            cursor = conn.execute(
                """
                SELECT task_id, description, result, status, agent_name, duration_ms, created_at
                FROM task_history
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            columns = [
                "task_id",
                "description",
                "result",
                "status",
                "agent_name",
                "duration_ms",
                "created_at",
            ]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def append_chat_message(
        self,
        project_id: str,
        role: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> None:
        """프로젝트별 대화 로그에 메시지를 추가한다."""
        chat_path = self._chat_history_path(project_id)
        chat_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().isoformat(),
            "role": role,
            "content": content,
            "metadata": metadata or {},
        }
        with open(chat_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get_chat_history(self, project_id: str, limit: int = 50) -> list[dict]:
        """프로젝트별 대화 기록을 최근순으로 반환한다."""
        chat_path = self._chat_history_path(project_id)
        if not chat_path.exists():
            return []

        records = []
        with open(chat_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.debug("채팅 기록 한 줄을 파싱하지 못했습니다: %s", chat_path)

        if limit > 0:
            records = records[-limit:]
        return records

    def clear_chat_history(self, project_id: str) -> None:
        """프로젝트별 대화 기록을 삭제한다."""
        self._chat_history_path(project_id).unlink(missing_ok=True)

    def get_project_path(self, project_id: str) -> Path:
        """실제 프로젝트 루트를 반환한다."""
        return self._project_dir(project_id)

    def get_artifacts_path(self, project_id: str) -> Path:
        """산출물 디렉터리 경로를 반환한다."""
        return self._storage_dir(project_id) / "artifacts"

    def export_project(self, project_id: str, output_path: str) -> str:
        """프로젝트 전체를 ZIP 으로 내보낸다."""
        project_dir = self.get_project_path(project_id)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        shutil.make_archive(str(output.with_suffix("")), "zip", str(project_dir))
        zip_path = str(output.with_suffix(".zip"))
        logger.info("프로젝트 내보내기: %s -> %s", project_id, zip_path)
        return zip_path

    def get_project_summary(self, project_id: str) -> str:
        """프로젝트 요약 정보를 반환한다."""
        metadata = self._load_metadata(project_id)
        task_history = self.get_task_history(project_id, limit=5)
        project_dir = self.get_project_path(project_id)

        all_files = self._collect_project_files(project_dir, limit=20)

        lines = [
            f"## 프로젝트: {metadata.name}",
            f"ID: {metadata.project_id}",
            f"설명: {metadata.description or '없음'}",
            f"모드: {'attached' if self.attached else 'managed'}",
            f"프로젝트 루트: {project_dir}",
            f"메타데이터 루트: {self._storage_dir(project_id)}",
            f"생성일: {metadata.created_at}",
            f"최종 수정: {metadata.updated_at}",
            f"작업 수: {metadata.task_count}",
            f"태그: {', '.join(metadata.tags) or '없음'}",
            "",
            "### 파일 목록:",
        ]

        if all_files:
            for item in all_files:
                lines.append(f"  - {item}")
        else:
            lines.append("  (없음)")

        if task_history:
            lines.extend(["", "### 최근 작업"])
            for task in task_history[:5]:
                lines.append(f"  [{task['status']}] {task['description'][:80]}")

        return "\n".join(lines)

    def get_project_guidance(self, project_id: str, max_chars: int = 4000) -> str:
        """프로젝트별 작업 규칙/설명 파일을 모아 프롬프트용 텍스트로 반환한다."""
        project_dir = self.get_project_path(project_id)
        gathered_files = []
        seen: set[Path] = set()

        for pattern in self.GUIDANCE_PATTERNS:
            for path in sorted(project_dir.glob(pattern)):
                if path in seen or not path.is_file():
                    continue
                if self.attached and self.metadata_dir_name in path.parts:
                    continue
                seen.add(path)
                gathered_files.append(path)

        if not gathered_files:
            return ""

        remaining = max_chars
        sections = []
        for path in gathered_files:
            if remaining <= 0:
                break

            try:
                content = path.read_text(encoding="utf-8", errors="replace").strip()
            except Exception as exc:
                logger.debug("Guidance file read failed: %s - %s", path, exc)
                continue

            if not content:
                continue

            header = f"--- {path.relative_to(project_dir)} ---\n"
            budget = max(0, remaining - len(header))
            if budget <= 0:
                break

            snippet = content[: min(budget, 1200)]
            sections.append(header + snippet)
            remaining -= len(header) + len(snippet) + 2

        return "\n\n".join(sections)

    def _attached_project_id(self) -> str:
        """Attached 모드의 고정 project_id."""
        safe = self._slugify(self.workspace_root.name) or "project"
        return f"attached_{safe}"

    def _name_to_id(self, name: str) -> str:
        """프로젝트 이름을 안전한 ID 로 바꾼다."""
        safe = self._slugify(name)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{safe}_{timestamp}" if safe else f"project_{timestamp}"

    def _slugify(self, value: str) -> str:
        """이름을 안전한 파일시스템 slug 로 변환한다."""
        safe = re.sub(r"[^\w가-힣]+", "_", value.lower())
        safe = re.sub(r"_+", "_", safe).strip("_")
        return safe

    def _project_dir(self, project_id: str) -> Path:
        """project_id 기준 실제 프로젝트 루트."""
        if self.attached:
            return self.workspace_root
        return self.workspace_root / project_id

    def _storage_dir(self, project_id: str) -> Path:
        """project_id 기준 메타데이터/산출물 저장 루트."""
        _ = project_id
        if self.attached:
            return self.storage_root
        return self._project_dir(project_id)

    def _chat_history_path(self, project_id: str) -> Path:
        """프로젝트 대화 로그 JSONL 경로."""
        return self._storage_dir(project_id) / "logs" / self.CHAT_HISTORY_FILE

    def _metadata_path(self, project_id: str) -> Path:
        """메타데이터 JSON 경로."""
        return self._storage_dir(project_id) / self.METADATA_FILE

    def _db_path(self, project_id: str) -> Path:
        """프로젝트 히스토리 DB 경로."""
        return self._storage_dir(project_id) / self.DB_FILE

    def _ensure_storage_subdirs(self) -> None:
        """Attached 모드용 하위 디렉터리 보장."""
        for subdir in ["artifacts", "inputs", "logs", "reports", "cache"]:
            (self.storage_root / subdir).mkdir(parents=True, exist_ok=True)

    def _save_metadata(self, project_id: str, metadata: ProjectMetadata) -> None:
        """메타데이터를 저장한다."""
        path = self._metadata_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(metadata.to_dict(), handle, ensure_ascii=False, indent=2)

    def _load_metadata(self, project_id: str) -> ProjectMetadata:
        """메타데이터를 읽는다."""
        with open(self._metadata_path(project_id), "r", encoding="utf-8") as handle:
            return ProjectMetadata.from_dict(json.load(handle))

    def _record_generated_file(self, project_id: str, absolute_path: Path) -> None:
        """메타데이터의 generated_files 목록을 갱신한다."""
        metadata = self._load_metadata(project_id)
        relative = self._relative_path_for_metadata(project_id, absolute_path)
        if relative not in metadata.generated_files:
            metadata.generated_files.append(relative)
        metadata.updated_at = datetime.now().isoformat()
        self._save_metadata(project_id, metadata)

    def _relative_path_for_metadata(self, project_id: str, absolute_path: Path) -> str:
        """프로젝트 기준 상대 경로를 계산한다."""
        project_dir = self.get_project_path(project_id)
        try:
            return str(absolute_path.relative_to(project_dir))
        except ValueError:
            return str(absolute_path)

    def _validate_project_relative_path(self, project_id: str, relative_path: str) -> Path:
        """프로젝트 루트 안쪽 경로인지 검증한다."""
        project_dir = self.get_project_path(project_id).resolve()
        resolved = (project_dir / relative_path).resolve()
        if not str(resolved).startswith(str(project_dir)):
            raise ValueError(f"프로젝트 루트 밖으로 나가는 경로입니다: {relative_path}")
        return resolved

    def _collect_project_files(self, project_dir: Path, limit: int = 20) -> list[str]:
        """요약용 파일 목록을 수집한다."""
        skip_names = {".git", "__pycache__", "node_modules", ".pytest_cache", "venv"}
        if self.attached:
            skip_names.add(self.metadata_dir_name)

        files = []
        for item in sorted(project_dir.rglob("*")):
            if item.is_dir():
                continue
            if any(part in skip_names for part in item.parts):
                continue
            try:
                files.append(str(item.relative_to(project_dir)))
            except ValueError:
                continue
            if len(files) >= limit:
                break
        return files

    def _init_database(self, project_id: str) -> None:
        """프로젝트 히스토리 DB 를 초기화한다."""
        db_path = self._db_path(project_id)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_history (
                    task_id TEXT PRIMARY KEY,
                    description TEXT,
                    result TEXT,
                    status TEXT,
                    agent_name TEXT,
                    duration_ms REAL,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_decisions (
                    decision_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    agent_name TEXT,
                    decision TEXT,
                    reasoning TEXT,
                    created_at TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
