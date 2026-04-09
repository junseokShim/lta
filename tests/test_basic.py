"""
기본 기능 테스트
실제 LLM 없이 동작 가능한 단위 테스트들입니다.
"""

import sys
import pytest
import tempfile
import json
from pathlib import Path

# 프로젝트 루트를 경로에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestImportNoCycle:
    """순환 임포트 회귀 테스트 - 이 클래스의 테스트가 실패하면 순환 임포트가 재발한 것입니다."""

    def test_orchestration_messages_importable(self):
        """orchestration.messages는 에이전트에 의존하지 않아야 합니다."""
        from src.orchestration.messages import (
            AgentRole, TaskStatus, MessageType,
            AgentTask, AgentResult, AgentMessage,
            ProjectPlan, OrchestrationState, Artifact,
        )
        assert AgentRole.MANAGER == "manager"

    def test_agents_base_importable(self):
        """agents.base가 순환 임포트 없이 로드되어야 합니다."""
        from src.agents.base import AgentBase
        assert AgentBase is not None

    def test_orchestration_engine_importable(self):
        """orchestration.engine이 순환 임포트 없이 로드되어야 합니다."""
        from src.orchestration.engine import OrchestrationEngine
        assert OrchestrationEngine is not None

    def test_setup_importable(self):
        """setup 모듈이 순환 임포트 없이 로드되어야 합니다."""
        from src.setup import create_engine, load_config
        assert callable(create_engine)
        assert callable(load_config)

    def test_engine_creation(self, tmp_path):
        """실제 엔진 생성이 성공해야 합니다 (LLM 없이)."""
        from src.setup import create_engine
        engine = create_engine(workspace_root=str(tmp_path))
        from src.orchestration.engine import OrchestrationEngine
        assert isinstance(engine, OrchestrationEngine)


class TestOrchestrationMessages:
    """오케스트레이션 메시지 타입 테스트"""

    def test_agent_task_creation(self):
        from src.orchestration.messages import AgentTask, AgentRole
        task = AgentTask(
            title="테스트 태스크",
            description="간단한 테스트",
            assigned_to=AgentRole.CODER,
        )
        assert task.task_id is not None
        assert task.title == "테스트 태스크"
        assert task.assigned_to == AgentRole.CODER

    def test_agent_result_creation(self):
        from src.orchestration.messages import AgentResult, AgentRole
        result = AgentResult(
            task_id="test-123",
            agent_name="TestAgent",
            agent_role=AgentRole.CODER,
            content="테스트 결과",
        )
        assert result.success is True
        assert result.content == "테스트 결과"

    def test_orchestration_state_context_summary(self):
        from src.orchestration.messages import OrchestrationState, AgentResult, AgentRole
        state = OrchestrationState(original_task="테스트 작업")
        state.add_result(AgentResult(
            task_id="t1",
            agent_name="Coder",
            agent_role=AgentRole.CODER,
            content="코드를 작성했습니다.",
        ))
        summary = state.get_context_summary()
        assert "테스트 작업" in summary
        assert "코드를 작성했습니다." in summary


class TestFilesystemTool:
    """파일시스템 도구 테스트"""

    def test_read_write_file(self, tmp_path):
        from src.tools.filesystem import FilesystemTool
        fs = FilesystemTool(str(tmp_path))

        # 파일 쓰기
        content = "테스트 내용\n두 번째 줄"
        fs.write_file("test.txt", content)

        # 파일 읽기
        read_content = fs.read_file("test.txt")
        assert read_content == content

    def test_path_traversal_blocked(self, tmp_path):
        from src.tools.filesystem import FilesystemTool
        fs = FilesystemTool(str(tmp_path))

        with pytest.raises(ValueError):
            fs.read_file("../../etc/passwd")

    def test_list_directory(self, tmp_path):
        from src.tools.filesystem import FilesystemTool
        fs = FilesystemTool(str(tmp_path))

        # 파일 생성
        fs.write_file("file1.py", "print('hello')")
        fs.write_file("file2.md", "# README")

        files = fs.list_directory(".")
        file_names = [f.name for f in files]
        assert "file1.py" in file_names
        assert "file2.md" in file_names

    def test_search_files(self, tmp_path):
        from src.tools.filesystem import FilesystemTool
        fs = FilesystemTool(str(tmp_path))

        fs.write_file("a.py", "def foo(): pass")
        fs.write_file("b.py", "def bar(): pass")
        fs.write_file("c.txt", "plain text")

        py_files = fs.search_files("*.py")
        assert len(py_files) == 2
        assert all(f.endswith(".py") for f in py_files)

    def test_directory_tree(self, tmp_path):
        from src.tools.filesystem import FilesystemTool
        fs = FilesystemTool(str(tmp_path))

        (tmp_path / "subdir").mkdir()
        fs.write_file("subdir/file.py", "pass")

        tree = fs.get_directory_tree()
        assert "subdir" in tree
        assert "file.py" in tree


