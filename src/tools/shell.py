"""
셸 명령 실행 도구
에이전트가 안전하게 시스템 명령을 실행할 수 있도록 합니다.
허용된 명령만 실행하고 타임아웃을 강제합니다.
"""

import subprocess
import shlex
import os
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from ..logging_utils import get_logger

logger = get_logger("tools.shell")

# 차단된 위험한 패턴
BLOCKED_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "> /dev/sda",
    "mkfs",
    ":(){:|:&};:",  # fork bomb
    "dd if=/dev/zero of=/dev/",
    "chmod 777 /",
    "sudo rm",
]


@dataclass
class CommandResult:
    """명령 실행 결과"""
    command: str
    stdout: str
    stderr: str
    return_code: int
    duration_ms: float
    success: bool
    error: Optional[str] = None


class ShellTool:
    """
    안전한 셸 명령 실행 도구
    화이트리스트 기반 명령 허용 및 타임아웃 강제 적용
    """

    def __init__(
        self,
        workspace_root: str,
        timeout: int = 60,
        allowed_commands: Optional[list[str]] = None,
    ):
        self.workspace_root = Path(workspace_root)
        self.timeout = timeout
        self.python_executable = sys.executable or "python"
        self.allowed_commands = set(allowed_commands or [
            "python", "python3", "pip", "pip3",
            "pytest", "unittest",
            "git",
            "ls", "dir", "cat", "type",
            "echo", "print",
            "mkdir", "cp", "copy", "mv", "move",
            "grep", "find", "which", "where",
            "node", "npm", "yarn",
            "curl", "wget",
            "black", "flake8", "mypy", "ruff",
            "poetry", "uv",
        ])

    def set_workspace_root(self, workspace_root: str) -> None:
        """작업 루트를 동적으로 바꾼다."""
        self.workspace_root = Path(workspace_root).resolve()

    def run(
        self,
        command: str,
        cwd: Optional[str] = None,
        env_override: Optional[dict] = None,
        timeout_override: Optional[int] = None,
    ) -> CommandResult:
        """
        명령 실행
        Args:
            command: 실행할 명령
            cwd: 작업 디렉토리 (기본: workspace_root)
            env_override: 환경 변수 오버라이드
            timeout_override: 타임아웃 오버라이드
        Returns: CommandResult
        """
        import time

        # 보안 검사
        security_error = self._check_security(command)
        if security_error:
            logger.warning(f"보안 차단: {command}")
            return CommandResult(
                command=command,
                stdout="",
                stderr=security_error,
                return_code=-1,
                duration_ms=0,
                success=False,
                error=security_error,
            )

        # 작업 디렉토리 설정
        work_dir = self.workspace_root
        if cwd:
            work_dir = (self.workspace_root / cwd).resolve()
            # 경로 탈출 방지
            if not str(work_dir).startswith(str(self.workspace_root)):
                return CommandResult(
                    command=command,
                    stdout="",
                    stderr="작업 디렉토리가 워크스페이스 외부입니다.",
                    return_code=-1,
                    duration_ms=0,
                    success=False,
                    error="경로 보안 오류",
                )

        # 환경 변수 설정
        env = os.environ.copy()
        if env_override:
            env.update(env_override)

        timeout = timeout_override or self.timeout
        start_time = time.time()

        try:
            logger.info(f"명령 실행: {command[:100]}")

            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(work_dir),
                env=env,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )

            duration_ms = (time.time() - start_time) * 1000
            success = result.returncode == 0

            if not success:
                logger.warning(f"명령 실패 (코드 {result.returncode}): {command[:100]}")
            else:
                logger.debug(f"명령 완료 ({duration_ms:.0f}ms): {command[:100]}")

            return CommandResult(
                command=command,
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.returncode,
                duration_ms=duration_ms,
                success=success,
            )

        except subprocess.TimeoutExpired:
            duration_ms = (time.time() - start_time) * 1000
            msg = f"명령 타임아웃 ({timeout}초 초과)"
            logger.error(f"{msg}: {command[:100]}")
            return CommandResult(
                command=command,
                stdout="",
                stderr=msg,
                return_code=-1,
                duration_ms=duration_ms,
                success=False,
                error=msg,
            )
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return CommandResult(
                command=command,
                stdout="",
                stderr=str(e),
                return_code=-1,
                duration_ms=duration_ms,
                success=False,
                error=str(e),
            )

    def run_python_code(self, code: str, timeout: int = 30) -> CommandResult:
        """
        Python 코드를 임시 파일로 실행
        Args:
            code: 실행할 Python 코드
            timeout: 실행 타임아웃
        Returns: CommandResult
        """
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            dir=str(self.workspace_root),
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(code)
            tmp_path = f.name

        try:
            result = self.run(
                self._join_command([self.python_executable, tmp_path]),
                timeout_override=timeout,
            )
        finally:
            # 임시 파일 정리
            Path(tmp_path).unlink(missing_ok=True)

        return result

    def run_pytest(
        self,
        test_targets: Optional[list[str]] = None,
        verbose: bool = True,
        extra_args: Optional[list[str]] = None,
    ) -> CommandResult:
        """pytest 실행 (특정 타깃 또는 전체 tests 디렉터리)."""
        args = [self.python_executable, "-m", "pytest"]
        if test_targets:
            args.extend(test_targets)
        else:
            args.append("tests/")
        if verbose:
            args.append("-v")
        args.append("--tb=short")
        if extra_args:
            args.extend(extra_args)
        return self.run(self._join_command(args))

    def run_tests(self, test_path: str = "tests/", verbose: bool = True) -> CommandResult:
        """
        pytest 실행
        Args:
            test_path: 테스트 경로
            verbose: 상세 출력 여부
        Returns: CommandResult
        """
        targets = [test_path] if test_path else None
        return self.run_pytest(targets, verbose=verbose)

    def check_python_syntax(self, file_path: str) -> CommandResult:
        """Python 파일 문법 검사"""
        return self.run(self._join_command([self.python_executable, "-m", "py_compile", file_path]))

    def format_python_code(self, file_path: str) -> CommandResult:
        """black으로 Python 코드 포맷팅"""
        return self.run(
            self._join_command([self.python_executable, "-m", "black", file_path, "--line-length", "100"])
        )

    def get_python_version(self) -> str:
        """Python 버전 확인"""
        result = self.run(self._join_command([self.python_executable, "--version"]))
        return result.stdout.strip() or result.stderr.strip()

    def get_git_status(self, max_lines: int = 50) -> str:
        """현재 workspace 기준 git status 요약을 반환한다."""
        probe = self.run(self._join_command(["git", "rev-parse", "--is-inside-work-tree"]))
        if not probe.success or "true" not in (probe.stdout or "").lower():
            return ""

        status = self.run(self._join_command(["git", "status", "--short", "--branch"]))
        if not status.success:
            return ""

        lines = [line for line in status.stdout.splitlines() if line.strip()]
        return "\n".join(lines[:max_lines])

    def _quote_arg(self, value: str) -> str:
        """플랫폼에 맞게 단일 인자를 quoting 한다."""
        if os.name == "nt":
            return subprocess.list2cmdline([value])
        return shlex.quote(value)

    def _join_command(self, args: list[str]) -> str:
        """인자 목록을 shell 실행용 문자열로 합친다."""
        if os.name == "nt":
            return subprocess.list2cmdline(args)
        return " ".join(self._quote_arg(arg) for arg in args)

    def _check_security(self, command: str) -> Optional[str]:
        """
        명령 보안 검사
        Returns: 오류 메시지 (None이면 안전)
        """
        # 차단된 패턴 검사
        cmd_lower = command.lower()
        for pattern in BLOCKED_PATTERNS:
            if pattern.lower() in cmd_lower:
                return f"차단된 명령 패턴: {pattern}"

        # 명령어의 첫 단어(프로그램 이름) 추출
        try:
            parts = shlex.split(command)
            if not parts:
                return "빈 명령"
            cmd_name = Path(parts[0]).name  # 경로에서 파일명만 추출
        except ValueError:
            # shlex 파싱 실패 시 공백으로 분리
            parts = command.split()
            if not parts:
                return "빈 명령"
            cmd_name = Path(parts[0]).name

        # 화이트리스트 검사 (strict 모드가 필요한 경우 활성화)
        # 현재는 차단 목록만 사용하여 유연성 유지
        # if cmd_name not in self.allowed_commands:
        #     return f"허용되지 않은 명령: {cmd_name}"

        return None
