import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_document_tool_reads_pdf_with_mocked_pypdf2(tmp_path, monkeypatch):
    from src.tools.document import DocumentTool

    class FakePage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class FakeReader:
        def __init__(self, _handle):
            self.pages = [FakePage("First page"), FakePage("Second page")]
            self.metadata = {"/Title": "Mock PDF"}

    monkeypatch.setitem(sys.modules, "PyPDF2", types.SimpleNamespace(PdfReader=FakeReader))

    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    tool = DocumentTool(str(tmp_path))
    doc = tool.read_document("sample.pdf")

    assert doc.file_type == "pdf"
    assert doc.title == "Mock PDF"
    assert "First page" in doc.content
    assert doc.metadata["page_count"] == 2


def test_document_tool_creates_pptx_with_mocked_python_pptx(tmp_path, monkeypatch):
    from src.tools.document import DocumentTool

    class FakeParagraph:
        def __init__(self):
            self.text = ""
            self.level = 0

    class FakeTextFrame:
        def __init__(self):
            self.paragraphs = [FakeParagraph()]

        def clear(self):
            self.paragraphs = [FakeParagraph()]

        def add_paragraph(self):
            paragraph = FakeParagraph()
            self.paragraphs.append(paragraph)
            return paragraph

    class FakePlaceholder:
        def __init__(self):
            self.text = ""
            self.text_frame = FakeTextFrame()

    class FakeShapes:
        def __init__(self):
            self.title = FakePlaceholder()

        def add_textbox(self, *_args):
            return types.SimpleNamespace(text_frame=FakeTextFrame())

    class FakeSlide:
        def __init__(self):
            self.shapes = FakeShapes()
            self.placeholders = [FakePlaceholder(), FakePlaceholder()]

    class FakeSlides(list):
        def add_slide(self, _layout):
            slide = FakeSlide()
            self.append(slide)
            return slide

    class FakePresentation:
        def __init__(self):
            self.slide_layouts = [object(), object()]
            self.slides = FakeSlides()

        def save(self, path):
            Path(path).write_bytes(b"PPTX")

    monkeypatch.setitem(sys.modules, "pptx", types.SimpleNamespace(Presentation=FakePresentation))
    monkeypatch.setitem(sys.modules, "pptx.util", types.SimpleNamespace(Inches=lambda value: value))

    source = tmp_path / "slides.md"
    source.write_text("# Demo Deck\n\n## Overview\n- first\n- second", encoding="utf-8")

    tool = DocumentTool(str(tmp_path))
    output = tool.create_presentation_from_file("slides.md", "out/demo.pptx")

    assert Path(output).exists()
    assert Path(output).suffix == ".pptx"
    assert Path(output).read_bytes() == b"PPTX"


def test_workspace_manager_attached_mode_uses_existing_project_root(tmp_path):
    from src.workspace.manager import WorkspaceManager

    project_root = tmp_path / "existing-project"
    project_root.mkdir()

    wm = WorkspaceManager(str(project_root), attached=True)
    meta = wm.create_project("existing-project", "attached workspace")

    assert wm.is_attached_mode() is True
    assert wm.get_project_path(meta.project_id) == project_root.resolve()

    artifact_path = Path(wm.save_artifact(meta.project_id, "note.txt", "hello"))
    project_file_path = Path(wm.save_project_file(meta.project_id, "src/app.py", "print('hi')"))

    assert artifact_path.parent == project_root.resolve() / ".lta" / "artifacts"
    assert artifact_path.read_text(encoding="utf-8") == "hello"
    assert project_file_path == project_root.resolve() / "src" / "app.py"
    assert project_file_path.read_text(encoding="utf-8") == "print('hi')"


def test_create_engine_supports_attached_project_root(tmp_path):
    from src.setup import create_engine

    project_root = tmp_path / "repo"
    project_root.mkdir()

    engine = create_engine(project_root=str(project_root))

    assert engine.workspace.is_attached_mode() is True
    assert engine.coder.fs.workspace_root == project_root.resolve()
    assert engine.manager.doc.workspace_root == project_root.resolve()


def test_engine_saves_generic_code_artifacts_into_visible_generated_dir(tmp_path):
    from src.orchestration.messages import AgentResult, AgentRole, Artifact, OrchestrationState
    from src.setup import create_engine

    project_root = tmp_path / "repo"
    project_root.mkdir()

    engine = create_engine(project_root=str(project_root))
    meta = engine.workspace.create_project("repo", "attached test project")

    artifact = Artifact(
        name="generated_code.py",
        artifact_type="code",
        content="print('hello from attached mode')",
        language="python",
    )
    state = OrchestrationState(project_id=meta.project_id, original_task="create a file")
    state.add_result(
        AgentResult(
            task_id="task-1",
            agent_name="Coder",
            agent_role=AgentRole.CODER,
            content="generated code",
            artifacts=[artifact],
        )
    )

    engine._save_artifacts(state)

    assert artifact.file_path is not None
    assert artifact.file_path.startswith("generated/")
    saved_path = project_root / Path(artifact.file_path)
    assert saved_path.exists()
    assert saved_path.read_text(encoding="utf-8") == "print('hello from attached mode')"
    assert not (project_root / ".lta" / "artifacts" / "generated_code.py").exists()


