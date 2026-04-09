"""
Lightweight runner for Local Team Agent.

Examples:
  python run.py "README 정리해줘"
  python run.py "테스트 추가해줘" --project-dir .
  python run.py --chat
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Team Agent quick runner")
    parser.add_argument("task", nargs="*", help="수행할 작업 설명")
    parser.add_argument("--project", dest="project_id", help="기존 managed 프로젝트 ID")
    parser.add_argument("--project-dir", dest="project_dir", help="붙일 프로젝트 폴더")
    parser.add_argument("--workspace", help="managed workspace 루트")
    parser.add_argument(
        "--managed",
        action="store_true",
        help="현재 폴더 attach 대신 기존 managed workspace 모드 사용",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Claude Code처럼 계속 이어서 사용하는 대화형 모드 시작",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="저장된 이전 대화를 불러오지 않고 새 채팅으로 시작",
    )
    return parser


def _resolve_runtime_project_dir(args: argparse.Namespace) -> str | None:
    if args.project_dir:
        return str(Path(args.project_dir).expanduser().resolve())
    if args.managed or args.project_id or args.workspace:
        return None
    return str(Path.cwd().resolve())


def _sanitize_console_text(text: str) -> str:
    sanitized = text.replace("\u2022", "-").replace("\u2013", "-").replace("\u2014", "-")
    encoding = sys.stdout.encoding or "utf-8"
    return sanitized.encode(encoding, errors="replace").decode(encoding, errors="replace")


def main() -> None:
    args = build_parser().parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from src.logging_utils import setup_logging
    from src.main import chat as chat_command
    from src.setup import create_engine

    setup_logging(level="INFO")

    if args.chat:
        chat_command(
            project_id=args.project_id,
            config=None,
            workspace=args.workspace,
            project_dir=args.project_dir,
            managed=args.managed,
            fresh=args.fresh,
        )
        return

    if not args.task:
        raise SystemExit("task 또는 --chat 이 필요합니다.")

    task = " ".join(args.task)
    resolved_project_dir = _resolve_runtime_project_dir(args)

    def status_print(agent: str, message: str) -> None:
        print(f"  [{agent}] {message}")

    print(f"\n작업: {task}\n")
    if resolved_project_dir:
        print(f"Attached project: {resolved_project_dir}\n")
    elif args.project_id:
        print(f"Managed project: {args.project_id}\n")

    engine = create_engine(
        workspace_root=args.workspace,
        project_id=args.project_id,
        project_root=resolved_project_dir,
        on_status_update=status_print,
    )

    if not engine.manager.backend.is_available():
        print("\nLLM backend에 연결되지 않았습니다.")
        print("다음을 확인해 주세요:")
        print("  ollama serve")
        print("  ollama pull llama3.1:8b")
        sys.exit(1)

    state = engine.run(
        user_task=task,
        project_id=None if resolved_project_dir else args.project_id,
    )

    print("\n" + "=" * 60)
    status_label = state.status.value.upper()
    print(f"최종 결과  [{status_label}]")
    print("=" * 60)
    print(_sanitize_console_text(state.final_output or "(출력 없음)"))

    if state.project_id:
        print(f"\n프로젝트 ID: {state.project_id}")

    if state.status.value != "completed":
        print(f"\n[!] 실행이 '{status_label}' 상태로 종료되었습니다. 프로젝트 폴더에 실제 파일이 생성되지 않았습니다.")


if __name__ == "__main__":
    main()
