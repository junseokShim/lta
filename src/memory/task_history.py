"""
태스크 히스토리 관리 모듈
에이전트의 작업 기록을 저장하고 조회합니다.
SQLite 기반으로 프로젝트별 히스토리를 관리합니다.
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, asdict

from ..logging_utils import get_logger

logger = get_logger("memory.task_history")


@dataclass
class TaskRecord:
    """저장된 태스크 레코드"""
    task_id: str
    session_id: str
    project_id: str
    title: str
    description: str
    assigned_to: str
    status: str
    result_summary: str
    artifacts: list[str]
    duration_ms: float
    created_at: str
    completed_at: Optional[str] = None
    error: Optional[str] = None
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class TaskHistory:
    """
    태스크 히스토리 저장소
    메모리(세션 내)와 영속적(SQLite) 두 가지 저장 방식 지원
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Args:
            db_path: SQLite 데이터베이스 경로 (None이면 메모리 전용)
        """
        self.db_path = db_path
        self._memory: list[TaskRecord] = []  # 세션 내 메모리

        if db_path:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def add(self, record: TaskRecord) -> None:
        """태스크 레코드 추가"""
        self._memory.append(record)

        if self.db_path:
            self._insert_db(record)

        logger.debug(f"태스크 기록: {record.task_id} [{record.status}]")

    def update_status(
        self,
        task_id: str,
        status: str,
        result_summary: str = "",
        error: Optional[str] = None,
        artifacts: Optional[list[str]] = None,
        duration_ms: float = 0.0,
    ) -> None:
        """태스크 상태 업데이트"""
        completed_at = datetime.now().isoformat() if status in ("completed", "failed") else None

        # 메모리 업데이트
        for record in self._memory:
            if record.task_id == task_id:
                record.status = status
                record.result_summary = result_summary
                record.error = error
                record.completed_at = completed_at
                record.duration_ms = duration_ms
                if artifacts is not None:
                    record.artifacts = artifacts
                break

        # DB 업데이트
        if self.db_path:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("""
                    UPDATE tasks SET
                        status=?, result_summary=?, error=?,
                        completed_at=?, duration_ms=?, artifacts=?
                    WHERE task_id=?
                """, (
                    status,
                    result_summary[:3000],
                    error,
                    completed_at,
                    duration_ms,
                    json.dumps(artifacts or [], ensure_ascii=False),
                    task_id,
                ))
                conn.commit()
            finally:
                conn.close()

    def get(self, task_id: str) -> Optional[TaskRecord]:
        """task_id로 레코드 조회"""
        # 메모리에서 먼저 검색
        for record in reversed(self._memory):
            if record.task_id == task_id:
                return record

        # DB에서 검색
        if self.db_path:
            return self._query_db_single(task_id)

        return None

    def get_recent(self, limit: int = 10, project_id: Optional[str] = None) -> list[TaskRecord]:
        """최근 태스크 목록"""
        if project_id:
            filtered = [r for r in reversed(self._memory) if r.project_id == project_id]
        else:
            filtered = list(reversed(self._memory))
        return filtered[:limit]

    def get_session_summary(self) -> str:
        """현재 세션의 태스크 요약"""
        if not self._memory:
            return "이번 세션에서 수행된 태스크가 없습니다."

        lines = [f"세션 태스크 요약 ({len(self._memory)}개):", ""]
        for record in self._memory:
            status_icon = "✓" if record.status == "completed" else "✗" if record.status == "failed" else "○"
            lines.append(f"{status_icon} [{record.assigned_to}] {record.title}")
            if record.result_summary:
                lines.append(f"   결과: {record.result_summary[:100]}")
        return "\n".join(lines)

    def get_context_for_agent(self, agent_role: str, limit: int = 3) -> str:
        """특정 에이전트에게 관련 히스토리 컨텍스트 제공"""
        related = [r for r in reversed(self._memory) if r.assigned_to == agent_role][:limit]
        if not related:
            return ""

        lines = [f"[이전 {agent_role} 작업 참고]"]
        for record in related:
            lines.append(f"- {record.title}: {record.result_summary[:200]}")
        return "\n".join(lines)

    def clear_session(self) -> None:
        """세션 메모리 초기화 (DB는 유지)"""
        self._memory.clear()
        logger.info("세션 메모리 초기화")

    def _init_db(self) -> None:
        """SQLite 테이블 초기화"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    project_id TEXT,
                    title TEXT,
                    description TEXT,
                    assigned_to TEXT,
                    status TEXT,
                    result_summary TEXT,
                    artifacts TEXT,
                    duration_ms REAL,
                    error TEXT,
                    created_at TEXT,
                    completed_at TEXT,
                    metadata TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_project ON tasks(project_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON tasks(created_at)")
            conn.commit()
        finally:
            conn.close()

    def _insert_db(self, record: TaskRecord) -> None:
        """DB에 레코드 삽입"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                record.task_id,
                record.session_id,
                record.project_id,
                record.title,
                record.description[:1000],
                record.assigned_to,
                record.status,
                record.result_summary[:3000],
                json.dumps(record.artifacts, ensure_ascii=False),
                record.duration_ms,
                record.error,
                record.created_at,
                record.completed_at,
                json.dumps(record.metadata, ensure_ascii=False),
            ))
            conn.commit()
        finally:
            conn.close()

    def _query_db_single(self, task_id: str) -> Optional[TaskRecord]:
        """DB에서 단일 레코드 조회"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_record(row)
        finally:
            conn.close()
        return None

    def _row_to_record(self, row: tuple) -> TaskRecord:
        """DB 행을 TaskRecord로 변환"""
        return TaskRecord(
            task_id=row[0],
            session_id=row[1],
            project_id=row[2],
            title=row[3],
            description=row[4],
            assigned_to=row[5],
            status=row[6],
            result_summary=row[7],
            artifacts=json.loads(row[8] or "[]"),
            duration_ms=row[9] or 0.0,
            error=row[10],
            created_at=row[11],
            completed_at=row[12],
            metadata=json.loads(row[13] or "{}"),
        )