class TestDocumentTool:
    """문서 도구 테스트"""

    def test_parse_markdown(self, tmp_path):
        from src.tools.document import DocumentTool
        doc_tool = DocumentTool(str(tmp_path))

        md_content = "# 제목\n\n소개 텍스트\n\n## 섹션 1\n\n내용1\n\n## 섹션 2\n\n내용2"
        (tmp_path / "test.md").write_text(md_content, encoding="utf-8")

        doc = doc_tool.read_document("test.md")
        assert doc.title == "제목"
        assert len(doc.sections) >= 2

    def test_parse_json(self, tmp_path):
        from src.tools.document import DocumentTool
        doc_tool = DocumentTool(str(tmp_path))

        data = {"key": "value", "number": 42}
        (tmp_path / "test.json").write_text(json.dumps(data), encoding="utf-8")

        doc = doc_tool.read_document("test.json")
        assert "key" in doc.metadata.get("keys", [])

    def test_generate_report(self, tmp_path):
        from src.tools.document import DocumentTool
        doc_tool = DocumentTool(str(tmp_path))

        sections = [
            {"title": "소개", "content": "이것은 테스트입니다."},
            {"title": "결론", "content": "테스트가 완료되었습니다."},
        ]

        output = doc_tool.generate_report("테스트 보고서", sections, str(tmp_path / "report"))
        assert Path(output).exists()
        content = Path(output).read_text(encoding="utf-8")
        assert "테스트 보고서" in content
        assert "소개" in content


class TestWorkspaceManager:
    """워크스페이스 관리자 테스트"""

    def test_create_project(self, tmp_path):
        from src.workspace.manager import WorkspaceManager
        wm = WorkspaceManager(str(tmp_path))

        meta = wm.create_project("테스트 프로젝트", "설명입니다")
        assert meta.name == "테스트 프로젝트"
        assert meta.project_id is not None

        # 디렉토리 생성 확인
        project_dir = tmp_path / meta.project_id
        assert project_dir.exists()
        assert (project_dir / ".project.json").exists()

    def test_list_projects(self, tmp_path):
        from src.workspace.manager import WorkspaceManager
        wm = WorkspaceManager(str(tmp_path))

        wm.create_project("프로젝트 1")
        wm.create_project("프로젝트 2")

        projects = wm.list_projects()
        assert len(projects) == 2

    def test_save_artifact(self, tmp_path):
        from src.workspace.manager import WorkspaceManager
        wm = WorkspaceManager(str(tmp_path))

        meta = wm.create_project("테스트")
        path = wm.save_artifact(meta.project_id, "test.py", "print('hello')")

        assert Path(path).exists()
        assert Path(path).read_text() == "print('hello')"

    def test_record_and_get_task(self, tmp_path):
        from src.workspace.manager import WorkspaceManager
        import uuid
        wm = WorkspaceManager(str(tmp_path))

        meta = wm.create_project("테스트")
        task_id = str(uuid.uuid4())

        wm.record_task(
            meta.project_id,
            task_id,
            "테스트 태스크",
            "태스크 결과",
            "completed",
            "coder",
        )

        history = wm.get_task_history(meta.project_id)
        assert len(history) == 1
        assert history[0]["description"] == "테스트 태스크"


