"""
Simple Streamlit launcher for Local Team Agent UI.

Examples:
  python run_ui.py
  python run_ui.py --project-dir .
  python run_ui.py --managed --workspace ./workspaces
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Local Team Agent Streamlit UI")
    parser.add_argument("--port", type=int, default=8501, help="Streamlit port")
    parser.add_argument("--project-dir", help="붙일 프로젝트 폴더")
    parser.add_argument("--workspace", help="managed workspace 루트")
    parser.add_argument(
        "--managed",
        action="store_true",
        help="현재 폴더 attach 대신 managed workspace UI 실행",
    )
    return parser


def _resolve_runtime_project_dir(args: argparse.Namespace) -> str | None:
    if args.project_dir:
        return str(Path(args.project_dir).expanduser().resolve())
    if args.managed or args.workspace:
        return None
    return str(Path.cwd().resolve())


def main() -> None:
    args = build_parser().parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    resolved_project_dir = _resolve_runtime_project_dir(args)
    ui_script = str(Path(__file__).parent / "src" / "ui" / "app.py")

    env = os.environ.copy()
    if args.workspace:
        env["WORKSPACE_ROOT"] = args.workspace
    if resolved_project_dir:
        env["LTA_PROJECT_DIR"] = resolved_project_dir
    else:
        env.pop("LTA_PROJECT_DIR", None)

    print("\nLocal Team Agent Web UI")
    print(f"URL: http://localhost:{args.port}")
    if resolved_project_dir:
        print(f"Attached project: {resolved_project_dir}")
    else:
        print("Mode: managed workspace")
    print("종료: Ctrl+C\n")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            ui_script,
            "--server.port",
            str(args.port),
            "--server.headless",
            "true",
        ],
        env=env,
    )


if __name__ == "__main__":
    main()
