"""
에이전트 팀 생성 팩토리 모듈
설정 파일을 기반으로 전체 에이전트 팀을 초기화합니다.
"""

import os
from pathlib import Path
from typing import Optional

import yaml

from .backends.base import BackendConfig
from .backends.ollama_backend import OllamaBackend
from .agents.manager import ManagerAgent
from .agents.planner import PlannerAgent
from .agents.coder import CoderAgent
from .agents.reviewer import ReviewerAgent
from .agents.researcher import ResearcherAgent
from .agents.tester import TesterAgent
from .agents.document_agent import DocumentAgent
from .agents.vision_agent import VisionAgent
from .orchestration.engine import OrchestrationEngine
from .orchestration.messages import AgentRole
from .tools.filesystem import FilesystemTool
from .tools.document import DocumentTool
from .tools.image import ImageTool
from .tools.shell import ShellTool
from .tools.web_search import WebSearchTool
from .workspace.manager import WorkspaceManager
from .memory.task_history import TaskHistory
from .memory.retrieval import LocalRetrieval
from .logging_utils import setup_logging, get_logger

logger = get_logger("setup")


def load_config(config_path: Optional[str] = None) -> dict:
    """
    설정 파일 로드
    우선순위: 인수 > 환경변수 > default.yaml
    """
    try:
        from dotenv import load_dotenv

        env_file = Path(__file__).parent.parent / ".env"
        if env_file.exists():
            load_dotenv(env_file)
    except ImportError:
        pass

    if config_path is None:
        config_path = os.environ.get(
            "CONFIG_PATH",
            str(Path(__file__).parent.parent / "config" / "default.yaml")
        )

    config = {}
    config_file = Path(config_path)
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

    # 환경 변수로 덮어쓰기
    env_overrides = {
        "ollama.host": os.environ.get("OLLAMA_HOST"),
        "ollama.default_model": os.environ.get("OLLAMA_DEFAULT_MODEL"),
        "ollama.fast_model": os.environ.get("OLLAMA_FAST_MODEL"),
        "ollama.vision_model": os.environ.get("OLLAMA_VISION_MODEL"),
        "ollama.context_length": os.environ.get("OLLAMA_CONTEXT_LENGTH"),
        "workspace.root": os.environ.get("WORKSPACE_ROOT"),
        "backend.default": os.environ.get("DEFAULT_BACKEND"),
        "backend.timeout": os.environ.get("BACKEND_TIMEOUT"),
        "backend.retry_attempts": os.environ.get("BACKEND_RETRY_ATTEMPTS"),
        "logging.level": os.environ.get("LOG_LEVEL"),
    }

    for key_path, value in env_overrides.items():
        if value:
            _set_nested(config, key_path.split("."), _coerce_env_value(value))

    return config


