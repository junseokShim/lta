"""
CLI entrypoint for Local Team Agent.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


sys.path.insert(0, str(Path(__file__).parent.parent))

from src.logging_utils import get_logger, setup_logging
from src.setup import _create_backend, create_engine, load_config


app = typer.Typer(
    name="local-team-agent",
    help="Local Team Agent CLI",
    add_completion=False,
)

console = Console()
logger = get_logger("cli")


def print_banner() -> None:
    console.print(
        Panel.fit(
            "[bold blue]Local Team Agent[/bold blue]\n[dim]multi-agent local coding assistant[/dim]",
            border_style="blue",
        )
    )


def status_callback(agent: str, message: str) -> None:
    agent_colors = {
        "manager": "blue",
        "planner": "cyan",
        "coder": "green",
        "reviewer": "yellow",
        "researcher": "magenta",
        "tester": "red",
        "document": "white",
        "vision": "bright_black",
        "system": "dim",
        "info": "green",
        "error": "red",
    }
    color = agent_colors.get(agent.lower(), "white")
    console.print(f"  [{color}][{agent}][/{color}] {message}")


def _sanitize_console_text(text: str) -> str:
    sanitized = text.replace("\u2022", "-").replace("\u2013", "-").replace("\u2014", "-")
    encoding = sys.stdout.encoding or "utf-8"
    return sanitized.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _print_text(text: str) -> None:
    console.print(_sanitize_console_text(text))


def _print_panel(text: str, title: str, border_style: str = "green") -> None:
    console.print(
        Panel(
            _sanitize_console_text(text),
            title=title,
            border_style=border_style,
        )
    )


def _resolve_project_dir(project_dir: Optional[str]) -> Optional[str]:
    if not project_dir:
        return None
    return str(Path(project_dir).expanduser().resolve())


def _resolve_runtime_project_dir(
    project_dir: Optional[str],
    managed: bool = False,
    project_id: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Optional[str]:
    """
    Default behavior is Claude Code-like:
    if no explicit managed/workspace/project target is provided, attach the current directory.
    """
    resolved = _resolve_project_dir(project_dir)
    if resolved:
        return resolved
    if managed or project_id or workspace:
        return None
    return str(Path.cwd().resolve())


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        env_file = Path(__file__).parent.parent / ".env"
        if env_file.exists():
            load_dotenv(env_file)
    except ImportError:
        pass


def _build_engine(
    config: Optional[str],
    workspace: Optional[str],
    project_dir: Optional[str],
    project_id: Optional[str],
    status_updates: bool = True,
):
    return create_engine(
        config_path=config,
        workspace_root=workspace,
        project_root=project_dir,
        project_id=project_id,
        on_status_update=status_callback if status_updates else None,
    )


def _normalize_output_format(output_format: str) -> str:
    normalized = (output_format or "text").strip().lower()
    if normalized not in {"text", "json"}:
        raise typer.BadParameter("output format must be one of: text, json")
    return normalized


def _emit_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _serialize_artifact(artifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "name": artifact.name,
        "artifact_type": artifact.artifact_type,
        "file_path": artifact.file_path,
        "language": artifact.language,
        "metadata": artifact.metadata,
        "content_preview": artifact.content[:400] if artifact.content else "",
    }


def _serialize_result(result) -> dict[str, Any]:
    return {
        "result_id": result.result_id,
        "task_id": result.task_id,
        "agent_name": result.agent_name,
        "agent_role": result.agent_role.value,
        "success": result.success,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "content_preview": result.content[:800],
        "metadata": result.metadata,
        "artifacts": [_serialize_artifact(artifact) for artifact in result.artifacts],
    }


def _serialize_state(state) -> dict[str, Any]:
    all_artifacts = [artifact for result in state.results for artifact in result.artifacts]
    return {
        "session_id": state.session_id,
        "project_id": state.project_id,
        "status": state.status.value,
        "original_task": state.original_task,
        "final_output": state.final_output,
        "started_at": state.started_at,
        "completed_at": state.completed_at,
        "iteration": state.iteration,
        "metadata": state.metadata,
        "results": [_serialize_result(result) for result in state.results],
        "artifacts": [_serialize_artifact(artifact) for artifact in all_artifacts],
    }


def _serialize_project_metadata(project) -> Optional[dict[str, Any]]:
    if not project:
        return None
    return {
        "project_id": project.project_id,
        "name": project.name,
        "description": project.description,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "task_count": project.task_count,
        "generated_files": project.generated_files,
        "tags": project.tags,
        "config": project.config,
    }


def _make_workspace_manager(cfg: dict, workspace: Optional[str], project_dir: Optional[str]):
    from src.workspace.manager import WorkspaceManager

    workspace_root = workspace or cfg.get("workspace", {}).get("root", "./workspaces")
    return WorkspaceManager(project_dir or workspace_root, attached=bool(project_dir))


def _resolve_existing_project_id(manager, project_id: Optional[str]) -> Optional[str]:
    if project_id:
        return project_id
    projects = manager.list_projects()
    if projects:
        return projects[0].project_id
    return None


def _safe_load_project(manager, project_id: Optional[str]):
    if not project_id:
        return None
    try:
        return manager.load_project(project_id)
    except FileNotFoundError:
        return None


def _count_files(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(1 for path in directory.rglob("*") if path.is_file())


def _check_module(module_name: str) -> dict[str, str]:
    try:
        importlib.import_module(module_name)
        return {"status": "ok", "detail": "installed"}
    except Exception as exc:  # pragma: no cover - depends on local env
        return {"status": "warn", "detail": str(exc)}


def _workspace_write_check(path: Path) -> dict[str, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".lta_doctor_", delete=True):
            pass
        return {"status": "ok", "detail": f"writable: {path}"}
    except Exception as exc:  # pragma: no cover - depends on local env
        return {"status": "error", "detail": str(exc)}


def _get_git_status_for_path(root: str, max_lines: int = 20) -> list[str]:
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=5,
            encoding="utf-8",
            errors="replace",
        )
        if probe.returncode != 0 or "true" not in (probe.stdout or "").lower():
            return []

        status = subprocess.run(
            ["git", "status", "--short", "--branch"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=5,
            encoding="utf-8",
            errors="replace",
        )
        if status.returncode != 0:
            return []
        return [line for line in status.stdout.splitlines() if line.strip()][:max_lines]
    except Exception:
        return []


def _collect_status_snapshot(
    cfg: dict,
    workspace: Optional[str],
    project_dir: Optional[str],
    project_id: Optional[str],
) -> dict[str, Any]:
    manager = _make_workspace_manager(cfg, workspace, project_dir)
    resolved_project_id = _resolve_existing_project_id(manager, project_id)
    project = _safe_load_project(manager, resolved_project_id)

    backend_type = cfg.get("backend", {}).get("default", "ollama")
    backend = _create_backend(cfg, backend_type)
    backend_available = backend.is_available()
    installed_models = backend.list_models() if backend_available else []

    ollama_cfg = cfg.get("ollama", {})
    configured_models = [
        ollama_cfg.get("default_model"),
        ollama_cfg.get("fast_model"),
        ollama_cfg.get("vision_model"),
    ]
    missing_models = sorted({model for model in configured_models if model and model not in installed_models})

    shell_root = project_dir or str(manager.workspace_root)
    git_status_lines = _get_git_status_for_path(shell_root, max_lines=20)

    task_history = manager.get_task_history(resolved_project_id, limit=10) if resolved_project_id else []
    chat_history = manager.get_chat_history(resolved_project_id, limit=200) if resolved_project_id else []
    guidance = manager.get_project_guidance(resolved_project_id) if resolved_project_id else ""

    storage_root = manager.get_storage_path()
    cache_dir = storage_root / "cache"
    reports_dir = storage_root / "reports"

    return {
        "cwd": str(Path.cwd().resolve()),
        "mode": "attached" if manager.is_attached_mode() else "managed",
        "workspace_root": str(manager.workspace_root),
        "storage_root": str(storage_root),
        "project_root": str(manager.get_project_path(resolved_project_id)) if resolved_project_id else None,
        "project": _serialize_project_metadata(project),
        "backend": {
            "type": backend_type,
            "available": backend_available,
            "default_model": ollama_cfg.get("default_model"),
            "fast_model": ollama_cfg.get("fast_model"),
            "vision_model": ollama_cfg.get("vision_model"),
            "timeout": cfg.get("backend", {}).get("timeout", 120),
            "retry_attempts": cfg.get("backend", {}).get("retry_attempts", 3),
            "installed_models_count": len(installed_models),
            "installed_models_sample": installed_models[:15],
            "missing_models": missing_models,
        },
        "git": {
            "available": bool(git_status_lines),
            "status_lines": git_status_lines,
        },
        "session": {
            "active_project_id": resolved_project_id,
            "chat_message_count": len(chat_history),
            "recent_task_count": len(task_history),
            "recent_tasks": task_history[:5],
        },
        "guidance": {
            "present": bool(guidance),
            "preview": guidance[:800],
        },
        "memory": {
            "chunk_size": cfg.get("memory", {}).get("chunk_size", 512),
            "chunk_overlap": cfg.get("memory", {}).get("chunk_overlap", 64),
            "retrieval_top_k": cfg.get("memory", {}).get("retrieval_top_k", 5),
            "max_indexed_files": cfg.get("memory", {}).get("max_indexed_files", 2000),
            "max_total_chunks": cfg.get("memory", {}).get("max_total_chunks", 25000),
            "max_chunks_per_file": cfg.get("memory", {}).get("max_chunks_per_file", 400),
            "max_index_file_size_mb": cfg.get("memory", {}).get("max_index_file_size_mb", 2.5),
            "cache_file_count": _count_files(cache_dir),
            "report_file_count": _count_files(reports_dir),
        },
    }


def _collect_doctor_report(
    cfg: dict,
    workspace: Optional[str],
    project_dir: Optional[str],
    project_id: Optional[str],
    config_path: Optional[str],
) -> dict[str, Any]:
    snapshot = _collect_status_snapshot(cfg, workspace, project_dir, project_id)
    storage_root = Path(snapshot["storage_root"])
    backend = snapshot["backend"]

    checks = [
        {
            "name": "python",
            "status": "ok",
            "detail": f"{sys.executable} ({sys.version.split()[0]})",
        },
        {
            "name": "virtualenv",
            "status": "ok" if os.environ.get("VIRTUAL_ENV") else "warn",
            "detail": os.environ.get("VIRTUAL_ENV") or "No active virtualenv detected.",
        },
        {
            "name": "config",
            "status": "ok",
            "detail": str(Path(config_path).resolve()) if config_path else "default config resolution",
        },
        {
            "name": "workspace",
            **_workspace_write_check(storage_root),
        },
        {
            "name": "backend",
            "status": "ok" if backend["available"] else "error",
            "detail": f"{backend['type']} ({backend['default_model']})",
        },
        {
            "name": "default_model",
            "status": "ok" if backend["default_model"] not in backend["missing_models"] else "warn",
            "detail": backend["default_model"] or "not configured",
        },
        {
            "name": "fast_model",
            "status": "ok" if backend["fast_model"] not in backend["missing_models"] else "warn",
            "detail": backend["fast_model"] or "not configured",
        },
        {
            "name": "vision_model",
            "status": "ok" if backend["vision_model"] not in backend["missing_models"] else "warn",
            "detail": backend["vision_model"] or "not configured",
        },
        {
            "name": "git",
            "status": "ok" if snapshot["git"]["available"] else "warn",
            "detail": "git repository detected" if snapshot["git"]["available"] else "current directory is not a git repository",
        },
        {
            "name": "pdf_support",
            **_check_module("PyPDF2"),
        },
        {
            "name": "ppt_support",
            **_check_module("pptx"),
        },
        {
            "name": "html_parser",
            **_check_module("bs4"),
        },
        {
            "name": "web_search_provider",
            "status": "ok",
            "detail": os.environ.get("WEB_SEARCH_PROVIDER", "duckduckgo"),
        },
    ]

    overall = "ok"
    if any(item["status"] == "error" for item in checks):
        overall = "error"
    elif any(item["status"] == "warn" for item in checks):
        overall = "warn"

    return {
        "overall_status": overall,
        "snapshot": snapshot,
        "checks": checks,
    }


def _format_status_text(snapshot: dict[str, Any]) -> str:
    project = snapshot.get("project") or {}
    backend = snapshot["backend"]
    git_lines = snapshot["git"]["status_lines"]

    lines = [
        f"Mode: {snapshot['mode']}",
        f"CWD: {snapshot['cwd']}",
        f"Workspace root: {snapshot['workspace_root']}",
        f"Storage root: {snapshot['storage_root']}",
        f"Project root: {snapshot.get('project_root') or '-'}",
        f"Project ID: {snapshot['session'].get('active_project_id') or '-'}",
        f"Project name: {project.get('name') or '-'}",
        f"Task count: {project.get('task_count', 0)}",
        f"Chat messages: {snapshot['session']['chat_message_count']}",
        f"Recent tasks: {snapshot['session']['recent_task_count']}",
        "",
        f"Backend: {backend['type']} ({'available' if backend['available'] else 'unavailable'})",
        f"Default model: {backend['default_model'] or '-'}",
        f"Fast model: {backend['fast_model'] or '-'}",
        f"Vision model: {backend['vision_model'] or '-'}",
        f"Missing models: {', '.join(backend['missing_models']) or 'none'}",
        "",
        "Git:",
    ]

    if git_lines:
        lines.extend(f"  {line}" for line in git_lines[:10])
    else:
        lines.append("  not a git repository or git unavailable")

    lines.extend(
        [
            "",
            "Memory / cache:",
            f"  max indexed files: {snapshot['memory']['max_indexed_files']}",
            f"  max total chunks: {snapshot['memory']['max_total_chunks']}",
            f"  cache files: {snapshot['memory']['cache_file_count']}",
            f"  reports: {snapshot['memory']['report_file_count']}",
        ]
    )
    return "\n".join(lines)


def _format_doctor_text(report: dict[str, Any]) -> str:
    lines = [f"Overall: {report['overall_status']}", ""]
    for item in report["checks"]:
        lines.append(f"[{item['status']}] {item['name']}: {item['detail']}")
    return "\n".join(lines)


def _format_config_text(cfg: dict[str, Any]) -> str:
    backend = cfg.get("backend", {})
    ollama_cfg = cfg.get("ollama", {})
    memory = cfg.get("memory", {})
    return "\n".join(
        [
            f"Backend default: {backend.get('default', 'ollama')}",
            f"Timeout: {backend.get('timeout', 600)}",
            f"Retry attempts: {backend.get('retry_attempts', 5)}",
            f"Default model: {ollama_cfg.get('default_model', '-')}",
            f"Fast model: {ollama_cfg.get('fast_model', '-')}",
            f"Vision model: {ollama_cfg.get('vision_model', '-')}",
            f"Context length: {ollama_cfg.get('context_length', 4096)}",
            f"Workspace root: {cfg.get('workspace', {}).get('root', './workspaces')}",
            f"Retrieval top_k: {memory.get('retrieval_top_k', 5)}",
            f"Max indexed files: {memory.get('max_indexed_files', 2000)}",
            f"Max total chunks: {memory.get('max_total_chunks', 25000)}",
        ]
    )


def _get_agent_catalog() -> list[dict[str, Any]]:
    return [
        {"name": "manager", "role": "orchestrates workflow", "tools": ["filesystem", "document", "shell", "web_search"]},
        {"name": "planner", "role": "creates step-by-step execution plan", "tools": ["filesystem", "document", "web_search"]},
        {"name": "researcher", "role": "collects repository and web context", "tools": ["filesystem", "document", "web_search", "retrieval"]},
        {"name": "coder", "role": "implements code changes", "tools": ["filesystem", "document", "shell"]},
        {"name": "reviewer", "role": "reviews risks and regressions", "tools": ["filesystem", "document"]},
        {"name": "tester", "role": "runs verification and creates tests", "tools": ["filesystem", "shell"]},
        {"name": "document", "role": "writes reports and docs", "tools": ["document", "filesystem"]},
        {"name": "vision", "role": "reads images when a vision model is available", "tools": ["image", "document"]},
    ]


def _format_agents_text() -> str:
    lines = []
    for agent in _get_agent_catalog():
        tools = ", ".join(agent["tools"])
        lines.append(f"{agent['name']}: {agent['role']} [{tools}]")
    return "\n".join(lines)


def _build_chat_export(
    history: list[dict],
    project_id: Optional[str],
    project_root: Optional[str],
) -> str:
    lines = [
        "# Local Team Agent Chat Export",
        "",
        f"- Exported at: {datetime.now().isoformat()}",
        f"- Project ID: {project_id or '-'}",
        f"- Project root: {project_root or '-'}",
        "",
    ]
    for item in history:
        lines.extend(
            [
                f"## {item['role']}",
                "",
                item["content"],
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _export_chat_history(
    engine,
    project_id: Optional[str],
    history: list[dict],
    target_path: Optional[str] = None,
) -> str:
    if not history:
        raise ValueError("No chat history to export.")

    project_root = None
    if engine.workspace and project_id:
        try:
            project_root = str(engine.workspace.get_project_path(project_id))
        except Exception:
            project_root = None

    export_content = _build_chat_export(history, project_id, project_root)
    if target_path:
        candidate = Path(target_path).expanduser()
        if candidate.is_absolute():
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(export_content, encoding="utf-8")
            return str(candidate)
        if engine.workspace and project_id:
            return engine.workspace.save_project_file(project_id, str(candidate), export_content)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(export_content, encoding="utf-8")
        return str(candidate.resolve())

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_name = f"chat_export_{timestamp}.md"
    if engine.workspace and project_id:
        return engine.workspace.save_report(project_id, export_content, default_name)

    fallback = Path.cwd() / default_name
    fallback.write_text(export_content, encoding="utf-8")
    return str(fallback.resolve())


def _format_chat_history(history: list[dict], limit: int = 12) -> str:
    recent = history[-limit:]
    lines = []
    for item in recent:
        lines.append(f"{item['role']}: {item['content']}")
    return "\n".join(lines)


def _append_chat_history(
    engine,
    project_id: Optional[str],
    history: list[dict],
    role: str,
    content: str,
) -> None:
    history.append({"role": role, "content": content})
    if engine.workspace and project_id:
        engine.workspace.append_chat_message(project_id, role, content)


def _collect_multiline_chat_input() -> Optional[str]:
    console.print("[dim]Multiline mode. Paste your prompt, then enter /end on its own line. Use /cancel to abort.[/dim]")
    lines: list[str] = []

    while True:
        try:
            line = console.input("[bold cyan]...[/bold cyan] ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Multiline input cancelled.[/dim]")
            return None

        marker = line.strip()
        if marker == "/cancel":
            console.print("[dim]Multiline input cancelled.[/dim]")
            return None
        if marker == "/end":
            break

        lines.append(line)

    content = "\n".join(lines).strip()
    if not content:
        console.print("[dim]Empty multiline input ignored.[/dim]")
        return None
    return content


def _print_chat_help() -> None:
    _print_text(
        """
