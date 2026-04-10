# File: main.py
import argparse
import sys
from greeter import greet, smoke_test_greeting

def main():
    """
    CLI 진입점 역할을 하며, 인자 파싱 및 핵심 로직 호출을 관리합니다.
    """
    parser = argparse.ArgumentParser(
        description="명령줄 인터페이스를 통해 사용자에게 인사말을 생성하고 출력합니다."
    )
    
    # 이름 인자 (선택 사항)
    parser.add_argument(
        "name", 
        nargs='?',  # 인자가 없을 수도 있음
        default=None, 
        help="인사할 대상의 이름 (예: 홍길동)"
    )
    
    # 스모크 테스트 플래그
    parser.add_argument(
        "--smoke-test", 
        action="store_true", 
        help="애플리케이션의 핵심 로직을 테스트하는 더미 모드 실행"
    )
    
    args = parser.parse_args()

    # 1. 스모크 테스트 모드 처리
    if args.smoke_test:
        print("--- Running Smoke Test ---")
        try:
            # greeter 모듈의 테스트 함수 호출
            result = smoke_test_greeting()
            # 인코딩 문제를 해결하기 위해 ASCII 기반 상태 표시자로 변경
            print(f"[PASS] {result}")
            # 성공적으로 실행되었으므로 0을 반환하며 종료
            sys.exit(0)
        except Exception as e:
            # 인코딩 문제를 해결하기 위해 ASCII 기반 상태 표시자로 변경
            print(f"[FAIL] Smoke Test Failed: {e}", file=sys.stderr)
            sys.exit(1)

    # 2. 이름 기반 인사말 생성 처리
    if args.name:
        try:
            # greeter 모듈의 핵심 함수 호출
            greeting = greet(args.name)
            print(greeting)
        except ValueError as e:
            print(f"🚨 오류: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # 이름도 없고 스모크 테스트도 아닐 경우 사용법 안내
        print("="*50)
        print("사용법: python main.py <이름> [--smoke-test]")
        print("예시 1 (인사): python main.py 홍길동")
        print("예시 2 (테스트): python main.py --smoke-test")
        print("="*50)
        sys.exit(1)

if __name__ == "__main__":
    main()