def create_engine(
    config_path: Optional[str] = None,
    workspace_root: Optional[str] = None,
    project_id: Optional[str] = None,
    project_root: Optional[str] = None,
    on_status_update=None,
) -> OrchestrationEngine:
    """
    설정 기반 오케스트레이션 엔진 생성
    Args:
        config_path: 설정 파일 경로
        workspace_root: managed 모드용 워크스페이스 루트 경로
        project_id: 초기 프로젝트 ID
        project_root: attached 모드에서 직접 작업할 프로젝트 루트
        on_status_update: 상태 업데이트 콜백
    Returns: OrchestrationEngine
    """
    config = load_config(config_path)

    # 로깅 설정
    log_config = config.get("logging", {})
    setup_logging(
        level=log_config.get("level", "INFO"),
        log_file=log_config.get("file"),
    )

    # 워크스페이스 설정
    attached_mode = bool(project_root)
    ws_root = workspace_root or config.get("workspace", {}).get("root", "./workspaces")
    tool_root = str(Path(project_root).resolve()) if project_root else ws_root
    workspace = WorkspaceManager(
        workspace_root=tool_root if attached_mode else ws_root,
        attached=attached_mode,
    )

    # 백엔드 생성
    backend_type = config.get("backend", {}).get("default", "ollama")
    backend = _create_backend(config, backend_type)

    if not backend.is_available():
        logger.warning(
            f"백엔드 '{backend_type}'이 사용 불가능합니다. "
            f"Ollama가 실행 중인지 확인하세요: 'ollama serve'"
        )

    # 비전 백엔드 (비전 모델이 설정된 경우)
    vision_backend = None
    vision_model = config.get("ollama", {}).get("vision_model")
    if vision_model and backend_type == "ollama":
        from .backends.ollama_backend import OllamaBackend
        vision_config = BackendConfig(
            model=vision_model,
            extra={"host": config.get("ollama", {}).get("host", "http://localhost:11434")},
        )
        vision_backend = OllamaBackend(vision_config)

    # 도구 생성
    fs_config = config.get("tools", {}).get("filesystem", {})
    allowed_extensions = fs_config.get("allowed_extensions")
    fs_tool = FilesystemTool(
        workspace_root=tool_root,
        max_file_size_mb=fs_config.get("max_file_size_mb", 50),
        allowed_extensions=set(allowed_extensions) if allowed_extensions else None,
    )
    doc_tool = DocumentTool(workspace_root=tool_root)
    img_tool = ImageTool(workspace_root=tool_root, vision_backend=vision_backend)
    shell_tool = ShellTool(
        workspace_root=tool_root,
        timeout=config.get("tools", {}).get("shell", {}).get("timeout", 60),
    )
    web_search_tool = WebSearchTool(timeout=config.get("backend", {}).get("timeout", 120))

    # RAG 검색 초기화
    retrieval = LocalRetrieval(
        workspace_root=tool_root,
        chunk_size=config.get("memory", {}).get("chunk_size", 512),
        chunk_overlap=config.get("memory", {}).get("chunk_overlap", 64),
        top_k=config.get("memory", {}).get("retrieval_top_k", 5),
        max_indexed_files=config.get("memory", {}).get("max_indexed_files", 2000),
        max_total_chunks=config.get("memory", {}).get("max_total_chunks", 25000),
        max_chunks_per_file=config.get("memory", {}).get("max_chunks_per_file", 400),
        max_file_size_mb=config.get("memory", {}).get("max_index_file_size_mb", 2.5),
    )

    # 태스크 히스토리
    history = TaskHistory(db_path=str(workspace.get_session_db_path()))

    # 에이전트 공통 설정
    agent_config = config.get("agents", {})
    ollama_config = config.get("ollama", {})
    fast_model = ollama_config.get("fast_model")
    default_model = ollama_config.get("default_model")

    common_kwargs = dict(
        backend=backend,
        filesystem_tool=fs_tool,
        document_tool=doc_tool,
        image_tool=img_tool,
        shell_tool=shell_tool,
        web_search_tool=web_search_tool,
    )

    # 각 에이전트 생성
    manager = ManagerAgent(
        **common_kwargs,
        role=AgentRole.MANAGER,
        name="Manager",
    )

    planner = PlannerAgent(
        **common_kwargs,
        role=AgentRole.PLANNER,
        name="Planner",
        use_fast_model=agent_config.get("planner", {}).get("use_fast_model", True),
        fast_model=fast_model,
    )

    coder = CoderAgent(
        **common_kwargs,
        role=AgentRole.CODER,
        name="Coder",
        use_fast_model=False,
    )

    reviewer = ReviewerAgent(
        **common_kwargs,
        role=AgentRole.REVIEWER,
        name="Reviewer",
        use_fast_model=agent_config.get("reviewer", {}).get("use_fast_model", True),
        fast_model=fast_model,
    )

    researcher = ResearcherAgent(
        **common_kwargs,
        role=AgentRole.RESEARCHER,
        name="Researcher",
        retrieval=retrieval,
        use_fast_model=agent_config.get("researcher", {}).get("use_fast_model", True),
        fast_model=fast_model,
    )

    tester = TesterAgent(
        **common_kwargs,
        role=AgentRole.TESTER,
        name="Tester",
    )

    document_agent = DocumentAgent(
        **common_kwargs,
        role=AgentRole.DOCUMENT,
        name="DocumentAgent",
        use_fast_model=True,
        fast_model=fast_model,
    )

    vision_agent = VisionAgent(
        **common_kwargs,
        role=AgentRole.VISION,
        name="VisionAgent",
    )

    # 오케스트레이션 엔진 생성
    engine = OrchestrationEngine(
        manager=manager,
        planner=planner,
        coder=coder,
        reviewer=reviewer,
        researcher=researcher,
        tester=tester,
        document_agent=document_agent,
        vision_agent=vision_agent,
        workspace_manager=workspace,
        task_history=history,
        max_iterations=config.get("agents", {}).get("manager", {}).get("max_iterations", 5),
        on_status_update=on_status_update,
    )

    if attached_mode or project_id:
        engine.bind_project_root(project_id=project_id)

    logger.info("오케스트레이션 엔진 초기화 완료")
    return engine


def _create_backend(config: dict, backend_type: str):
    """설정으로부터 백엔드 생성"""
    if backend_type == "ollama":
        ollama_cfg = config.get("ollama", {})
        backend_config = BackendConfig(
            model=ollama_cfg.get("default_model", "llama3.1:8b"),
            temperature=ollama_cfg.get("temperature", 0.7),
            top_p=ollama_cfg.get("top_p", 0.9),
            max_tokens=ollama_cfg.get("context_length", 4096) // 2,
            context_length=ollama_cfg.get("context_length", 4096),
            timeout=config.get("backend", {}).get("timeout", 120),
            retry_attempts=config.get("backend", {}).get("retry_attempts", 3),
            extra={
                "host": ollama_cfg.get("host", "http://localhost:11434"),
                "vision_model": ollama_cfg.get("vision_model"),
                "fast_model": ollama_cfg.get("fast_model"),
            },
        )
        return OllamaBackend(backend_config)
    elif backend_type == "transformers":
        from .backends.transformers_backend import TransformersBackend
        hf_cfg = config.get("transformers", {})
        backend_config = BackendConfig(
            model=hf_cfg.get("model_id", "mistralai/Mistral-7B-Instruct-v0.2"),
            temperature=hf_cfg.get("temperature", 0.7),
            max_tokens=hf_cfg.get("max_new_tokens", 2048),
            timeout=config.get("backend", {}).get("timeout", 120),
            retry_attempts=config.get("backend", {}).get("retry_attempts", 3),
            extra={
                "device": hf_cfg.get("device", "auto"),
                "load_in_4bit": hf_cfg.get("load_in_4bit", True),
                "load_in_8bit": hf_cfg.get("load_in_8bit", False),
                "fast_model": config.get("ollama", {}).get("fast_model"),
            },
        )
        return TransformersBackend(backend_config)
    else:
        raise ValueError(f"지원하지 않는 백엔드: {backend_type}")


def _set_nested(d: dict, keys: list[str], value) -> None:
    """중첩 딕셔너리에 값 설정"""
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def _coerce_env_value(value):
    """환경 변수 문자열을 bool/int/float 로 최대한 자연스럽게 변환한다."""
    if not isinstance(value, str):
        return value

    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"

    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value
