from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_case_dir(name: str) -> Path:
    base_dir = Path(".codex_pytest_tmp")
    base_dir.mkdir(exist_ok=True)
    path = base_dir / f"{name}_{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _make_engine(project_root: Path, attached: bool = True):
    from src.backends.base import BackendConfig, GenerateResponse, LLMBackend
    from src.setup import create_engine

    class DummyBackend(LLMBackend):
        def __init__(self):
            super().__init__(BackendConfig(model="dummy"))

        def initialize(self):
            return True

        def generate(self, request):
            return GenerateResponse(success=True, content="ok")

        def generate_stream(self, request):
            yield "ok"

        def is_available(self):
            return True

        def list_models(self):
            return ["dummy"]

    with patch("src.setup._create_backend", return_value=DummyBackend()):
        if attached:
            return create_engine(project_root=str(project_root))
        return create_engine(workspace_root=str(project_root))


def _project_plan(task: str):
    from src.orchestration.messages import ProjectPlan

    return ProjectPlan(
        title="python project",
        objective=task,
        steps=[
            {
                "step_num": 1,
                "title": "implement",
                "description": task,
                "assigned_agent": "coder",
            }
        ],
    )


def _result(role, content="ok", success=True, artifacts=None):
    from src.orchestration.messages import AgentResult, AgentRole

    return AgentResult(
        task_id="task",
        agent_name=role.value.title(),
        agent_role=role,
        content=content,
        success=success,
        artifacts=artifacts or [],
    )


def test_managed_python_project_assigns_main_entrypoint():
    from src.orchestration.messages import AgentRole, Artifact, OrchestrationState

    workspace_root = _make_case_dir("managed_entrypoint")
    try:
        engine = _make_engine(workspace_root, attached=False)
        meta = engine.workspace.create_project("demo", "managed python project")

        state = OrchestrationState(project_id=meta.project_id, original_task="create python project")
        state.metadata["task_analysis"] = {
            "project_generation": {"is_new_project": True, "language": "python"},
        }
        state.add_result(
            _result(
                AgentRole.CODER,
                artifacts=[
                    Artifact(
                        name="generated_code.py",
                        artifact_type="code",
                        content="def greet(name):\n    return f'Hello, {name}!'\n",
                        language="python",
                    )
                ],
            )
        )

        engine._prepare_generated_project_artifacts(state)
        engine._save_artifacts(state)

        assert (workspace_root / meta.project_id / "main.py").exists()
    finally:
        shutil.rmtree(workspace_root, ignore_errors=True)


def test_python_project_validation_loop_repairs_until_runnable(monkeypatch):
    from src.orchestration.messages import AgentRole, Artifact

    project_root = _make_case_dir("validation_success") / "repo"
    project_root.mkdir(parents=True)
    try:
        engine = _make_engine(project_root)
        engine.max_iterations = 3

        monkeypatch.setattr(
            engine.manager,
            "analyze_task",
            lambda user_task, workspace_context="": {
                "task_type": "code",
                "needs_file_access": False,
                "needs_code_execution": True,
                "required_agents": ["coder", "reviewer", "tester", "document"],
            },
        )
        monkeypatch.setattr(engine.planner, "create_plan", lambda task, context=None: _project_plan(task))
        monkeypatch.setattr(engine, "_run_researcher", lambda *args, **kwargs: _result(AgentRole.RESEARCHER))
        monkeypatch.setattr(engine, "_run_reviewer", lambda *args, **kwargs: _result(AgentRole.REVIEWER))
        monkeypatch.setattr(
            engine,
            "_run_document_agent",
            lambda *args, **kwargs: _result(
                AgentRole.DOCUMENT,
                artifacts=[
                    Artifact(
                        name="README.md",
                        artifact_type="document",
                        content="# Demo\n",
                        file_path="README.md",
                    )
                ],
            ),
        ),

        responses = iter(
            [
                """File: app.py
```python
def greet(name: str) -> str:
    return f"Hello, {name}!"
```""",
                """File: main.py
```python
import argparse

from app import greet


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Greeting CLI")
    parser.add_argument("--name", default="world")
    parser.add_argument("--smoke-test", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.smoke_test:
        print(greet("smoke"))
        return 0
    print(greet(args.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```""",
            ]
        )
        monkeypatch.setattr(engine.coder, "generate", lambda prompt: next(responses))

        state = engine.run("파이썬으로 실행 가능한 인사 CLI 프로젝트를 만들어줘.")

        assert state.status.value == "completed"
        assert (project_root / "main.py").exists()
        smoke = engine.tester.run_python_entrypoint("main.py", args=["--smoke-test"])
        assert smoke["success"] is True
        assert state.metadata["project_validation"]["success"] is True
    finally:
        shutil.rmtree(project_root.parent, ignore_errors=True)


