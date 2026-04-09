"""
Streamlit UI for Local Team Agent.

The UI supports two workspace modes:
1. Attached mode: work directly inside an existing project folder.
2. Managed mode: keep generated projects under the configured workspace root.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

import streamlit as st


PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.setup import create_engine
from src.tools.document import DocumentTool
from src.tools.filesystem import FilesystemTool
from src.workspace.manager import WorkspaceManager


st.set_page_config(
    page_title="Local Team Agent",
    page_icon="🤝",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
    .agent-card {
        background: #f5f7fb;
        border-radius: 10px;
        padding: 10px 12px;
        margin: 6px 0;
        border: 1px solid #dbe4f0;
    }
    .agent-active {
        background: #ecf7ff;
        border-color: #6aa9d8;
        box-shadow: inset 4px 0 0 #1f77b4;
    }
    .status-line {
        font-size: 0.9rem;
        color: #4f6273;
    }
</style>
""",
    unsafe_allow_html=True,
)


def _resolve_optional_dir(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return str(Path(value).expanduser().resolve())


def _default_attached_dir() -> str:
    return _resolve_optional_dir(os.environ.get("LTA_PROJECT_DIR")) or ""


def init_session_state() -> None:
    defaults = {
        "messages": [],
        "agent_logs": [],
        "current_project": None,
        "project_dir": _default_attached_dir(),
        "workspace_root": os.environ.get("WORKSPACE_ROOT", "./workspaces"),
        "engine": None,
        "engine_signature": None,
        "is_running": False,
        "active_agent": None,
        "final_output": "",
        "config_path": None,
        "task_input": "",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _current_mode() -> str:
    return "attached" if st.session_state.get("project_dir") else "managed"


def _project_root() -> Optional[Path]:
    project_dir = _resolve_optional_dir(st.session_state.get("project_dir"))
    if project_dir:
        return Path(project_dir)

    project_id = st.session_state.get("current_project")
    if not project_id:
        return None

    workspace_root = _resolve_optional_dir(st.session_state.get("workspace_root")) or str(
        Path("./workspaces").resolve()
    )
    return Path(workspace_root) / project_id


def _workspace_manager() -> WorkspaceManager:
    project_dir = _resolve_optional_dir(st.session_state.get("project_dir"))
    if project_dir:
        return WorkspaceManager(project_dir, attached=True)

    workspace_root = _resolve_optional_dir(st.session_state.get("workspace_root")) or str(
        Path("./workspaces").resolve()
    )
    return WorkspaceManager(workspace_root)


def _reset_engine() -> None:
    st.session_state.engine = None
    st.session_state.engine_signature = None


def get_or_create_engine(force_recreate: bool = False):
    workspace_root = _resolve_optional_dir(st.session_state.get("workspace_root")) or str(
        Path("./workspaces").resolve()
    )
    project_dir = _resolve_optional_dir(st.session_state.get("project_dir"))
    signature = (workspace_root, project_dir, st.session_state.get("config_path"))

    if force_recreate or st.session_state.engine is None or st.session_state.engine_signature != signature:
        def status_update(agent: str, message: str) -> None:
            st.session_state.agent_logs.append(
                {"agent": agent, "message": message, "time": time.strftime("%H:%M:%S")}
            )
            st.session_state.active_agent = agent

        try:
            st.session_state.engine = create_engine(
                config_path=st.session_state.get("config_path"),
                workspace_root=workspace_root,
                project_root=project_dir,
                on_status_update=status_update,
            )
            st.session_state.engine_signature = signature
        except Exception as exc:
            st.error(f"엔진 초기화 실패: {exc}")
            st.session_state.engine = None
            st.session_state.engine_signature = None

    return st.session_state.engine


def _apply_path_inputs(workspace_value: str, project_dir_value: str) -> None:
    normalized_workspace = workspace_value.strip() or "./workspaces"
    normalized_project_dir = _resolve_optional_dir(project_dir_value.strip()) or ""

    workspace_changed = normalized_workspace != st.session_state.workspace_root
    project_changed = normalized_project_dir != st.session_state.project_dir

    if workspace_changed:
        st.session_state.workspace_root = normalized_workspace
    if project_changed:
        st.session_state.project_dir = normalized_project_dir
        st.session_state.current_project = None

    if workspace_changed or project_changed:
        _reset_engine()


def render_sidebar() -> None:
    with st.sidebar:
        st.title("Local Team Agent")
        st.caption(
            "현재 모드: Attached" if _current_mode() == "attached" else "현재 모드: Managed workspace"
        )

        workspace_value = st.text_input(
            "Workspace root",
            value=st.session_state.workspace_root,
            help="Managed 모드에서 새 프로젝트가 저장되는 기본 경로입니다.",
        )
        project_dir_value = st.text_input(
            "Attached project dir",
            value=st.session_state.project_dir,
            help="값이 있으면 해당 폴더를 바로 작업 루트로 사용합니다.",
        )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Use current dir", use_container_width=True):
                project_dir_value = str(Path.cwd().resolve())
        with col2:
            if st.button("Managed mode", use_container_width=True):
                project_dir_value = ""

        _apply_path_inputs(workspace_value, project_dir_value)

        st.divider()
        st.subheader("Engine")

        if st.button("Initialize / Reload", type="primary", use_container_width=True):
            get_or_create_engine(force_recreate=True)

        engine = st.session_state.engine
        if engine:
            backend = engine.manager.backend
            if backend.is_available():
                st.success("LLM backend connected")
                models = backend.list_models()
                if models:
                    st.caption(f"Detected models: {len(models)}")
                    with st.expander("Model list"):
                        st.write("\n".join(models[:20]))
            else:
                st.error("LLM backend unavailable")
                st.caption("`ollama serve`가 실행 중인지 확인해 주세요.")
        else:
            st.caption("아직 엔진이 초기화되지 않았습니다.")

        st.divider()
        st.subheader("Project")

        try:
            workspace = _workspace_manager()
            if workspace.is_attached_mode():
                st.write(f"Project root: `{workspace.workspace_root}`")
                projects = workspace.list_projects()
                if projects:
                    st.session_state.current_project = projects[0].project_id
                    with st.expander("Project summary"):
                        st.markdown(workspace.get_project_summary(projects[0].project_id))
                else:
                    st.caption("첫 실행 시 `.lta/` 메타데이터가 생성됩니다.")
            else:
                projects = workspace.list_projects()
                options = {"새 프로젝트 자동 생성": None}
                for project in projects:
                    label = f"{project.name} ({project.project_id[:8]})"
                    options[label] = project.project_id

                selected = st.selectbox("Managed project", list(options.keys()))
                st.session_state.current_project = options[selected]

                if st.session_state.current_project:
                    with st.expander("Project summary"):
                        st.markdown(workspace.get_project_summary(st.session_state.current_project))
        except Exception as exc:
            st.error(f"프로젝트 정보를 불러오지 못했습니다: {exc}")

        st.divider()
        if st.button("Clear activity log", use_container_width=True):
            st.session_state.agent_logs = []
            st.rerun()


def render_agent_status() -> None:
    agents = [
        ("manager", "Manager"),
        ("planner", "Planner"),
        ("researcher", "Researcher"),
        ("coder", "Coder"),
        ("reviewer", "Reviewer"),
        ("tester", "Tester"),
        ("document", "Document"),
        ("vision", "Vision"),
    ]

    active = st.session_state.get("active_agent")
    columns = st.columns(4)
    for index, (agent_id, label) in enumerate(agents):
        is_active = st.session_state.is_running and agent_id == active
        class_name = "agent-card agent-active" if is_active else "agent-card"
        status = "running" if is_active else "idle"
        with columns[index % 4]:
            st.markdown(
                f"<div class='{class_name}'><strong>{label}</strong><div class='status-line'>{status}</div></div>",
                unsafe_allow_html=True,
            )


def render_activity_log() -> None:
    logs = st.session_state.agent_logs[-20:]
    if not logs:
        st.caption("활동 로그가 아직 없습니다.")
        return

    for log in reversed(logs):
        st.markdown(
            f"**{log['time']}** `[{log['agent']}]` {log['message']}",
        )


def render_workspace_files() -> None:
    root = _project_root()
    if not root:
        st.caption("프로젝트가 아직 선택되지 않았습니다.")
        return

    if not root.exists():
        st.caption("프로젝트 폴더를 찾을 수 없습니다.")
        return

    fs = FilesystemTool(str(root))
    doc_tool = DocumentTool(str(root))
    skip_dirs = {".git", "__pycache__", "node_modules", ".pytest_cache", ".lta", "venv"}

    try:
        files = []
        for item in fs.list_directory(".", recursive=True):
            if item.is_directory:
                continue
            if any(part in skip_dirs for part in Path(item.path).parts):
                continue
            files.append(item)
    except Exception as exc:
        st.error(f"파일 목록을 불러오지 못했습니다: {exc}")
        return

    if not files:
        st.caption("표시할 파일이 없습니다.")
        return

    file_options = [item.path for item in files]
    selected_file = st.selectbox("Browse file", options=file_options)
    if not selected_file:
        return

    suffix = Path(selected_file).suffix.lower()

    try:
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            st.image(str(root / selected_file), caption=selected_file, use_container_width=True)
            return

        if suffix in {".pdf", ".docx"}:
            doc = doc_tool.read_document(selected_file)
            st.caption(
                f"type={doc.file_type} | title={doc.title or '-'} | words={doc.word_count} | sections={len(doc.sections)}"
            )
            st.text_area("Preview", doc.content[:5000] or "(empty)", height=420)
            return

        content = fs.read_file(selected_file)
        if suffix in {".py", ".js", ".ts", ".json", ".yaml", ".yml", ".sh", ".css", ".html", ".md"}:
            language = suffix.lstrip(".")
            if language == "yml":
                language = "yaml"
            st.code(content, language=language)
        else:
            st.text_area("Preview", content, height=420)
    except Exception as exc:
        st.error(f"파일을 열지 못했습니다: {exc}")


def _append_chat_message(role: str, content: str, **extra) -> None:
    message = {"role": role, "content": content}
    message.update(extra)
    st.session_state.messages.append(message)


def main() -> None:
    init_session_state()
    render_sidebar()

    st.title("Local Team Agent")
    if _current_mode() == "attached":
        st.caption(f"현재 폴더를 직접 작업 대상으로 사용합니다: `{st.session_state.project_dir}`")
    else:
        st.caption(f"Managed workspace: `{st.session_state.workspace_root}`")

    tab_chat, tab_agents, tab_files, tab_help = st.tabs(
        ["Task", "Agents", "Files", "Help"]
    )

    with tab_chat:
        st.subheader("작업 요청")

        examples = [
            "현재 프로젝트 구조를 분석하고 개선 포인트를 정리해줘",
            "PDF 문서를 읽고 요약 문서를 만들어줘",
            "FastAPI 엔드포인트에 대한 테스트 코드를 추가해줘",
            "최근 공식 문서를 바탕으로 필요한 변경 사항을 조사해줘",
        ]
        with st.expander("예시 작업"):
            for example in examples:
                if st.button(example, key=f"example::{example}"):
                    st.session_state.task_input = example

        with st.form("task_form", clear_on_submit=False):
            task_input = st.text_area(
                "작업 설명",
                value=st.session_state.get("task_input", ""),
                height=120,
                placeholder="예: 이 저장소의 README를 개선하고 테스트를 추가해줘",
            )
            col1, col2 = st.columns([3, 1])
            with col1:
                quick_mode = st.checkbox("Quick mode", value=False)
                quick_agent = "coder"
                if quick_mode:
                    quick_agent = st.selectbox(
                        "Quick agent",
                        ["coder", "planner", "researcher", "reviewer", "document"],
                    )
            with col2:
                submitted = st.form_submit_button("Run", type="primary", use_container_width=True)

        if submitted and task_input.strip():
            st.session_state.task_input = task_input
            st.session_state.is_running = True
            st.session_state.agent_logs = []
            st.session_state.final_output = ""

            engine = get_or_create_engine()
            if not engine:
                st.session_state.is_running = False
                st.stop()

            try:
                with st.spinner("에이전트가 작업 중입니다..."):
                    if _project_root():
                        engine.bind_project_root(st.session_state.current_project)

                    if quick_mode:
                        result = engine.run_quick(task_input, quick_agent)
                        st.session_state.final_output = result
                        _append_chat_message("user", task_input)
                        _append_chat_message("assistant", result, agent=quick_agent)
                    else:
                        state = engine.run(
                            user_task=task_input,
                            project_id=None if _current_mode() == "attached" else st.session_state.current_project,
                        )
                        st.session_state.final_output = state.final_output
                        st.session_state.current_project = state.project_id
                        _append_chat_message("user", task_input)
                        _append_chat_message(
                            "assistant",
                            state.final_output,
                            artifacts=[
                                {
                                    "name": artifact.name,
                                    "type": artifact.artifact_type,
                                    "content": artifact.content,
                                    "file_path": artifact.file_path,
                                }
                                for result in state.results
                                for artifact in result.artifacts
                            ],
                        )
            finally:
                st.session_state.is_running = False

            st.rerun()

        if st.session_state.messages:
            st.subheader("대화 기록")
            recent_messages = list(reversed(st.session_state.messages[-12:]))
            for message_index, message in enumerate(recent_messages):
                with st.chat_message(message["role"]):
                    st.markdown(message["content"])
                    if message.get("artifacts"):
                        with st.expander(f"Artifacts ({len(message['artifacts'])})"):
                            for artifact_index, artifact in enumerate(message["artifacts"]):
                                label = artifact["name"] or "(unnamed)"
                                if artifact.get("file_path"):
                                    label = f"{label} -> {artifact['file_path']}"
                                st.write(f"**{label}** [{artifact['type']}]")
                                preview = artifact["content"][:1500]
                                if artifact["type"] == "code":
                                    language = Path(artifact["name"]).suffix.lstrip(".") or "text"
                                    st.code(preview, language=language)
                                else:
                                    st.text_area(
                                        label,
                                        preview,
                                        height=160,
                                        key=f"artifact_preview_{message_index}_{artifact_index}",
                                    )

    with tab_agents:
        st.subheader("에이전트 상태")
        render_agent_status()
        st.divider()
        st.subheader("활동 로그")
        render_activity_log()

    with tab_files:
        st.subheader("프로젝트 파일")
        render_workspace_files()

    with tab_help:
        st.subheader("사용 방법")
        st.markdown(
            """
`Attached mode`
현재 폴더나 지정한 폴더를 그대로 작업 루트로 사용합니다.

`Managed mode`
`workspace_root/project_id` 아래에 에이전트 전용 작업 공간을 만듭니다.

`문서 기능`
- PDF / DOCX 읽기
- Markdown / text 기반 PPTX 생성

`검색 기능`
- CLI `search-web`
- 연구 에이전트가 최신 정보가 필요한 작업에서 자동 사용

`타임아웃 개선`
- Ollama 요청 타임아웃 시 재시도하면서 더 작은 `max_tokens`와 fast model로 자동 완화합니다.
"""
        )


if __name__ == "__main__":
    main()
