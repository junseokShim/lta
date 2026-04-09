"""
구조화된 로깅 모듈
에이전트 활동, 도구 사용, 오류를 추적하는 로거를 제공합니다.
"""

import logging
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Optional, Any


class JSONFormatter(logging.Formatter):
    """JSON 형식으로 로그를 출력하는 포맷터"""

    def format(self, record: logging.LogRecord) -> str:
        # 기본 로그 데이터 구성
        log_data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # 에이전트 관련 추가 필드
        for field in ["agent_name", "task_id", "project_id", "tool_name", "duration_ms"]:
            if hasattr(record, field):
                log_data[field] = getattr(record, field)

        # 예외 정보 포함
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, ensure_ascii=False)


class ColoredConsoleFormatter(logging.Formatter):
    """컬러 콘솔 출력 포맷터"""

    COLORS = {
        "DEBUG": "\033[36m",    # 청록
        "INFO": "\033[32m",     # 초록
        "WARNING": "\033[33m",  # 노랑
        "ERROR": "\033[31m",    # 빨강
        "CRITICAL": "\033[35m", # 보라
        "RESET": "\033[0m",
    }

    AGENT_COLORS = {
        "manager": "\033[94m",    # 밝은 파랑
        "planner": "\033[96m",    # 밝은 청록
        "coder": "\033[92m",      # 밝은 초록
        "reviewer": "\033[93m",   # 밝은 노랑
        "researcher": "\033[95m", # 밝은 보라
        "tester": "\033[91m",     # 밝은 빨강
        "document": "\033[97m",   # 흰색
        "vision": "\033[90m",     # 밝은 검정
    }

    def format(self, record: logging.LogRecord) -> str:
        level_color = self.COLORS.get(record.levelname, self.COLORS["RESET"])
        reset = self.COLORS["RESET"]

        # 에이전트 이름이 있으면 색상 적용
        agent_prefix = ""
        if hasattr(record, "agent_name"):
            agent = record.agent_name.lower()
            agent_color = self.AGENT_COLORS.get(agent, "")
            agent_prefix = f"{agent_color}[{record.agent_name}]{reset} "

        timestamp = datetime.now().strftime("%H:%M:%S")
        level_str = f"{level_color}{record.levelname:8}{reset}"

        return f"{timestamp} {level_str} {agent_prefix}{record.getMessage()}"


class AgentLogger:
    """에이전트용 컨텍스트 로거 - 에이전트 이름과 태스크 ID를 자동으로 포함"""

    def __init__(self, agent_name: str, logger: Optional[logging.Logger] = None):
        self.agent_name = agent_name
        self._logger = logger or get_logger(f"agent.{agent_name}")

    def _log(self, level: int, msg: str, **kwargs: Any) -> None:
        extra = {"agent_name": self.agent_name}
        extra.update(kwargs)
        self._logger.log(level, msg, extra=extra, stacklevel=2)

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, **kwargs)

    def critical(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.CRITICAL, msg, **kwargs)

    def task_start(self, task_id: str, description: str) -> None:
        """태스크 시작 로깅"""
        self._log(logging.INFO, f"태스크 시작: {description}", task_id=task_id)

    def task_end(self, task_id: str, success: bool, duration_ms: Optional[float] = None) -> None:
        """태스크 종료 로깅"""
        status = "성공" if success else "실패"
        self._log(logging.INFO, f"태스크 완료 [{status}]", task_id=task_id, duration_ms=duration_ms)

    def tool_call(self, tool_name: str, args: dict) -> None:
        """도구 호출 로깅"""
        self._log(logging.DEBUG, f"도구 호출: {tool_name} | 인수: {json.dumps(args, ensure_ascii=False)[:200]}", tool_name=tool_name)

    def tool_result(self, tool_name: str, success: bool, result_preview: str = "") -> None:
        """도구 결과 로깅"""
        status = "성공" if success else "실패"
        self._log(logging.DEBUG, f"도구 결과 [{status}]: {tool_name} | {result_preview[:100]}", tool_name=tool_name)


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    json_output: bool = False,
) -> None:
    """
    전역 로깅 설정

    Args:
        level: 로그 레벨 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: 로그 파일 경로 (None이면 파일 저장 안 함)
        max_bytes: 로그 파일 최대 크기
        backup_count: 백업 파일 수
        json_output: JSON 형식 출력 여부
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 기존 핸들러 제거
    root_logger.handlers.clear()

    # 콘솔 핸들러
    console_handler = logging.StreamHandler()
    if json_output:
        console_handler.setFormatter(JSONFormatter())
    else:
        console_handler.setFormatter(ColoredConsoleFormatter())
    root_logger.addHandler(console_handler)

    # 파일 핸들러 (지정된 경우)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(JSONFormatter())
        root_logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """이름으로 로거 인스턴스를 가져옵니다"""
    return logging.getLogger(name)
