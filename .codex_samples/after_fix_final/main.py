import sys
import argparse

def greet(name: str) -> str:
    """
    입력받은 이름으로 인사말을 생성합니다.
    
    Args:
        name (str): 인사할 대상의 이름
        
    Returns:
        str: 완성된 인사말
    """
    if not name.strip():
        return "안녕하세요, 이름 없는 방문자님!"
    return f"안녕하세요, {name}님! 만나서 반갑습니다."

def run_cli() -> None:
    """
    CLI 애플리케이션의 메인 실행 루프입니다.
    """
    parser = argparse.ArgumentParser(description="이름을 입력받아 인사하는 CLI 도구입니다.")
    parser.add_argument(
        "--name", 
        type=str, 
        help="인사할 사람의 이름 (입력하지 않으면 대화형으로 진행합니다)"
    )
    parser.add_argument(
        "--smoke-test", 
        action="store_true", 
        help="프로그램의 정상 작동 여부를 확인하는 테스트 모드입니다."
    )

    args = parser.parse_args()

    # Smoke Test 모드 구현
    if args.smoke_test:
        print("Running smoke test...")
        test_result = greet("TestUser")
        assert test_result == "안녕하세요, TestUser님! 만나서 반갑습니다."
        print("Smoke test passed successfully!")
        sys.exit(0)

    # 이름 결정 로직
    name = args.name
    if not name:
        try:
            name = input("이름을 입력해주세요: ").strip()
        except EOFError:
            name = ""

    # 결과 출력
    print(greet(name))

if __name__ == "__main__":
    run_cli()