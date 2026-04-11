"""
chat 모드 워크스페이스 인식 테스트

핵심 검증:
1. chat_direct()가 워크스페이스 내 파일을 실제로 읽어 LLM 컨텍스트에 전달하는지
2. 워크스페이스 경계(path traversal 방지) 동작
3. 힌트 추출 로직
4. 존재하지 않는 파일/외부 경로 처리
5. 워크스페이스 없는 경우의 폴백 동작
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.orchestration.engine import OrchestrationEngine


# ──────────────────────────────────────────────────────────────────────────────
# 헬퍼: 최소한의 mock engine 생성
# ──────────────────────────────────────────────────────────────────────────────

def _make_mock_engine(workspace_root: Path) -> OrchestrationEngine:
    """테스트용 최소 OrchestrationEngine 인스턴스 생성."""
    from src.tools.filesystem import FilesystemTool

    engine = object.__new__(OrchestrationEngine)
    engine.workspace = None

    # manager mock — generate() 호출 추적
    engine.manager = MagicMock()
    engine.manager.generate.return_value = "테스트 응답입니다."

    # researcher mock — fs는 실제 FilesystemTool 사용
    fs = FilesystemTool(str(workspace_root))
    engine.researcher = MagicMock()
    engine.researcher.fs = fs
    engine.researcher.find_relevant_files.return_value = []

    return engine


# ──────────────────────────────────────────────────────────────────────────────
# 1. _extract_path_hints_from_message() 테스트
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractPathHints:
    """사용자 메시지에서 경로 힌트를 올바르게 추출하는지 검증."""

    def setup_method(self):
        self.engine = object.__new__(OrchestrationEngine)

    def test_extracts_file_with_extension(self):
        hints = self.engine._extract_path_hints_from_message("README.md 파일을 읽어줘")
        assert "README.md" in hints

    def test_extracts_python_file(self):
        hints = self.engine._extract_path_hints_from_message("config.py 내용을 설명해줘")
        assert "config.py" in hints

    def test_extracts_common_dir_keyword(self):
        hints = self.engine._extract_path_hints_from_message("data 폴더 분석해줘")
        assert "data" in hints

    def test_extracts_src_keyword(self):
        hints = self.engine._extract_path_hints_from_message("src/ 디렉토리를 보여줘")
        assert "src" in hints

    def test_extracts_korean_data_keyword(self):
        hints = self.engine._extract_path_hints_from_message("데이터 폴더 분석해주세요")
        assert "data" in hints

    def test_extracts_multiple_hints(self):
        hints = self.engine._extract_path_hints_from_message(
            "config.py 와 main.py 를 비교해줘"
        )
        assert "config.py" in hints
        assert "main.py" in hints

    def test_no_hints_for_general_question(self):
        hints = self.engine._extract_path_hints_from_message("파이썬이란 무엇인가요?")
        # 일반 질문에는 경로 힌트가 없거나 거의 없어야 함
        # config, src 등 공통 키워드는 없을 것
        assert "README.md" not in hints
        assert "data" not in hints


# ──────────────────────────────────────────────────────────────────────────────
# 2. _is_path_within_workspace() 테스트
# ──────────────────────────────────────────────────────────────────────────────

class TestPathBoundary:
    """워크스페이스 경계 검사 — path traversal 방지."""

    def test_valid_file_in_workspace(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        test_file = tmp_path / "README.md"
        test_file.write_text("# 테스트")

        result = engine._is_path_within_workspace("README.md", tmp_path)
        assert result is not None
        assert result == test_file

    def test_path_traversal_blocked(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        # ../etc/passwd 형태의 경로 탈출 시도
        result = engine._is_path_within_workspace("../outside.txt", tmp_path)
        assert result is None

    def test_nonexistent_path_returns_none(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        result = engine._is_path_within_workspace("nonexistent_file.py", tmp_path)
        assert result is None

    def test_valid_subdirectory(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        sub = tmp_path / "data"
        sub.mkdir()

        result = engine._is_path_within_workspace("data", tmp_path)
        assert result is not None
        assert result == sub


# ──────────────────────────────────────────────────────────────────────────────
# 3. _read_workspace_item_for_chat() 테스트
# ──────────────────────────────────────────────────────────────────────────────

class TestReadWorkspaceItem:
    """파일/디렉토리 읽기 출력 형식 검증."""

    def test_reads_text_file(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        f = tmp_path / "README.md"
        f.write_text("# Hello World\nThis is a test.")

        result = engine._read_workspace_item_for_chat(f, tmp_path)
        assert "README.md" in result
        assert "Hello World" in result

    def test_lists_directory(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file1.csv").write_text("a,b,c")
        (data_dir / "file2.csv").write_text("x,y,z")

        result = engine._read_workspace_item_for_chat(data_dir, tmp_path)
        assert "[디렉토리]" in result
        assert "file1.csv" in result
        assert "file2.csv" in result

    def test_truncates_large_file(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        f = tmp_path / "large.py"
        f.write_text("x = 1\n" * 5000)  # 큰 파일

        result = engine._read_workspace_item_for_chat(f, tmp_path, max_file_chars=100)
        assert "..." in result  # 잘렸음을 표시

    def test_binary_file_shows_metadata_only(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")

        result = engine._read_workspace_item_for_chat(f, tmp_path)
        # 바이너리 파일은 내용 읽기 시도 없이 메타정보만
        assert "텍스트가 아닌 파일" in result or "image.png" in result


# ──────────────────────────────────────────────────────────────────────────────
# 4. _gather_workspace_context_for_chat() 테스트
# ──────────────────────────────────────────────────────────────────────────────

class TestGatherWorkspaceContext:
    """워크스페이스 컨텍스트 수집 통합 테스트."""

    def test_collects_readme_when_mentioned(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        readme = tmp_path / "README.md"
        readme.write_text("# My Project\nThis is a test project.")

        ctx = engine._gather_workspace_context_for_chat("README.md 요약해줘", tmp_path)
        assert "README.md" in ctx
        assert "My Project" in ctx

    def test_collects_directory_listing(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "sales.csv").write_text("month,revenue\nJan,1000")

        ctx = engine._gather_workspace_context_for_chat("data 폴더 분석해줘", tmp_path)
        assert "data" in ctx
        assert "sales.csv" in ctx

    def test_always_includes_workspace_root_listing(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "README.md").write_text("# Readme")

        # 경로 힌트 없는 일반 질문도 워크스페이스 최상위 구조 포함
        ctx = engine._gather_workspace_context_for_chat("이 프로젝트 설명해줘", tmp_path)
        assert "워크스페이스" in ctx
        assert "main.py" in ctx or "README.md" in ctx

    def test_ignores_external_path_hint(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        # 워크스페이스 외부 경로 힌트는 무시되어야 함
        ctx = engine._gather_workspace_context_for_chat("/etc/passwd 분석해줘", tmp_path)
        # /etc/passwd 내용이 포함되면 안 됨
        assert "root:" not in ctx

    def test_compares_two_files(self, tmp_path):
        engine = _make_mock_engine(tmp_path)
        (tmp_path / "config.py").write_text("# config\nDEBUG = True")
        (tmp_path / "main.py").write_text("# main\nfrom config import DEBUG")

        ctx = engine._gather_workspace_context_for_chat(
            "config.py 와 main.py 를 비교해줘", tmp_path
        )
        assert "config.py" in ctx
        assert "main.py" in ctx
        assert "DEBUG" in ctx


# ──────────────────────────────────────────────────────────────────────────────
# 5. chat_direct() 통합 테스트
# ──────────────────────────────────────────────────────────────────────────────

class TestChatDirect:
    """chat_direct() 전체 흐름 통합 테스트."""

    def test_workspace_context_injected_into_prompt(self, tmp_path):
        """워크스페이스 내 파일 내용이 LLM 프롬프트에 삽입되는지 검증."""
        engine = _make_mock_engine(tmp_path)
        (tmp_path / "README.md").write_text("# Workspace Project\nImportant description.")

        engine.chat_direct("README.md 요약해줘")

        # manager.generate()가 호출되었는지 확인
        assert engine.manager.generate.called
        call_args = engine.manager.generate.call_args

        # 첫 번째 인자(prompt)에 워크스페이스 내용이 포함되어야 함
        prompt_sent = call_args[0][0]
        assert "Workspace Project" in prompt_sent, (
            "README.md 내용이 LLM 프롬프트에 포함되어야 합니다"
        )

    def test_system_prompt_mentions_local_workspace(self, tmp_path):
        """시스템 프롬프트가 로컬 워크스페이스 접근 가능함을 명시하는지."""
        engine = _make_mock_engine(tmp_path)

        engine.chat_direct("이 프로젝트 설명해줘")

        call_args = engine.manager.generate.call_args
        system_prompt = call_args[1].get("system_prompt_override", "")
        # 시스템 프롬프트가 로컬 워크스페이스 접근을 명시해야 함
        assert "로컬 워크스페이스" in system_prompt or "워크스페이스" in system_prompt
        # "파일 업로드를 요청하거나 ... 말하지 마세요" 형태로 금지 규칙이 있어야 함
        # (단순히 "업로드"라는 단어가 없어야 하는 것이 아니라 금지 맥락으로 사용되어야 함)
        assert "말하지 마세요" in system_prompt or "하지 마세요" in system_prompt

    def test_no_workspace_uses_fallback_system_prompt(self):
        """워크스페이스 없는 경우 일반 어시스턴트 시스템 프롬프트 사용."""
        engine = object.__new__(OrchestrationEngine)
        engine.workspace = None
        engine.researcher = MagicMock()
        engine.researcher.fs = None
        engine.manager = MagicMock()
        engine.manager.generate.return_value = "응답"

        engine.chat_direct("안녕하세요")

        call_args = engine.manager.generate.call_args
        system_prompt = call_args[1].get("system_prompt_override", "")
        # 워크스페이스 없어도 "파일 업로드" 를 강요하지 않아야 함
        assert "유용하고 친절한 AI" in system_prompt or "어시스턴트" in system_prompt

    def test_conversation_history_included(self, tmp_path):
        """이전 대화 이력이 프롬프트에 포함되는지."""
        engine = _make_mock_engine(tmp_path)
        history = [
            {"role": "user", "content": "안녕하세요"},
            {"role": "assistant", "content": "안녕하세요! 무엇을 도와드릴까요?"},
        ]

        engine.chat_direct("이전 대화 기억해?", conversation_history=history)

        call_args = engine.manager.generate.call_args
        prompt_sent = call_args[0][0]
        assert "안녕하세요" in prompt_sent

    def test_path_traversal_does_not_leak_external_content(self, tmp_path):
        """경로 탈출 시도 시 외부 파일 내용이 LLM에 전달되지 않아야 함."""
        engine = _make_mock_engine(tmp_path)
        # /etc/passwd 같은 시스템 파일 참조 시도
        engine.chat_direct("/etc/passwd 보여줘")

        call_args = engine.manager.generate.call_args
        prompt_sent = call_args[0][0]
        # 실제 /etc/passwd 내용이 없어야 함
        assert "root:" not in prompt_sent
        assert "/bin/bash" not in prompt_sent

    def test_data_folder_analysis_reads_files(self, tmp_path):
        """data 폴더 분석 요청 시 실제 파일 내용이 전달되는지."""
        engine = _make_mock_engine(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "sales.csv").write_text("month,revenue\nJan,1000\nFeb,1500")
        (data_dir / "README.md").write_text("# Data Description\nSales data for 2024.")

        engine.chat_direct("data 폴더를 분석해서 인과관계를 설명해줘")

        call_args = engine.manager.generate.call_args
        prompt_sent = call_args[0][0]

        # data 폴더 내용이 프롬프트에 포함되어야 함
        assert "sales.csv" in prompt_sent or "Data Description" in prompt_sent, (
            "data 폴더 내 파일 내용이 LLM 프롬프트에 포함되어야 합니다"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 6. _get_active_workspace_root_for_chat() 테스트
# ──────────────────────────────────────────────────────────────────────────────

class TestGetActiveWorkspaceRoot:
    """워크스페이스 루트 결정 로직 테스트."""

    def test_returns_researcher_fs_root(self, tmp_path):
        from src.tools.filesystem import FilesystemTool
        engine = object.__new__(OrchestrationEngine)
        engine.workspace = None
        engine.researcher = MagicMock()
        engine.researcher.fs = FilesystemTool(str(tmp_path))

        result = engine._get_active_workspace_root_for_chat()
        assert result == tmp_path.resolve()

    def test_returns_none_when_no_workspace(self):
        engine = object.__new__(OrchestrationEngine)
        engine.workspace = None
        engine.researcher = MagicMock()
        engine.researcher.fs = None

        result = engine._get_active_workspace_root_for_chat()
        assert result is None

    def test_returns_workspace_manager_root_in_attached_mode(self, tmp_path):
        from src.workspace.manager import WorkspaceManager
        engine = object.__new__(OrchestrationEngine)
        engine.researcher = MagicMock()
        engine.researcher.fs = None

        # attached 모드 WorkspaceManager mock
        ws_mock = MagicMock()
        ws_mock.is_attached_mode.return_value = True
        ws_mock.workspace_root = tmp_path
        engine.workspace = ws_mock

        result = engine._get_active_workspace_root_for_chat()
        assert result == tmp_path