def test_python_project_validation_failure_blocks_completion(monkeypatch):
    from src.orchestration.messages import AgentRole

    project_root = _make_case_dir("validation_failure") / "repo"
    project_root.mkdir(parents=True)
    try:
        engine = _make_engine(project_root)
        engine.max_iterations = 2

        monkeypatch.setattr(
            engine.manager,
            "analyze_task",
            lambda user_task, workspace_context="": {
                "task_type": "code",
                "needs_file_access": False,
                "needs_code_execution": True,
                "required_agents": ["coder", "reviewer"],
            },
        )
        monkeypatch.setattr(engine.planner, "create_plan", lambda task, context=None: _project_plan(task))
        monkeypatch.setattr(engine, "_run_researcher", lambda *args, **kwargs: _result(AgentRole.RESEARCHER))
        monkeypatch.setattr(engine, "_run_reviewer", lambda *args, **kwargs: _result(AgentRole.REVIEWER))

        monkeypatch.setattr(
            engine.coder,
            "generate",
            lambda prompt: """File: app.py
```python
def greet(name: str) -> str:
    return f"Hello, {name}!"
```""",
        )

        state = engine.run("파이썬으로 실행 가능한 인사 CLI 프로젝트를 만들어줘.")

        assert state.status.value == "failed"
        assert state.metadata["project_validation"]["success"] is False
    finally:
        shutil.rmtree(project_root.parent, ignore_errors=True)


def test_python_project_validation_repairs_packaging_mismatch(monkeypatch):
    from src.orchestration.messages import AgentRole, Artifact

    project_root = _make_case_dir("validation_packaging") / "repo"
    project_root.mkdir(parents=True)
    try:
        engine = _make_engine(project_root)
        engine.max_iterations = 3

        monkeypatch.setattr(
            engine.manager,
            "analyze_task",
            lambda user_task, workspace_context="": {
                "task_type": "code",
                "needs_file_access": False,
                "needs_code_execution": True,
                "required_agents": ["coder", "reviewer", "tester", "document"],
            },
        )
        monkeypatch.setattr(engine.planner, "create_plan", lambda task, context=None: _project_plan(task))
        monkeypatch.setattr(engine, "_run_researcher", lambda *args, **kwargs: _result(AgentRole.RESEARCHER))
        monkeypatch.setattr(engine, "_run_reviewer", lambda *args, **kwargs: _result(AgentRole.REVIEWER))
        monkeypatch.setattr(
            engine,
            "_run_document_agent",
            lambda *args, **kwargs: _result(
                AgentRole.DOCUMENT,
                artifacts=[
                    Artifact(
                        name="README.md",
                        artifact_type="document",
                        content="# Demo\n",
                        file_path="README.md",
                    )
                ],
            ),
        )

        monkeypatch.setattr(
            engine.coder,
            "generate",
            lambda prompt: """File: main.py
```python
import argparse


def run_cli() -> None:
    parser = argparse.ArgumentParser(description="Greeting CLI")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()
    if args.smoke_test:
        print("smoke ok")
        return
    print("hello")


if __name__ == "__main__":
    run_cli()
```

File: pyproject.toml
```toml
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setual.build_meta"

[project]
name = "greeting-cli"
version = "0.1.0"

[project.scripts]
greet-me = "greeting_cli.main:run_cli"

[tool.setuptools.packages.find]
where = ["src"]
```""",
        )

        state = engine.run("파이썬으로 실행 가능한 인사 CLI 프로젝트를 만들어줘.")

        assert state.status.value == "completed"
        assert (project_root / "src" / "greeting_cli" / "main.py").exists()
        assert (project_root / "src" / "greeting_cli" / "__init__.py").exists()
        assert "project_entrypoint" in (project_root / "main.py").read_text(encoding="utf-8")
        smoke = engine.tester.run_python_entrypoint("main.py", args=["--smoke-test"])
        assert smoke["success"] is True
        packaging = state.metadata["project_validation"]["verification"]["packaging_result"]
        assert packaging["success"] is True
        assert "setuptools.build_meta" in (project_root / "pyproject.toml").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(project_root.parent, ignore_errors=True)


