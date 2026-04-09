import sys
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_case_dir(name: str) -> Path:
    base_dir = Path(".codex_pytest_tmp")
    base_dir.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{name}_", dir=base_dir))


def test_status_snapshot_reports_project_backend_and_history(monkeypatch):
    import src.main as main
    from src.backends.base import BackendConfig, LLMBackend
    from src.workspace.manager import WorkspaceManager

    class DummyBackend(LLMBackend):
        def __init__(self):
            super().__init__(BackendConfig(model="dummy"))

        def initialize(self):
            return True

        def generate(self, request):  # pragma: no cover - not used here
            raise NotImplementedError

        def generate_stream(self, request):  # pragma: no cover - not used here
            yield ""

        def is_available(self):
            return True

        def list_models(self):
            return ["dummy", "dummy-fast", "dummy-vision"]

    project_root = _make_case_dir("cli_status")
    try:
        (project_root / "AGENTS.md").write_text("Always keep tests updated.", encoding="utf-8")

        manager = WorkspaceManager(str(project_root), attached=True)
        meta = manager.create_project("repo", "attached test project")
        manager.append_chat_message(meta.project_id, "user", "hello")
        manager.append_chat_message(meta.project_id, "assistant", "world")
        manager.record_task(meta.project_id, "task-1", "check status", "ok", agent_name="manager")

        monkeypatch.setattr(main, "_create_backend", lambda cfg, backend_type: DummyBackend())
        cfg = {
            "workspace": {"root": "./workspaces"},
            "backend": {"default": "ollama", "timeout": 180, "retry_attempts": 3},
            "ollama": {
                "default_model": "dummy",
                "fast_model": "dummy-fast",
                "vision_model": "dummy-vision",
            },
            "memory": {
                "chunk_size": 512,
                "chunk_overlap": 64,
                "retrieval_top_k": 5,
                "max_indexed_files": 2000,
                "max_total_chunks": 25000,
                "max_chunks_per_file": 400,
                "max_index_file_size_mb": 2.5,
            },
        }

        snapshot = main._collect_status_snapshot(cfg, None, str(project_root), meta.project_id)

        assert snapshot["mode"] == "attached"
        assert snapshot["project"]["project_id"] == meta.project_id
        assert snapshot["session"]["chat_message_count"] == 2
        assert snapshot["session"]["recent_task_count"] == 1
        assert snapshot["backend"]["available"] is True
        assert snapshot["backend"]["missing_models"] == []
        assert snapshot["guidance"]["present"] is True
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_export_chat_history_writes_markdown_into_project():
    import src.main as main
    from src.workspace.manager import WorkspaceManager

    project_root = _make_case_dir("cli_export")
    try:
        manager = WorkspaceManager(str(project_root), attached=True)
        meta = manager.create_project("repo", "attached test project")
        engine = SimpleNamespace(workspace=manager)
        history = [
            {"role": "user", "content": "summarize the repository"},
            {"role": "assistant", "content": "here is the summary"},
        ]

        saved_path = main._export_chat_history(engine, meta.project_id, history, "exports/chat.md")

        saved = Path(saved_path)
        assert saved.exists()
        content = saved.read_text(encoding="utf-8")
        assert "# Local Team Agent Chat Export" in content
        assert "## user" in content
        assert "## assistant" in content
        assert "summarize the repository" in content
    finally:
        shutil.rmtree(project_root, ignore_errors=True)
