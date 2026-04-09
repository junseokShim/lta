"""
실행 예제 모음
다양한 use case를 보여주는 예제 스크립트입니다.
실행 전 Ollama 서버가 실행 중이어야 합니다: ollama serve
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.setup import create_engine
from src.logging_utils import setup_logging


def status_print(agent: str, message: str):
    """상태 출력 콜백"""
    print(f"  [{agent}] {message}")


def example_1_build_rest_api():
    """예제 1: Python REST API 구축"""
    print("\n" + "="*60)
    print("예제 1: Python REST API 구축")
    print("="*60)

    engine = create_engine(on_status_update=status_print)

    state = engine.run(
        user_task="FastAPI를 사용하여 사용자 관리 REST API를 만들어줘. "
                  "CRUD 엔드포인트(생성, 읽기, 수정, 삭제)와 "
                  "SQLite 데이터베이스를 사용해줘.",
    )

    print("\n--- 결과 ---")
    print(state.final_output[:1000])
    print(f"\n생성된 파일: {[a.name for r in state.results for a in r.artifacts]}")


def example_2_analyze_repo():
    """예제 2: 레포지토리 분석 및 리팩토링 제안"""
    print("\n" + "="*60)
    print("예제 2: 레포지토리 분석")
    print("="*60)

    engine = create_engine(on_status_update=status_print)

    state = engine.run(
        user_task="현재 프로젝트를 분석하고 코드 품질 개선사항과 "
                  "리팩토링 기회를 제안해줘.",
    )

    print("\n--- 결과 ---")
    print(state.final_output[:1000])


def example_3_generate_documentation():
    """예제 3: 문서 생성"""
    print("\n" + "="*60)
    print("예제 3: 프로젝트 문서 생성")
    print("="*60)

    engine = create_engine(on_status_update=status_print)

    state = engine.run(
        user_task="프로젝트 파일을 읽고 종합적인 README.md와 "
                  "API 문서를 생성해줘. 설치 방법과 사용 예제를 포함해줘.",
    )

    print("\n--- 결과 ---")
    print(state.final_output[:1000])


def example_4_debug_code():
    """예제 4: 버그 디버깅"""
    print("\n" + "="*60)
    print("예제 4: 코드 디버깅")
    print("="*60)

    # 버그 있는 코드 예제
    buggy_code = '''
def calculate_average(numbers):
    total = 0
    for num in numbers:
        total += num
    return total / len(numbers)  # ZeroDivisionError 가능!

def find_max(lst):
    max_val = lst[0]  # IndexError 가능!
    for item in lst:
        if item > max_val:
            max_val = item
    return max_val

# 테스트
print(calculate_average([]))
print(find_max([]))
'''

    engine = create_engine(on_status_update=status_print)

    # 버그 있는 파일 생성
    import tempfile, os
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(buggy_code)
        tmp_path = f.name

    state = engine.run(
        user_task=f"다음 Python 코드에서 버그를 찾아서 수정해줘:\n\n{buggy_code}",
    )

    print("\n--- 결과 ---")
    print(state.final_output[:1000])
    os.unlink(tmp_path)


def example_5_project_summary():
    """예제 5: 프로젝트 요약 보고서"""
    print("\n" + "="*60)
    print("예제 5: 프로젝트 요약 보고서")
    print("="*60)

    engine = create_engine(on_status_update=status_print)

    state = engine.run(
        user_task="현재 프로젝트의 모든 문서와 코드를 읽고 "
                  "종합 요약 보고서를 작성해줘. "
                  "프로젝트 목적, 주요 기능, 기술 스택, "
                  "개선 방향을 포함해줘.",
    )

    print("\n--- 결과 ---")
    print(state.final_output[:1000])


def example_6_quick_code():
    """예제 6: 빠른 코드 생성 (단일 에이전트)"""
    print("\n" + "="*60)
    print("예제 6: 빠른 코드 생성 (단일 에이전트 모드)")
    print("="*60)

    engine = create_engine(on_status_update=status_print)

    result = engine.run_quick(
        "Python에서 이진 탐색 알고리즘을 구현해줘. "
        "테스트 케이스도 포함해줘.",
        agent_role="coder",
    )

    print("\n--- 결과 ---")
    print(result[:1000])


def main():
    setup_logging(level="INFO")

    print("Local Team Agent - 실행 예제")
    print("Ollama 서버가 실행 중인지 확인하세요: ollama serve")
    print()

    examples = {
        "1": ("Python REST API 구축", example_1_build_rest_api),
        "2": ("레포지토리 분석", example_2_analyze_repo),
        "3": ("문서 생성", example_3_generate_documentation),
        "4": ("버그 디버깅", example_4_debug_code),
        "5": ("프로젝트 요약 보고서", example_5_project_summary),
        "6": ("빠른 코드 생성", example_6_quick_code),
        "all": ("모든 예제 실행", None),
    }

    print("실행할 예제를 선택하세요:")
    for key, (name, _) in examples.items():
        print(f"  {key}: {name}")

    choice = input("\n선택 (기본: 6): ").strip() or "6"

    if choice == "all":
        for key, (name, func) in examples.items():
            if func:
                func()
    elif choice in examples and examples[choice][1]:
        examples[choice][1]()
    else:
        print("잘못된 선택입니다.")


if __name__ == "__main__":
    main()