def test_python_project_save_skips_non_coder_noise_artifacts():
    from src.orchestration.messages import AgentRole, Artifact, OrchestrationState

    project_root = _make_case_dir("save_filter") / "repo"
    project_root.mkdir(parents=True)
    try:
        engine = _make_engine(project_root)
        project_id = engine.workspace.create_project("repo", "save filter test").project_id
        state = OrchestrationState(project_id=project_id, original_task="create python project")
        state.metadata["task_analysis"] = {
            "project_generation": {"is_new_project": True, "language": "python"},
        }
        state.add_result(
            _result(
                AgentRole.CODER,
                artifacts=[
                    Artifact(
                        name="main.py",
                        artifact_type="code",
                        content="print('hello')\n",
                        file_path="main.py",
                        language="python",
                    )
                ],
            )
        )
        state.add_result(
            _result(
                AgentRole.REVIEWER,
                artifacts=[
                    Artifact(
                        name="generated_code.sh",
                        artifact_type="code",
                        content="echo review\n",
                        language="bash",
                    )
                ],
            )
        )

        engine._save_artifacts(state)

        assert (project_root / "main.py").exists()
        assert not any((project_root / "generated").glob("*.sh")) if (project_root / "generated").exists() else True
    finally:
        shutil.rmtree(project_root.parent, ignore_errors=True)


def test_gate2_triggers_when_only_config_py_in_workspace(monkeypatch):
    """Unit test: Gate-2 fires when initial workspace has config.py and no new Python files
    were generated by the agent.

    Directly tests _run_python_project_validation_loop to isolate Gate-2 logic from
    _normalize_task_analysis and rebuild-cycle complexity.
    """
    from src.orchestration.messages import AgentRole, Artifact, OrchestrationState

    project_root = _make_case_dir("gate2_unit") / "repo"
    project_root.mkdir(parents=True)
    (project_root / "config.py").write_text(
        "DATA_DIR = 'data/'\nTARGET_COLUMNS = ['A1', 'A2']\n", encoding="utf-8"
    )
    try:
        engine = _make_engine(project_root)
        project_id = engine.workspace.create_project("repo", "gate2 test").project_id

        state = OrchestrationState(project_id=project_id, original_task="build data analysis project")
        state.metadata["task_analysis"] = {
            "project_generation": {"is_new_project": True, "language": "python"},
        }
        # Simulate: initial workspace already had config.py before the agent ran
        state.metadata["initial_visible_files"] = ["config.py"]

        # No coder results added — agent produced nothing new
        repair_calls = []

        def mock_request_missing(state_arg, attempt_arg, workspace_files):
            repair_calls.append(attempt_arg)
            main_content = (
                "import argparse\n\n"
                "def main():\n"
                "    p = argparse.ArgumentParser()\n"
                "    p.add_argument('--smoke-test', action='store_true')\n"
                "    args = p.parse_args()\n"
                "    print('ok')\n\n"
                "if __name__ == '__main__':\n"
                "    raise SystemExit(main())\n"
            )
            return _result(
                AgentRole.CODER,
                content=f"File: main.py\n```python\n{main_content}\n```",
                artifacts=[
                    Artifact(
                        name="main.py",
                        artifact_type="code",
                        content=main_content,
                        file_path="main.py",
                        language="python",
                    )
                ],
            )

        monkeypatch.setattr(engine, "_request_missing_python_implementation", mock_request_missing)

        result = engine._run_python_project_validation_loop(state)

        assert len(repair_calls) >= 1, "Gate-2 repair was never triggered"
        assert (project_root / "main.py").exists(), "main.py was not created by Gate-2 repair"
        assert result.success is True, (
            f"Validation failed after Gate-2 repair: {result.error}\n"
            f"attempt_logs: {result.metadata.get('attempt_logs')}"
        )
        smoke = engine.tester.run_python_entrypoint("main.py", args=["--smoke-test"])
        assert smoke["success"] is True, f"Smoke test failed: {smoke}"
    finally:
        shutil.rmtree(project_root.parent, ignore_errors=True)