/help      show commands
/multi     enter multiline paste mode, finish with /end
/history   show recent conversation
/status    show project and backend status
/doctor    run local health checks
/config    show effective runtime config summary
/export    export the current chat history to markdown
/clear     clear conversation memory
/project   show current project binding
/quick a t run a quick single-agent request, e.g. /quick coder 테스트 추가해줘
/exit      leave chat
"""
    )


@app.command()
def run(
    task: str = typer.Argument(..., help="수행할 작업 설명"),
    project_id: Optional[str] = typer.Option(None, "--project", "-p", help="기존 managed 프로젝트 ID"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="설정 파일 경로"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="managed workspace 루트"),
    project_dir: Optional[str] = typer.Option(None, "--project-dir", help="붙일 프로젝트 폴더"),
    managed: bool = typer.Option(
        False,
        "--managed",
        help="현재 폴더 attach 대신 기존 workspaces 기반 managed 모드 사용",
    ),
    quick: bool = typer.Option(False, "--quick", "-q", help="단일 에이전트 빠른 실행"),
    agent: str = typer.Option("coder", "--agent", "-a", help="quick 모드 에이전트"),
    output_format: str = typer.Option("text", "--output-format", help="text or json"),
) -> None:
    """Run the multi-agent workflow once."""
    print_banner()
    _load_dotenv()
    setup_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    output_format = _normalize_output_format(output_format)

    resolved_project_dir = _resolve_runtime_project_dir(
        project_dir,
        managed=managed,
        project_id=project_id,
        workspace=workspace,
    )
    engine = _build_engine(config, workspace, resolved_project_dir, project_id)

    console.print(f"\n[bold]Task:[/bold] {task}")
    if resolved_project_dir:
        console.print(f"[bold]Attached project:[/bold] {resolved_project_dir}")
    elif project_id:
        console.print(f"[bold]Managed project:[/bold] {project_id}")
    elif workspace:
        console.print(f"[bold]Workspace:[/bold] {workspace}")
    console.print()

    if quick:
        result = engine.run_quick(task, agent)
        if output_format == "json":
            _emit_json(
                {
                    "mode": "quick",
                    "agent": agent,
                    "task": task,
                    "project_id": project_id,
                    "project_dir": resolved_project_dir,
                    "result": result,
                }
            )
        else:
            _print_text(result)
        return

    state = engine.run(
        user_task=task,
        project_id=None if resolved_project_dir else project_id,
    )
    if output_format == "json":
        _emit_json(_serialize_state(state))
        return

    _print_panel(state.final_output, "[bold green]Final Output[/bold green]")

    all_artifacts = [artifact for result in state.results for artifact in result.artifacts]
    if all_artifacts:
        table = Table(title="Artifacts")
        table.add_column("Name", style="cyan")
        table.add_column("Type")
        table.add_column("Target")
        for artifact in all_artifacts:
            table.add_row(
                artifact.name or "(unnamed)",
                artifact.artifact_type,
                artifact.file_path or "-",
            )
        console.print(table)

    if state.project_id:
        console.print(f"\n[dim]Project ID: {state.project_id}[/dim]")


@app.command()
def chat(
    project_id: Optional[str] = typer.Option(None, "--project", "-p", help="기존 managed 프로젝트 ID"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="설정 파일 경로"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="managed workspace 루트"),
    project_dir: Optional[str] = typer.Option(None, "--project-dir", help="붙일 프로젝트 폴더"),
    managed: bool = typer.Option(
        False,
        "--managed",
        help="현재 폴더 attach 대신 managed workspace 모드 사용",
    ),
    fresh: bool = typer.Option(
        False,
        "--fresh",
        help="저장된 이전 대화를 불러오지 않고 새 채팅으로 시작",
    ),
) -> None:
    """Start an interactive chat session like Claude Code in CMD."""
    print_banner()
    _load_dotenv()
    setup_logging(level=os.environ.get("LOG_LEVEL", "INFO"))

    resolved_project_dir = _resolve_runtime_project_dir(
        project_dir,
        managed=managed,
        project_id=project_id,
        workspace=workspace,
    )
    engine = _build_engine(config, workspace, resolved_project_dir, project_id)
    cfg = load_config(config)
    session_project_id = project_id
    if resolved_project_dir and engine.workspace:
        session_project_id = engine.workspace.create_project(
            Path(resolved_project_dir).name,
            "Interactive chat session",
        ).project_id

    history: list[dict] = []
    if not fresh and engine.workspace and session_project_id:
        history = engine.workspace.get_chat_history(session_project_id, limit=24)

    console.print("[dim]Interactive chat started. Type /help for commands.[/dim]")
    if resolved_project_dir:
        console.print(f"[dim]Attached project: {resolved_project_dir}[/dim]")
    elif project_id:
        console.print(f"[dim]Managed project: {project_id}[/dim]")
    if history:
        console.print(f"[dim]Loaded {len(history)} previous messages for this project.[/dim]")
    console.print("[dim]For long prompts, use /multi and finish with /end.[/dim]")

    while True:
        try:
            user_input = typer.prompt("you")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Chat ended.[/dim]")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        if user_input in {"/exit", "exit", "quit"}:
            console.print("[dim]Chat ended.[/dim]")
            break
        if user_input == "/help":
            _print_chat_help()
            continue
        if user_input in {"/multi", "/multiline", "/paste"}:
            user_input = _collect_multiline_chat_input()
            if not user_input:
                continue
        if user_input == "/history":
            if history:
                _print_panel(_format_chat_history(history, limit=16), "[bold cyan]Recent History[/bold cyan]", "cyan")
            else:
                console.print("[dim]No conversation history yet.[/dim]")
            continue
        if user_input == "/status":
            snapshot = _collect_status_snapshot(cfg, workspace, resolved_project_dir, session_project_id)
            _print_panel(_format_status_text(snapshot), "[bold cyan]Status[/bold cyan]", "cyan")
            continue
        if user_input == "/doctor":
            report = _collect_doctor_report(cfg, workspace, resolved_project_dir, session_project_id, config)
            border_style = "green" if report["overall_status"] == "ok" else "yellow"
            if report["overall_status"] == "error":
                border_style = "red"
            _print_panel(_format_doctor_text(report), "[bold cyan]Doctor[/bold cyan]", border_style)
            continue
        if user_input == "/config":
            _print_panel(_format_config_text(cfg), "[bold cyan]Config[/bold cyan]", "cyan")
            continue
        if user_input.startswith("/export"):
            export_target = user_input[len("/export"):].strip() or None
            try:
                saved_path = _export_chat_history(engine, session_project_id, history, export_target)
            except ValueError as exc:
                console.print(f"[yellow]{exc}[/yellow]")
                continue
            console.print(f"[green]Chat exported:[/green] {saved_path}")
            continue
        if user_input == "/clear":
            history.clear()
            if engine.workspace and session_project_id:
                engine.workspace.clear_chat_history(session_project_id)
            console.print("[dim]Conversation history cleared for this project.[/dim]")
            continue
        if user_input == "/project":
            if resolved_project_dir:
                console.print(f"[dim]Attached project: {resolved_project_dir}[/dim]")
            else:
                console.print(f"[dim]Managed project: {session_project_id or '(not created yet)'}[/dim]")
            continue
        if user_input.startswith("/quick "):
            quick_parts = user_input.split(maxsplit=2)
            if len(quick_parts) < 3:
                console.print("[yellow]Usage: /quick <agent> <task>[/yellow]")
                continue
            quick_agent = quick_parts[1]
            quick_task = quick_parts[2]
            quick_result = engine.run_quick(
                quick_task,
                quick_agent,
                additional_context={"conversation_history": _format_chat_history(history)},
            )
            _append_chat_history(engine, session_project_id, history, "user", user_input)
            _append_chat_history(engine, session_project_id, history, "assistant", quick_result)
            _print_panel(quick_result, f"[bold cyan]Quick:{quick_agent}[/bold cyan]", border_style="cyan")
            continue

        additional_context = {}
        if history:
            additional_context["conversation_history"] = _format_chat_history(history)

        state = engine.run(
            user_task=user_input,
            project_id=session_project_id,
            additional_context=additional_context or None,
        )
        if state.project_id:
            session_project_id = state.project_id

        _append_chat_history(engine, session_project_id, history, "user", user_input)
        _append_chat_history(engine, session_project_id, history, "assistant", state.final_output)
        _print_panel(state.final_output, "[bold green]Assistant[/bold green]")


@app.command()
def new_project(
    name: str = typer.Argument(..., help="프로젝트 이름"),
    description: str = typer.Option("", "--desc", "-d", help="프로젝트 설명"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="workspace 경로"),
) -> None:
    """Create a new managed workspace project."""
    _load_dotenv()
    from src.workspace.manager import WorkspaceManager

    ws_root = workspace or os.environ.get("WORKSPACE_ROOT", "./workspaces")
    manager = WorkspaceManager(ws_root)
    meta = manager.create_project(name=name, description=description)
    console.print(
        Panel.fit(
            f"Name: [bold]{meta.name}[/bold]\nID: [cyan]{meta.project_id}[/cyan]\nPath: {ws_root}/{meta.project_id}",
            title="Managed Project Created",
            border_style="green",
        )
    )


@app.command("list-projects")
def list_projects(
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
    project_dir: Optional[str] = typer.Option(None, "--project-dir", help="조회할 attached 프로젝트 폴더"),
    managed: bool = typer.Option(
        False,
        "--managed",
        help="현재 폴더 attach 대신 workspaces 목록 조회",
    ),
) -> None:
    """List projects for the active mode."""
    _load_dotenv()
    from src.workspace.manager import WorkspaceManager

    resolved_project_dir = _resolve_runtime_project_dir(
        project_dir,
        managed=managed,
        workspace=workspace,
    )
    ws_root = workspace or os.environ.get("WORKSPACE_ROOT", "./workspaces")
    manager = WorkspaceManager(resolved_project_dir or ws_root, attached=bool(resolved_project_dir))
    projects = manager.list_projects()

    if not projects:
        console.print("[dim]No projects found.[/dim]")
        return

    title_root = resolved_project_dir or ws_root
    table = Table(title=f"Projects ({title_root})")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Tasks")
    table.add_column("Updated")

    for project in projects:
        table.add_row(
            project.project_id,
            project.name,
            (project.description or "")[:60],
            str(project.task_count),
            project.updated_at[:19],
        )
    console.print(table)


@app.command()
def inspect(
    project_id: Optional[str] = typer.Argument(None, help="managed 프로젝트 ID"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
    project_dir: Optional[str] = typer.Option(None, "--project-dir", help="조회할 attached 프로젝트 폴더"),
    managed: bool = typer.Option(
        False,
        "--managed",
        help="현재 폴더 attach 대신 managed 프로젝트 조회",
    ),
    output_format: str = typer.Option("text", "--output-format", help="text or json"),
) -> None:
    """Show project summary and recent task history."""
    _load_dotenv()
    from src.workspace.manager import WorkspaceManager
    output_format = _normalize_output_format(output_format)

    resolved_project_dir = _resolve_runtime_project_dir(
        project_dir,
        managed=managed,
        project_id=project_id,
        workspace=workspace,
    )
    ws_root = workspace or os.environ.get("WORKSPACE_ROOT", "./workspaces")
    manager = WorkspaceManager(resolved_project_dir or ws_root, attached=bool(resolved_project_dir))

    try:
        if resolved_project_dir and not project_id:
            projects = manager.list_projects()
            if projects:
                project_id = projects[0].project_id
            else:
                project_id = manager.create_project(Path(resolved_project_dir).name, "").project_id

        if not project_id:
            raise typer.BadParameter("project_id 또는 --project-dir 가 필요합니다.")

        if output_format == "json":
            _emit_json(
                {
                    "project": _serialize_project_metadata(manager.load_project(project_id)),
                    "summary": manager.get_project_summary(project_id),
                    "guidance": manager.get_project_guidance(project_id),
                    "recent_tasks": manager.get_task_history(project_id, limit=10),
                }
            )
            return

        _print_text(manager.get_project_summary(project_id))
        guidance = manager.get_project_guidance(project_id)
        if guidance:
            _print_panel(guidance, "[bold cyan]Project Guidance[/bold cyan]", border_style="cyan")

        history = manager.get_task_history(project_id, limit=10)
        if history:
            table = Table(title="Recent Tasks")
            table.add_column("Time")
            table.add_column("Agent")
            table.add_column("Description")
            table.add_column("Status")
            for task in history:
                status = "[green]completed[/green]" if task["status"] == "completed" else f"[red]{task['status']}[/red]"
                table.add_row(
                    task["created_at"][:16],
                    task["agent_name"] or "-",
                    task["description"][:70],
                    status,
                )
            console.print(table)
    except FileNotFoundError:
        console.print(f"[red]Project not found: {project_id}[/red]")


@app.command()
def status(
    project_id: Optional[str] = typer.Option(None, "--project", "-p", help="managed project id"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="config path"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="managed workspace root"),
    project_dir: Optional[str] = typer.Option(None, "--project-dir", help="attached project directory"),
    managed: bool = typer.Option(False, "--managed", help="use managed workspace mode"),
    output_format: str = typer.Option("text", "--output-format", help="text or json"),
) -> None:
    """Show current workspace, backend, and session status."""
    _load_dotenv()
    output_format = _normalize_output_format(output_format)
    cfg = load_config(config)
    resolved_project_dir = _resolve_runtime_project_dir(
        project_dir,
        managed=managed,
        project_id=project_id,
        workspace=workspace,
    )
    snapshot = _collect_status_snapshot(cfg, workspace, resolved_project_dir, project_id)

    if output_format == "json":
        _emit_json(snapshot)
        return

    _print_panel(_format_status_text(snapshot), "[bold cyan]Status[/bold cyan]", "cyan")


@app.command()
def doctor(
    project_id: Optional[str] = typer.Option(None, "--project", "-p", help="managed project id"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="config path"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="managed workspace root"),
    project_dir: Optional[str] = typer.Option(None, "--project-dir", help="attached project directory"),
    managed: bool = typer.Option(False, "--managed", help="use managed workspace mode"),
    output_format: str = typer.Option("text", "--output-format", help="text or json"),
) -> None:
    """Run local health checks for backend, models, workspace, and optional tooling."""
    _load_dotenv()
    output_format = _normalize_output_format(output_format)
    cfg = load_config(config)
    resolved_project_dir = _resolve_runtime_project_dir(
        project_dir,
        managed=managed,
        project_id=project_id,
        workspace=workspace,
    )
    report = _collect_doctor_report(cfg, workspace, resolved_project_dir, project_id, config)

    if output_format == "json":
        _emit_json(report)
        return

    border_style = "green" if report["overall_status"] == "ok" else "yellow"
    if report["overall_status"] == "error":
        border_style = "red"
    _print_panel(_format_doctor_text(report), "[bold cyan]Doctor[/bold cyan]", border_style)


@app.command("config")
def show_config(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="config path"),
    output_format: str = typer.Option("text", "--output-format", help="text or json"),
) -> None:
    """Show the effective runtime config after .env overrides."""
    _load_dotenv()
    output_format = _normalize_output_format(output_format)
    cfg = load_config(config)

    if output_format == "json":
        _emit_json(cfg)
        return

    _print_panel(_format_config_text(cfg), "[bold cyan]Config[/bold cyan]", "cyan")


@app.command()
def agents(
    output_format: str = typer.Option("text", "--output-format", help="text or json"),
) -> None:
    """List the built-in agent roles and their main responsibilities."""
    output_format = _normalize_output_format(output_format)
    catalog = _get_agent_catalog()

    if output_format == "json":
        _emit_json(catalog)
        return

    table = Table(title="Agents")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Role")
    table.add_column("Tools")
    for item in catalog:
        table.add_row(item["name"], item["role"], ", ".join(item["tools"]))
    console.print(table)


@app.command("check-backend")
def check_backend(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    output_format: str = typer.Option("text", "--output-format", help="text or json"),
) -> None:
    """Check backend availability."""
    _load_dotenv()
    output_format = _normalize_output_format(output_format)
    cfg = load_config(config)
    backend_type = cfg.get("backend", {}).get("default", "ollama")
    backend = _create_backend(cfg, backend_type)
    available = backend.is_available()
    models = backend.list_models() if available else []

    if output_format == "json":
        _emit_json(
            {
                "backend": backend_type,
                "available": available,
                "models": models,
            }
        )
        return

    console.print(f"\nBackend: [bold]{backend_type}[/bold]")
    if available:
        console.print("[green]Backend is available.[/green]")
        if models:
            console.print("\nInstalled models:")
            for model in models[:20]:
                console.print(f"  - {model}")
    else:
        console.print("[red]Backend is unavailable.[/red]")
        if backend_type == "ollama":
            console.print("[dim]Try: ollama serve[/dim]")


@app.command("read-doc")
def read_doc(
    file_path: str = typer.Argument(..., help="읽을 문서 경로"),
    project_dir: Optional[str] = typer.Option(None, "--project-dir", help="문서 기준 프로젝트 루트"),
    preview_chars: int = typer.Option(4000, "--preview-chars", help="미리보기 최대 글자 수"),
) -> None:
    """Read PDF/DOCX/Markdown/Text documents from the CLI."""
    _load_dotenv()
    from src.tools.document import DocumentTool

    root = _resolve_project_dir(project_dir) or str(Path.cwd().resolve())
    tool = DocumentTool(root)
    doc = tool.read_document(file_path)

    table = Table(title="Document Info")
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("File", doc.file_path)
    table.add_row("Type", doc.file_type)
    table.add_row("Title", doc.title or "-")
    table.add_row("Chars", str(doc.char_count))
    table.add_row("Words", str(doc.word_count))
    table.add_row("Sections", str(len(doc.sections)))
    if doc.metadata:
        table.add_row("Metadata", json.dumps(doc.metadata, ensure_ascii=False)[:500])
    console.print(table)

    preview = doc.content[:preview_chars]
    if len(doc.content) > preview_chars:
        preview += "\n\n[...truncated...]"
    console.print(Panel(preview or "(empty)", title="Preview", border_style="blue"))


@app.command("make-ppt")
def make_ppt(
    source_path: str = typer.Argument(..., help="슬라이드로 바꿀 입력 문서"),
    output_path: str = typer.Argument(..., help="생성할 .pptx 경로"),
    title: Optional[str] = typer.Option(None, "--title", help="표지 제목 덮어쓰기"),
    subtitle: Optional[str] = typer.Option(None, "--subtitle", help="표지 부제목"),
    project_dir: Optional[str] = typer.Option(None, "--project-dir", help="입력 문서 기준 프로젝트 루트"),
) -> None:
    """Generate PPTX from markdown, text, PDF, or DOCX content."""
    _load_dotenv()
    from src.tools.document import DocumentTool

    root = _resolve_project_dir(project_dir) or str(Path.cwd().resolve())
    tool = DocumentTool(root)
    saved_path = tool.create_presentation_from_file(
        source_path=source_path,
        output_path=output_path,
        title=title,
        subtitle=subtitle,
    )
    console.print(
        Panel.fit(
            f"Input: {source_path}\nOutput: {saved_path}",
            title="PPT Generated",
            border_style="green",
        )
    )


@app.command("search-web")
def search_web(
    query: str = typer.Argument(..., help="검색할 쿼리"),
    max_results: int = typer.Option(5, "--max-results", help="최대 검색 결과 수"),
    output_format: str = typer.Option("text", "--output-format", help="text or json"),
) -> None:
    """Search the web from the CLI."""
    _load_dotenv()
    from src.tools.web_search import WebSearchTool
    output_format = _normalize_output_format(output_format)

    tool = WebSearchTool()
    results = tool.search(query, max_results=max_results)

    if output_format == "json":
        _emit_json(
            {
                "query": query,
                "results": [
                    {
                        "title": result.title,
                        "url": result.url,
                        "snippet": result.snippet,
                        "source": result.source,
                    }
                    for result in results
                ],
            }
        )
        return

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    table = Table(title=f"Web Search: {query}")
    table.add_column("Title", style="cyan")
    table.add_column("URL", style="blue")
    table.add_column("Snippet")
    for result in results:
        table.add_row(result.title, result.url, result.snippet[:200])
    console.print(table)


@app.command()
def ui(
    port: int = typer.Option(8501, "--port", "-p", help="웹 UI 포트"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
    project_dir: Optional[str] = typer.Option(None, "--project-dir", help="웹 UI가 붙을 프로젝트 폴더"),
    managed: bool = typer.Option(
        False,
        "--managed",
        help="현재 폴더 attach 대신 managed workspace UI 실행",
    ),
) -> None:
    """Launch the Streamlit UI."""
    import subprocess

    resolved_project_dir = _resolve_runtime_project_dir(
        project_dir,
        managed=managed,
        workspace=workspace,
    )
    ui_script = str(Path(__file__).parent / "ui" / "app.py")
    env = os.environ.copy()
    if workspace:
        env["WORKSPACE_ROOT"] = workspace
    if resolved_project_dir:
        env["LTA_PROJECT_DIR"] = resolved_project_dir
    else:
        env.pop("LTA_PROJECT_DIR", None)

    console.print(f"\nWeb UI: http://localhost:{port}")
    if resolved_project_dir:
        console.print(f"[dim]Attached project: {resolved_project_dir}[/dim]")
    else:
        console.print("[dim]Managed workspace mode[/dim]")
    console.print("[dim]Stop with Ctrl+C[/dim]\n")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            ui_script,
            "--server.port",
            str(port),
            "--server.headless",
            "true",
        ],
        env=env,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