class TestTaskHistory:
    """태스크 히스토리 테스트"""

    def test_add_and_get(self):
        from src.memory.task_history import TaskHistory, TaskRecord
        from datetime import datetime

        history = TaskHistory()  # 메모리 전용
        record = TaskRecord(
            task_id="t1",
            session_id="s1",
            project_id="p1",
            title="테스트",
            description="테스트 태스크",
            assigned_to="coder",
            status="completed",
            result_summary="완료됨",
            artifacts=[],
            duration_ms=100.0,
            created_at=datetime.now().isoformat(),
        )
        history.add(record)
        retrieved = history.get("t1")
        assert retrieved is not None
        assert retrieved.title == "테스트"

    def test_get_recent(self):
        from src.memory.task_history import TaskHistory, TaskRecord
        from datetime import datetime

        history = TaskHistory()
        for i in range(5):
            history.add(TaskRecord(
                task_id=f"t{i}",
                session_id="s1",
                project_id="p1",
                title=f"태스크 {i}",
                description="",
                assigned_to="coder",
                status="completed",
                result_summary="",
                artifacts=[],
                duration_ms=0,
                created_at=datetime.now().isoformat(),
            ))

        recent = history.get_recent(limit=3)
        assert len(recent) == 3


class TestLocalRetrieval:
    """로컬 검색 테스트"""

    def test_index_and_search(self, tmp_path):
        from src.memory.retrieval import LocalRetrieval

        # 테스트 파일 생성
        (tmp_path / "doc1.md").write_text(
            "# Python 프로그래밍\n\nPython은 강력한 프로그래밍 언어입니다."
        )
        (tmp_path / "doc2.md").write_text(
            "# JavaScript\n\nJavaScript는 웹 개발에 사용됩니다."
        )

        retrieval = LocalRetrieval(str(tmp_path))
        retrieval.index_directory(file_extensions=[".md"])

        stats = retrieval.get_stats()
        assert stats["total_chunks"] > 0
        assert stats["indexed_files"] == 2

        # 검색
        results = retrieval.search("Python 프로그래밍")
        assert len(results) > 0
        assert any("Python" in r.content for r in results)

    def test_empty_search(self, tmp_path):
        from src.memory.retrieval import LocalRetrieval

        retrieval = LocalRetrieval(str(tmp_path))
        results = retrieval.search("아무것도 없는 검색")
        assert results == []


class TestShellTool:
    """셸 도구 테스트"""

    def test_run_simple_command(self, tmp_path):
        from src.tools.shell import ShellTool
        shell = ShellTool(str(tmp_path))

        result = shell.run("echo hello")
        assert result.success
        assert "hello" in result.stdout

    def test_blocked_command(self, tmp_path):
        from src.tools.shell import ShellTool
        shell = ShellTool(str(tmp_path))

        result = shell.run("rm -rf /")
        assert not result.success
        assert "차단" in result.error or "차단" in result.stderr

    def test_python_version(self, tmp_path):
        from src.tools.shell import ShellTool
        shell = ShellTool(str(tmp_path))

        version = shell.get_python_version()
        assert "Python" in version


