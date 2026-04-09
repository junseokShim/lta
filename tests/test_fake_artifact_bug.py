"""
Regression tests for the fake-artifact / blueprint-completion bug.

Acceptance criteria:
- If the model returns only blueprint / pseudo-code / file-tree content:
    * the run must NOT be marked `completed`
    * the content must NOT be saved as a usable artifact
- If the model returns real implementation files:
    * they must be saved as visible files in the attached project root
- `completed` status must only happen when there are real implementation files.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine_with_mock_backend(llm_response: str, tmp_path: Path):
    """
    Create a minimal OrchestrationEngine whose LLM always returns `llm_response`.
    Uses attached mode (tmp_path is the project root).
    """
    from src.backends.base import BackendConfig, GenerateResponse, LLMBackend
    from src.setup import create_engine

    class FixedResponseBackend(LLMBackend):
        def initialize(self) -> bool:
            return True

        def is_available(self) -> bool:
            return True

        def list_models(self) -> list:
            return ["test"]

        def generate(self, request) -> GenerateResponse:
            return GenerateResponse(content=llm_response, model="test")

        def generate_stream(self, request):
            yield llm_response

    backend_instance = FixedResponseBackend(BackendConfig(model="test"))

    with patch("src.setup._create_backend", return_value=backend_instance):
        engine = create_engine(project_root=str(tmp_path))

    return engine


# ---------------------------------------------------------------------------
# Unit tests for _looks_like_blueprint_response
# ---------------------------------------------------------------------------

class TestBlueprintDetection:
    """Tests for OrchestrationEngine._looks_like_blueprint_response"""

    def _engine(self):
        from src.setup import create_engine
        return create_engine(workspace_root="/tmp")

    def _make_artifacts(self, content: str, source: str = "code_block", has_explicit_path: bool = False):
        from src.orchestration.messages import Artifact
        return [
            Artifact(
                name="generated_code.py" if not has_explicit_path else "src/main.py",
                artifact_type="code",
                content=content,
                file_path="src/main.py" if has_explicit_path else None,
                metadata={
                    "source": source,
                    "has_explicit_path": has_explicit_path,
                },
            )
        ]

    # --- Should be detected as blueprint (must return True) ---

    def test_detects_unicode_file_tree(self):
        engine = self._engine()
        content = textwrap.dedent("""\
            Here is the project structure:

            multimodal_prediction_system/
            ├── data/
            │   └── raw/
            └── src/
                └── main.py
        """)
        artifacts = self._make_artifacts(content, source="raw_response")
        assert engine._looks_like_blueprint_response(content, artifacts) is True

    def test_detects_ascii_file_tree(self):
        engine = self._engine()
        content = textwrap.dedent("""\
            Project layout:

            project/
            |-- data/
            |-- src/
            +-- tests/
        """)
        artifacts = self._make_artifacts(content, source="raw_response")
        assert engine._looks_like_blueprint_response(content, artifacts) is True

    def test_detects_directory_lines_ending_with_slash(self):
        """3+ lines ending with / → directory tree, no Unicode box chars needed."""
        engine = self._engine()
        content = textwrap.dedent("""\
            multimodal_prediction_system/
            data/
            src/
            tests/
        """)
        artifacts = self._make_artifacts(content, source="raw_response")
        assert engine._looks_like_blueprint_response(content, artifacts) is True

    def test_detects_english_blueprint_terms(self):
        engine = self._engine()
        content = "Phase 1: Data collection\nPhase 2: Model training\nDeliverable: Working prototype"
        artifacts = self._make_artifacts(content, source="raw_response")
        assert engine._looks_like_blueprint_response(content, artifacts) is True

    def test_detects_korean_blueprint_terms(self):
        engine = self._engine()
        content = "파일 구조:\n- data/\n- src/\n\n단계별 계획: 1단계부터 시작합니다."
        artifacts = self._make_artifacts(content, source="raw_response")
        assert engine._looks_like_blueprint_response(content, artifacts) is True

    def test_detects_raw_response_with_no_code_patterns(self):
        """raw_response artifact with no def/class/import = blueprint."""
        engine = self._engine()
        content = "This is a high-level description of what the system will do. No code here."
        artifacts = self._make_artifacts(content, source="raw_response")
        assert engine._looks_like_blueprint_response(content, artifacts) is True

    def test_detects_architecture_keyword(self):
        engine = self._engine()
        content = "## Architecture\n\nThe system follows a microservices architecture."
        artifacts = self._make_artifacts(content, source="raw_response")
        assert engine._looks_like_blueprint_response(content, artifacts) is True

    def test_detects_pseudo_code_term(self):
        engine = self._engine()
        content = "Here is the pseudo-code for the algorithm:\n```\nfor each item: process(item)\n```"
        artifacts = self._make_artifacts(content, source="code_block")
        assert engine._looks_like_blueprint_response(content, artifacts) is True

    # --- Should NOT be detected as blueprint (must return False) ---

    def test_allows_real_python_code(self):
        engine = self._engine()
        content = textwrap.dedent("""\
            Here is the implementation:

            ```python
            import os

            def main():
                data = load_data()
                model = train(data)
                return model
            ```
        """)
        from src.orchestration.messages import Artifact
        artifacts = [
            Artifact(
                name="main.py",
                artifact_type="code",
                content="import os\n\ndef main():\n    data = load_data()\n    model = train(data)\n    return model\n",
                file_path="main.py",
                metadata={"source": "code_block", "has_explicit_path": True},
            )
        ]
        assert engine._looks_like_blueprint_response(content, artifacts) is False

    def test_allows_real_code_with_explicit_path(self):
        engine = self._engine()
        content = "```python\nclass Foo:\n    def bar(self): pass\n```"
        artifacts = self._make_artifacts("class Foo:\n    def bar(self): pass\n", has_explicit_path=True)
        assert engine._looks_like_blueprint_response(content, artifacts) is False

    def test_allows_raw_response_with_real_code_patterns(self):
        """If raw_response but has Python-like code, should NOT be flagged."""
        engine = self._engine()
        content = "def hello():\n    print('hello')\n\nhello()"
        artifacts = self._make_artifacts(content, source="raw_response")
        # No blueprint markers, has real code patterns → should not be flagged
        assert engine._looks_like_blueprint_response(content, artifacts) is False

    def test_allows_large_code_block_with_minor_blueprint_term(self):
        """Large, explicit-path artifact should not be blocked just by an 'architecture' mention."""
        engine = self._engine()
        real_code = "\n".join([f"def func_{i}(): pass" for i in range(60)])
        content = f"Clean architecture\n\n```python\n{real_code}\n```"
        from src.orchestration.messages import Artifact
        artifacts = [
            Artifact(
                name="app.py",
                artifact_type="code",
                content=real_code,
                file_path="app.py",
                metadata={"source": "code_block", "has_explicit_path": True},
            )
        ]
        assert engine._looks_like_blueprint_response(content, artifacts) is False


# ---------------------------------------------------------------------------
# Integration tests: full engine run with mock backend
# ---------------------------------------------------------------------------

class TestBlueprintRunNotCompleted:
    """
    When the coder returns a blueprint (both initial + retry), the run must be
    FAILED, not COMPLETED, and no artifact files must appear in the project root.
    """

    BLUEPRINT_RESPONSE = textwrap.dedent("""\
        Here is the plan for the multimodal prediction system:

        multimodal_prediction_system/
        ├── data/
        │   └── raw/
        └── src/
            └── main.py

        Phase 1: Data collection
        Phase 2: Model training architecture
        Phase 3: Deployment

        This is a high-level blueprint of the system.
    """)

    def test_blueprint_run_is_failed(self, tmp_path):
        engine = _make_engine_with_mock_backend(self.BLUEPRINT_RESPONSE, tmp_path)
        state = engine.run("create a multimodal prediction system")
        assert state.status.value == "failed", (
            f"Expected FAILED but got {state.status.value!r}. "
            "A blueprint response must not be marked as completed."
        )

    def test_blueprint_run_has_no_project_root_files(self, tmp_path):
        engine = _make_engine_with_mock_backend(self.BLUEPRINT_RESPONSE, tmp_path)
        engine.run("create a multimodal prediction system")

        # Check that no .py files were written directly in the project root
        # (.lta/ is allowed for metadata/logs)
        generated_files = [
            p for p in tmp_path.rglob("*.py")
            if ".lta" not in p.parts
        ]
        assert generated_files == [], (
            f"Blueprint response must not create .py files in the project root. "
            f"Found: {generated_files}"
        )

    def test_blueprint_run_final_output_explains_failure(self, tmp_path):
        engine = _make_engine_with_mock_backend(self.BLUEPRINT_RESPONSE, tmp_path)
        state = engine.run("create a multimodal prediction system")
        assert state.final_output, "final_output must not be empty even for failed runs"
        # Should contain some indication that it failed / no real files created
        lower = state.final_output.lower()
        assert any(
            kw in lower for kw in ["failed", "실패", "blueprint", "의사코드", "구현", "fail"]
        ), f"final_output should explain failure reason, got: {state.final_output[:300]}"

    def test_blueprint_dot_lta_artifacts_not_created(self, tmp_path):
        """Fake blueprint content must not be saved inside .lta/artifacts/"""
        engine = _make_engine_with_mock_backend(self.BLUEPRINT_RESPONSE, tmp_path)
        engine.run("create a multimodal prediction system")

        lta_artifacts = list((tmp_path / ".lta" / "artifacts").glob("*.py")) if (
            (tmp_path / ".lta" / "artifacts").exists()
        ) else []
        assert lta_artifacts == [], (
            f"Blueprint content must not be saved in .lta/artifacts/. Found: {lta_artifacts}"
        )


class TestRealCodeRunCompleted:
    """
    When the coder returns real implementation code the run must be COMPLETED
    and the file must be visible in the project root.
    """

    REAL_CODE_RESPONSE = textwrap.dedent("""\
        Here is the implementation:

        `main.py`
        ```python
        import argparse

        def parse_args():
            parser = argparse.ArgumentParser()
            parser.add_argument("--input", required=True)
            return parser.parse_args()

        def main():
            args = parse_args()
            print(f"Processing: {args.input}")

        if __name__ == "__main__":
            main()
        ```

        This implements a basic CLI entry point.
    """)

    def test_real_code_run_is_completed(self, tmp_path):
        engine = _make_engine_with_mock_backend(self.REAL_CODE_RESPONSE, tmp_path)
        state = engine.run("write a CLI tool")
        assert state.status.value == "completed", (
            f"Expected COMPLETED but got {state.status.value!r}. "
            f"Final output: {state.final_output[:300]}"
        )

    def test_real_code_creates_visible_file(self, tmp_path):
        engine = _make_engine_with_mock_backend(self.REAL_CODE_RESPONSE, tmp_path)
        engine.run("write a CLI tool")

        # A .py file must be visible somewhere in the project root (outside .lta/)
        generated_files = [
            p for p in tmp_path.rglob("*.py")
            if ".lta" not in p.parts
        ]
        assert generated_files, (
            "Real code response must create at least one .py file in the project root. "
            f"Only found: {list(tmp_path.rglob('*'))}"
        )


# ---------------------------------------------------------------------------
# Unit tests for _validate_coder_output
# ---------------------------------------------------------------------------

class TestValidateCoderOutput:
    def _engine(self):
        from src.setup import create_engine
        return create_engine(workspace_root="/tmp")

    def test_empty_artifacts_fails(self):
        engine = self._engine()
        valid, error = engine._validate_coder_output("some content", [])
        assert valid is False
        assert error

    def test_blueprint_content_fails(self):
        from src.orchestration.messages import Artifact
        engine = self._engine()
        blueprint_content = "Phase 1: Setup\nPhase 2: Implementation\n\nproject/\n├── src/\n└── tests/"
        artifacts = [
            Artifact(
                name="generated_code.py",
                artifact_type="code",
                content=blueprint_content,
                metadata={"source": "raw_response", "has_explicit_path": False},
            )
        ]
        valid, error = engine._validate_coder_output(blueprint_content, artifacts)
        assert valid is False, "Blueprint response must fail validation"
        assert error

    def test_real_code_passes(self):
        from src.orchestration.messages import Artifact
        engine = self._engine()
        real_code = "import os\n\ndef main():\n    print('hello')\n"
        artifacts = [
            Artifact(
                name="main.py",
                artifact_type="code",
                content=real_code,
                file_path="main.py",
                metadata={"source": "code_block", "has_explicit_path": True},
            )
        ]
        valid, error = engine._validate_coder_output("Here is main.py:\n```python\n" + real_code + "```", artifacts)
        assert valid is True, f"Real code should pass validation. Error: {error}"


# ---------------------------------------------------------------------------
# Smoke test: file-tree-only response (the original repro case)
# ---------------------------------------------------------------------------

class TestOriginalRepro:
    """Reproduces the exact scenario from the bug report."""

    REPRO_RESPONSE = textwrap.dedent("""\
        multimodal_prediction_system/
        ├── data/
        └── src/
    """)

    def test_file_tree_only_is_not_completed(self, tmp_path):
        """The exact bug repro: file tree / blueprint → must be FAILED."""
        engine = _make_engine_with_mock_backend(self.REPRO_RESPONSE, tmp_path)
        state = engine.run("create multimodal prediction system")
        assert state.status.value == "failed", (
            "File-tree-only response must be FAILED, not completed. "
            f"Got status={state.status.value!r}, final_output={state.final_output[:300]}"
        )

    def test_no_generated_code_py_in_lta_artifacts(self, tmp_path):
        """generated_code.py must NOT appear in .lta/artifacts/"""
        engine = _make_engine_with_mock_backend(self.REPRO_RESPONSE, tmp_path)
        engine.run("create multimodal prediction system")

        bad_path = tmp_path / ".lta" / "artifacts" / "generated_code.py"
        assert not bad_path.exists(), (
            f"Blueprint content must not be saved at {bad_path}. "
            "The run should have been rejected before artifact saving."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