def test_workspace_with_only_config_py_full_pipeline(monkeypatch):
    """End-to-end regression: workspace with config.py → agent generates only requirements.txt
    first (no Python) → Gate-2 triggers → repair creates main.py → validation passes.

    Task text must include 'python' + 'project' to trigger _normalize_task_analysis correctly.
    """
    from src.orchestration.messages import AgentRole, Artifact

    project_root = _make_case_dir("config_only_e2e") / "repo"
    project_root.mkdir(parents=True)
    (project_root / "config.py").write_text(
        "DATA_DIR = 'data/'\nTARGET_COLUMNS = ['A1', 'A2']\n", encoding="utf-8"
    )
    (project_root / "README.md").write_text("# Machining Analysis\n", encoding="utf-8")
    try:
        engine = _make_engine(project_root)
        engine.max_iterations = 3

        monkeypatch.setattr(engine.planner, "create_plan", lambda task, context=None: _project_plan(task))
        monkeypatch.setattr(engine, "_run_researcher", lambda *args, **kwargs: _result(AgentRole.RESEARCHER))
        monkeypatch.setattr(engine, "_run_reviewer", lambda *args, **kwargs: _result(AgentRole.REVIEWER))
        monkeypatch.setattr(
            engine,
            "_run_document_agent",
            lambda *args, **kwargs: _result(
                AgentRole.DOCUMENT,
                artifacts=[
                    Artifact(
                        name="README.md",
                        artifact_type="document",
                        content="# Machining Analysis\n",
                        file_path="README.md",
                    )
                ],
            ),
        )

        MAIN_PY = (
            "import argparse\n"
            "import sys\n"
            "sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))\n"
            "from config import DATA_DIR, TARGET_COLUMNS\n\n"
            "def main():\n"
            "    p = argparse.ArgumentParser(description='Analysis')\n"
            "    p.add_argument('--smoke-test', action='store_true')\n"
            "    args = p.parse_args()\n"
            "    if args.smoke_test:\n"
            "        print(f'OK: DATA_DIR={DATA_DIR}')\n"
            "        return 0\n"
            "    return 0\n\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n"
        )

        call_count = [0]

        def mock_generate(prompt):
            call_count[0] += 1
            if call_count[0] == 1:
                # Initial step: returns only requirements.txt (no Python source files)
                # This triggers Gate-2 since no new Python files are created
                return "File: requirements.txt\n```\nnumpy\npandas\nscikit-learn\n```"
            # Gate-2 repair call and any subsequent calls: return valid main.py
            return f"File: main.py\n```python\n{MAIN_PY}\n```"

        monkeypatch.setattr(engine.coder, "generate", mock_generate)

        # Task text MUST contain 'python' + 'project' to pass _normalize_task_analysis
        state = engine.run("Build a Python data analysis project using config.py settings.")

        assert (project_root / "main.py").exists(), "main.py was not created"
        assert state.metadata["project_validation"]["success"] is True, (
            f"Validation failed: {state.metadata['project_validation'].get('failure_summary')}"
        )
        assert state.status.value == "completed"
        smoke = engine.tester.run_python_entrypoint("main.py", args=["--smoke-test"])
        assert smoke["success"] is True, f"Smoke test failed: {smoke}"
    finally:
        shutil.rmtree(project_root.parent, ignore_errors=True)