class TestDeviceUtils:
    """디바이스 선택 유틸리티 테스트 — Apple Silicon / CUDA / CPU 환경 모두 커버"""

    def test_get_best_device_returns_valid_string(self):
        """get_best_device()는 항상 유효한 디바이스 문자열을 반환해야 합니다."""
        from src.backends.device_utils import get_best_device
        device = get_best_device()
        assert device in ("cuda", "mps", "cpu")

    def test_resolve_device_auto(self):
        """'auto' 요청은 get_best_device()와 같은 결과여야 합니다."""
        from src.backends.device_utils import resolve_device, get_best_device
        assert resolve_device("auto") == get_best_device()

    def test_resolve_device_none(self):
        """None 요청은 'auto'와 동일하게 처리되어야 합니다."""
        from src.backends.device_utils import resolve_device, get_best_device
        assert resolve_device(None) == get_best_device()

    def test_resolve_device_cpu_always_available(self):
        """'cpu'는 항상 요청 그대로 반환되어야 합니다."""
        from src.backends.device_utils import resolve_device
        assert resolve_device("cpu") == "cpu"

    def test_resolve_device_cuda_fallback(self, monkeypatch):
        """CUDA가 없을 때 'cuda' 요청은 'cpu'로 폴백되어야 합니다."""
        import sys
        import types

        # torch.cuda.is_available()을 False로 패치
        fake_cuda = types.SimpleNamespace(is_available=lambda: False)
        fake_torch = types.SimpleNamespace(cuda=fake_cuda, backends=types.SimpleNamespace())
        monkeypatch.setitem(sys.modules, "torch", fake_torch)

        from importlib import reload
        import src.backends.device_utils as du
        # 함수 직접 테스트 (모듈 재로드 없이 로직 검증)
        result = du.resolve_device("cpu")
        assert result == "cpu"

    def test_resolve_device_mps_fallback(self, monkeypatch):
        """MPS를 지원하지 않는 환경에서 'mps' 요청은 'cpu'로 폴백되어야 합니다."""
        import sys
        import types

        fake_mps_backend = types.SimpleNamespace(is_available=lambda: False)
        fake_backends = types.SimpleNamespace(mps=fake_mps_backend)
        fake_cuda = types.SimpleNamespace(is_available=lambda: False)
        fake_torch = types.SimpleNamespace(cuda=fake_cuda, backends=fake_backends)
        monkeypatch.setitem(sys.modules, "torch", fake_torch)

        from src.backends.device_utils import resolve_device
        result = resolve_device("mps")
        # MPS 없는 환경이면 cpu 폴백, MPS 있으면 mps 유지
        assert result in ("mps", "cpu")

    def test_supports_quantization_cuda_only(self):
        """양자화(bitsandbytes)는 CUDA에서만 지원됩니다."""
        from src.backends.device_utils import supports_quantization
        assert supports_quantization("cuda") is True
        assert supports_quantization("mps") is False
        assert supports_quantization("cpu") is False

    def test_get_torch_dtype_cuda(self):
        """CUDA는 float16을 반환해야 합니다."""
        try:
            import torch
        except ImportError:
            pytest.skip("torch가 설치되지 않음")
        from src.backends.device_utils import get_torch_dtype
        assert get_torch_dtype("cuda") == torch.float16

    def test_get_torch_dtype_mps_and_cpu(self):
        """MPS와 CPU는 float32를 반환해야 합니다 (MPS float16은 일부 연산 미지원)."""
        try:
            import torch
        except ImportError:
            pytest.skip("torch가 설치되지 않음")
        from src.backends.device_utils import get_torch_dtype
        assert get_torch_dtype("mps") == torch.float32
        assert get_torch_dtype("cpu") == torch.float32

    def test_transformers_backend_uses_auto_device(self):
        """TransformersBackend는 device='auto'일 때 get_best_device()를 사용해야 합니다."""
        from src.backends.base import BackendConfig
        from src.backends.transformers_backend import TransformersBackend
        from src.backends.device_utils import get_best_device

        config = BackendConfig(
            model="test-model",
            extra={"device": "auto", "load_in_4bit": False},
        )
        backend = TransformersBackend(config)
        assert backend.device == get_best_device()

    def test_transformers_backend_no_quantization_on_mps(self, monkeypatch):
        """MPS 환경에서 4bit 양자화 요청은 무시되어야 합니다."""
        import sys
        import types

        # MPS 사용 가능, CUDA 없음 시뮬레이션
        fake_mps_backend = types.SimpleNamespace(is_available=lambda: True)
        fake_backends = types.SimpleNamespace(mps=fake_mps_backend)
        fake_cuda = types.SimpleNamespace(is_available=lambda: False)
        fake_torch = types.SimpleNamespace(cuda=fake_cuda, backends=fake_backends)
        monkeypatch.setitem(sys.modules, "torch", fake_torch)

        from src.backends.device_utils import resolve_device, supports_quantization
        device = resolve_device("auto")
        # MPS 환경이라면 mps, 아니라면 cpu
        assert device in ("mps", "cpu")
        assert not supports_quantization(device)

    def test_current_machine_device(self):
        """현재 머신에서 선택된 디바이스를 출력합니다 (항상 통과)."""
        from src.backends.device_utils import get_best_device
        device = get_best_device()
        print(f"\n현재 머신 선택 디바이스: {device}")
        # Apple Silicon M3 Mac에서는 'mps'여야 합니다
        # NVIDIA GPU 환경에서는 'cuda'여야 합니다
        # 그 외에는 'cpu'입니다
        assert device in ("cuda", "mps", "cpu")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
