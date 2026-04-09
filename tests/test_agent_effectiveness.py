import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_case_dir(name: str) -> Path:
    base_dir = Path(".codex_pytest_tmp")
    base_dir.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{name}_", dir=base_dir))


def test_workspace_manager_collects_project_guidance():
    from src.workspace.manager import WorkspaceManager

    project_root = _make_case_dir("guidance")
    try:
        (project_root / "AGENTS.md").write_text("Follow repository rules.", encoding="utf-8")
        (project_root / "README.md").write_text("# Demo Project", encoding="utf-8")
        (project_root / ".github").mkdir()
        (project_root / ".github" / "copilot-instructions.md").write_text(
            "Use pytest for tests.",
            encoding="utf-8",
        )

        manager = WorkspaceManager(str(project_root), attached=True)
        meta = manager.create_project("repo", "")
        guidance = manager.get_project_guidance(meta.project_id)

        assert "Follow repository rules." in guidance
        assert "Use pytest for tests." in guidance
        assert "README.md" in guidance
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_shell_tool_python_syntax_check_uses_local_interpreter():
    from src.tools.shell import ShellTool

    tmp_path = _make_case_dir("syntax")
    try:
        shell = ShellTool(str(tmp_path))
        (tmp_path / "ok.py").write_text("print('ok')\n", encoding="utf-8")
        result = shell.check_python_syntax("ok.py")

        assert result.success is True
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_workspace_manager_chat_history_roundtrip():
    from src.workspace.manager import WorkspaceManager

    project_root = _make_case_dir("chat")
    try:
        manager = WorkspaceManager(str(project_root), attached=True)
        meta = manager.create_project("repo", "")
        manager.append_chat_message(meta.project_id, "user", "첫 질문")
        manager.append_chat_message(meta.project_id, "assistant", "첫 답변")

        history = manager.get_chat_history(meta.project_id, limit=10)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["content"] == "첫 답변"

        manager.clear_chat_history(meta.project_id)
        assert manager.get_chat_history(meta.project_id, limit=10) == []
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_engine_quick_mode_includes_project_guidance():
    from unittest.mock import patch

    from src.backends.base import BackendConfig, GenerateResponse, LLMBackend
    from src.setup import create_engine

    class DummyBackend(LLMBackend):
        def __init__(self):
            super().__init__(BackendConfig(model="dummy"))
            self.prompts = []

        def initialize(self):
            return True

        def generate(self, request):
            self.prompts.append(request.prompt)
            return GenerateResponse(success=True, content="ok")

        def generate_stream(self, request):
            yield "ok"

        def is_available(self):
            return True

        def list_models(self):
            return ["dummy"]

    project_root = _make_case_dir("quick")
    try:
        (project_root / "AGENTS.md").write_text("Always update tests together with code.", encoding="utf-8")
        (project_root / "service.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        dummy = DummyBackend()
        with patch("src.setup._create_backend", return_value=dummy):
            engine = create_engine(project_root=str(project_root))

        result = engine.run_quick("service.py에 테스트를 추가해줘", "coder")

        assert result == "ok"
        assert dummy.prompts
        assert "Always update tests together with code." in dummy.prompts[0]
    finally:
        shutil.rmtree(project_root, ignore_errors=True)