def test_file_package_conflict_detection():
    """Regression test: detect module shadowing (data_processor.py + data_processor/ both exist)."""
    from src.orchestration.messages import OrchestrationState

    project_root = _make_case_dir("conflict_detect") / "repo"
    project_root.mkdir(parents=True)
    try:
        engine = _make_engine(project_root)
        project_id = engine.workspace.create_project("repo", "conflict test").project_id

        # Create conflicting files
        (project_root / "data_processor.py").write_text("def process(): pass\n", encoding="utf-8")
        (project_root / "data_processor").mkdir()
        (project_root / "data_processor" / "__init__.py").write_text("", encoding="utf-8")
        (project_root / "main.py").write_text("from data_processor import process\n", encoding="utf-8")

        state = OrchestrationState(project_id=project_id, original_task="test")
        python_files = ["main.py", "data_processor.py"]
        conflicts = engine._detect_file_package_conflicts(state, python_files)

        assert len(conflicts) == 1, f"Expected 1 conflict, got {conflicts}"
        assert conflicts[0]["module_name"] == "data_processor"
        assert conflicts[0]["type"] == "shadowed_by_package"
    finally:
        shutil.rmtree(project_root.parent, ignore_errors=True)


def test_self_import_is_excluded_from_entrypoint_discovery():
    """Unit test: _discover_python_entrypoint must skip files that import from their own module name.

    Scenario: analyze_data.py has both an __main__ block AND `from analyze_data import X`.
    Stage 3 content scan would normally select it (high score), but self-import detection
    must exclude it so it is never run as an entrypoint.
    """
    from src.orchestration.messages import OrchestrationState

    project_root = _make_case_dir("self_import_skip") / "repo"
    project_root.mkdir(parents=True)

    # File that self-imports — must be skipped by entrypoint discovery
    (project_root / "analyze_data.py").write_text(
        "from analyze_data import AnalysisEngine\n\n"
        "class AnalysisEngine:\n"
        "    def run(self): return 42\n\n"
        "if __name__ == '__main__':\n"
        "    engine = AnalysisEngine()\n"
        "    engine.run()\n",
        encoding="utf-8",
    )
    try:
        engine = _make_engine(project_root)
        project_id = engine.workspace.create_project("repo", "self-import test").project_id

        state = OrchestrationState(project_id=project_id, original_task="test")
        state.metadata["task_analysis"] = {
            "project_generation": {"is_new_project": True, "language": "python"},
        }

        # Stage 1 and 2 won't match (no main.py/__main__.py/manage.py)
        # Stage 3 would normally pick analyze_data.py (score=8+2=10), but self-import excludes it
        result = engine._discover_python_entrypoint(state)

        assert result is None, (
            f"Expected None (self-importing file must be excluded), got: {result}"
        )

        # Verify _check_self_import detects the pattern
        self_imports = engine._check_self_import("analyze_data.py", project_root)
        assert len(self_imports) > 0, "Self-import not detected by _check_self_import"
        assert any("analyze_data" in s for s in self_imports)
    finally:
        shutil.rmtree(project_root.parent, ignore_errors=True)