def test_engine_rejects_blueprint_style_coder_output(tmp_path):
    from src.orchestration.messages import Artifact
    from src.setup import create_engine

    project_root = tmp_path / "repo"
    project_root.mkdir()

    engine = create_engine(project_root=str(project_root))
    artifacts = [
        Artifact(
            name="generated_code.py",
            artifact_type="code",
            content="# Pseudo-code for the Main Model\nclass FusionPredictor:\n    pass",
            language="python",
            metadata={"has_explicit_path": False, "source": "code_block"},
        )
    ]
    content = """# Project Blueprint

## Executive Summary

multimodal_prediction_system/
├── data/
└── src/

Phase 1: planning
"""

    valid, error = engine._validate_coder_output(content, artifacts)

    assert valid is False
    assert "의사코드" in error


def test_save_artifacts_skips_failed_results(tmp_path):
    from src.orchestration.messages import AgentResult, AgentRole, Artifact, OrchestrationState
    from src.setup import create_engine

    project_root = tmp_path / "repo"
    project_root.mkdir()

    engine = create_engine(project_root=str(project_root))
    meta = engine.workspace.create_project("repo", "attached test project")

    artifact = Artifact(
        name="generated_code.py",
        artifact_type="code",
        content="print('should not be saved')",
        language="python",
    )
    state = OrchestrationState(project_id=meta.project_id, original_task="create a file")
    state.add_result(
        AgentResult(
            task_id="task-1",
            agent_name="Coder",
            agent_role=AgentRole.CODER,
            content="failed generated code",
            artifacts=[artifact],
            success=False,
            error="코더가 실제 구현 대신 설계 문서/의사코드를 반환했습니다.",
        )
    )

    engine._save_artifacts(state)

    assert not (project_root / "generated").exists()
    assert not (project_root / ".lta" / "artifacts" / "generated_code.py").exists()


def test_web_search_tool_parses_mocked_duckduckgo_results(monkeypatch):
    from src.tools.web_search import WebSearchTool

    class FakeLink:
        def get(self, key, default=""):
            if key == "href":
                return "/l/?uddg=https%3A%2F%2Fexample.com%2Farticle"
            return default

        def get_text(self, *_args, **_kwargs):
            return "Example Result"

    class FakeSnippet:
        def get_text(self, *_args, **_kwargs):
            return "Example snippet"

    class FakeNode:
        def select_one(self, selector):
            if selector == ".result__a":
                return FakeLink()
            if selector == ".result__snippet":
                return FakeSnippet()
            return None

    class FakeSoup:
        def __init__(self, *_args, **_kwargs):
            pass

        def select(self, selector):
            if selector == ".result":
                return [FakeNode()]
            return []

    class FakeResponse:
        text = "<html></html>"

        def raise_for_status(self):
            return None

    monkeypatch.setitem(sys.modules, "bs4", types.SimpleNamespace(BeautifulSoup=FakeSoup))

    tool = WebSearchTool()
    monkeypatch.setattr(tool.session, "get", lambda *args, **kwargs: FakeResponse())

    results = tool.search("example", max_results=3)

    assert len(results) == 1
    assert results[0].title == "Example Result"
    assert results[0].url == "https://example.com/article"


def test_backend_retry_reduces_tokens_and_uses_fast_model_on_timeout():
    from src.backends.base import BackendConfig, GenerateRequest, GenerateResponse, LLMBackend

    class DummyBackend(LLMBackend):
        def __init__(self, config):
            super().__init__(config)
            self.calls = []

        def initialize(self):
            return True

        def generate(self, request):
            self.calls.append(
                (
                    request.model_override,
                    request.max_tokens_override,
                    request.temperature_override,
                )
            )
            if len(self.calls) < 3:
                return GenerateResponse(success=False, error="timeout")
            return GenerateResponse(success=True, content="ok")

        def generate_stream(self, request):
            yield "ok"

        def is_available(self):
            return True

        def list_models(self):
            return ["base", "fast"]

    backend = DummyBackend(
        BackendConfig(
            model="base",
            max_tokens=1000,
            retry_attempts=3,
            extra={"fast_model": "fast"},
        )
    )

    response = backend.generate_with_retry(GenerateRequest(prompt="hello"))

    assert response.success is True
    assert backend.calls[0] == (None, None, None)
    assert backend.calls[1][0] == "fast"
    assert backend.calls[1][1] == 500
    assert backend.calls[1][2] == 0.3


def test_backend_retry_falls_back_on_server_error_and_reduces_context():
    from src.backends.base import BackendConfig, GenerateRequest, GenerateResponse, LLMBackend

    class DummyBackend(LLMBackend):
        def __init__(self, config):
            super().__init__(config)
            self.calls = []

        def initialize(self):
            return True

        def generate(self, request):
            self.calls.append(
                (
                    request.model_override,
                    request.max_tokens_override,
                    request.temperature_override,
                    request.context_length_override,
                )
            )
            if len(self.calls) < 2:
                return GenerateResponse(success=False, error="Ollama 요청 오류 (500): internal server error")
            return GenerateResponse(success=True, content="ok")

        def generate_stream(self, request):
            yield "ok"

        def is_available(self):
            return True

        def list_models(self):
            return ["base", "fast"]

    backend = DummyBackend(
        BackendConfig(
            model="base",
            max_tokens=1000,
            context_length=4096,
            retry_attempts=2,
            extra={"fast_model": "fast"},
        )
    )

    response = backend.generate_with_retry(GenerateRequest(prompt="hello"))

    assert response.success is True
    assert backend.calls[0] == (None, None, None, None)
    assert backend.calls[1][0] == "fast"
    assert backend.calls[1][1] == 500
    assert backend.calls[1][2] == 0.3
    assert backend.calls[1][3] == 4096
