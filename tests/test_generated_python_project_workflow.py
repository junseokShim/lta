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