def test_self_import_triggers_repair_and_creates_main(monkeypatch):
    """Regression test: when analyze_data.py self-imports, the validation loop must:
    1. Detect the self-import before attempting execution.
    2. Call _repair_self_import_entrypoint.
    3. Agent creates separate main.py (no self-import) + fixed analyze_data.py.
    4. Validation passes with a real smoke test.
    """
    from src.orchestration.messages import AgentRole, Artifact, OrchestrationState

    project_root = _make_case_dir("self_import_repair") / "repo"
    project_root.mkdir(parents=True)

    # Pre-create the buggy file: analyze_data.py with self-import + __main__ block
    BUGGY = (
        "from analyze_data import AnalysisEngine\n\n"  # self-import!
        "class AnalysisEngine:\n"
        "    def run(self, smoke_test=False):\n"
        "        if smoke_test:\n"
        "            print('smoke ok')\n"
        "            return 0\n"
        "        return 0\n\n"
        "if __name__ == '__main__':\n"
        "    import argparse\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--smoke-test', action='store_true')\n"
        "    args = p.parse_args()\n"
        "    engine = AnalysisEngine()\n"
        "    raise SystemExit(engine.run(smoke_test=args.smoke_test))\n"
    )
    (project_root / "analyze_data.py").write_text(BUGGY, encoding="utf-8")

    # Fixed versions to return from repair
    FIXED_ANALYZE = (
        "class AnalysisEngine:\n"
        "    def run(self, smoke_test=False):\n"
        "        if smoke_test:\n"
        "            print('smoke ok')\n"
        "            return 0\n"
        "        return 0\n"
    )
    FIXED_MAIN = (
        "import argparse\n"
        "from analyze_data import AnalysisEngine\n\n"
        "def main():\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--smoke-test', action='store_true')\n"
        "    args = p.parse_args()\n"
        "    engine = AnalysisEngine()\n"
        "    return engine.run(smoke_test=args.smoke_test)\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )

    try:
        engine = _make_engine(project_root)
        project_id = engine.workspace.create_project("repo", "self-import repair test").project_id

        state = OrchestrationState(project_id=project_id, original_task="analyze data")
        state.metadata["task_analysis"] = {
            "project_generation": {"is_new_project": True, "language": "python"},
        }

        repair_calls = []

        def mock_repair_self_import(state_arg, failure_arg, attempt_arg):
            repair_calls.append(attempt_arg)
            assert failure_arg["phase"] == "self_import", f"Expected self_import phase, got: {failure_arg['phase']}"
            return _result(
                AgentRole.CODER,
                content=(
                    f"File: analyze_data.py\n```python\n{FIXED_ANALYZE}\n```\n"
                    f"File: main.py\n```python\n{FIXED_MAIN}\n```"
                ),
                artifacts=[
                    Artifact(
                        name="analyze_data.py",
                        artifact_type="code",
                        content=FIXED_ANALYZE,
                        file_path="analyze_data.py",
                        language="python",
                    ),
                    Artifact(
                        name="main.py",
                        artifact_type="code",
                        content=FIXED_MAIN,
                        file_path="main.py",
                        language="python",
                    ),
                ],
            )

        monkeypatch.setattr(engine, "_repair_self_import_entrypoint", mock_repair_self_import)

        result = engine._run_python_project_validation_loop(state)

        assert len(repair_calls) >= 1, "self-import repair was never triggered"
        assert (project_root / "main.py").exists(), "main.py was not created by repair"
        assert result.success is True, (
            f"Validation failed: {result.error}\nattempt_logs: {result.metadata.get('attempt_logs')}"
        )

        # Verify smoke test actually works
        smoke = engine.tester.run_python_entrypoint("main.py", args=["--smoke-test"])
        assert smoke["success"] is True, f"Smoke test failed: {smoke}"

        # Verify analyze_data.py no longer self-imports
        fixed_content = (project_root / "analyze_data.py").read_text(encoding="utf-8")
        assert "from analyze_data import" not in fixed_content, (
            "analyze_data.py still contains self-import after repair"
        )
    finally:
        shutil.rmtree(project_root.parent, ignore_errors=True)


def test_self_import_check_detects_various_patterns():
    """Unit test: _check_self_import correctly detects self-import patterns."""
    project_root = _make_case_dir("self_import_check") / "repo"
    project_root.mkdir(parents=True)
    try:
        engine = _make_engine(project_root)

        # Case 1: `from module import X` — should be detected
        (project_root / "mymodule.py").write_text(
            "from mymodule import MyClass\nclass MyClass: pass\n", encoding="utf-8"
        )
        assert engine._check_self_import("mymodule.py", project_root) != []

        # Case 2: `import module` — should be detected
        (project_root / "runner.py").write_text(
            "import runner\nprint('hi')\n", encoding="utf-8"
        )
        assert engine._check_self_import("runner.py", project_root) != []

        # Case 3: No self-import — should return empty
        (project_root / "main.py").write_text(
            "from mymodule import MyClass\nif __name__ == '__main__': pass\n", encoding="utf-8"
        )
        assert engine._check_self_import("main.py", project_root) == []

        # Case 4: Import from a different module with similar name — not a self-import
        (project_root / "analyze.py").write_text(
            "from analyze_data import X\npass\n", encoding="utf-8"
        )
        assert engine._check_self_import("analyze.py", project_root) == []

        # Case 5: Non-existent file — should return empty gracefully
        assert engine._check_self_import("nonexistent.py", project_root) == []
    finally:
        shutil.rmtree(project_root.parent, ignore_errors=True)
