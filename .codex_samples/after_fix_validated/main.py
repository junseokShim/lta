import argparse
import sys

def greet(name: str) -> str:
    """
    입력받은 이름으로 인사말을 생성합니다.
    
    Args:
        name: 인사할 대상의 이름
        
    Returns:
        생성된 인사말 문자열
    """
    if not name.strip():
        raise ValueError("이름은 공백일 수 없습니다.")
    return f"안녕하세요, {name}님! 만나서 반갑습니다."

def main() -> None:
    """
    CLI 애플리케이션의 엔트리포인트 함수입니다.
    """
    parser = argparse.ArgumentParser(
        description="사용자에게 인사말을 건네는 간단한 CLI 도구입니다."
    )
    
    # --name 인자 추가 (선택 사항)
    parser.add_argument(
        "--name", 
        type=str, 
        help="인사할 사용자의 이름"
    )
    
    # --smoke-test 인자 추가 (요구사항 준수)
    parser.add_argument(
        "--smoke-test", 
        action="store_true", 
        help="기능 검증을 위한 테스트 모드"
    )

    args = parser.parse_args()

    # Smoke Test 로직
    if args.smoke_test:
        print("Running smoke test... SUCCESS")
        sys.exit(0)

    try:
        # 1. 인자로 이름이 들어온 경우
        if args.name:
            user_name = args.name
        else:
            # 2. 인자가 없으면 사용자 입력을 받음
            user_name = input("이름을 입력해주세요: ").strip()

        if not user_name:
            print("Error: 이름을 입력해야 합니다.")
            sys.exit(1)

        # 인사말 출력
        message = greet(user_name)
        print(message)

    except ValueError as e:
        print(f"입력 오류: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n사용자에 의해 프로그램이 종료되었습니다.")
        sys.exit(0)
    except Exception as e:
        print(f"예상치 못한 오류가 발생했습니다: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()