import argparse
import sys
from typing import List

# calculator.py에서 핵심 계산 함수를 임포트합니다.
# 프로젝트 구조상 같은 디렉토리에 위치합니다.
try:
    from calculator import calculate
except ImportError:
    print("오류: calculator.py 모듈을 찾을 수 없습니다. 같은 디렉토리에 있는지 확인하세요.")
    sys.exit(1)


def run_smoke_test() -> bool:
    """
    Smoke 테스트를 실행합니다. 필수적인 기능(덧셈)이 정상적으로 작동하는지 확인합니다.
    성공 시 True를 반환하고, 실패 시 False를 반환합니다.
    """
    print("="*50)
    print("🚀 [Smoke Test] 실행 중...")
    print("="*50)
    
    # 테스트 케이스 1: 기본적인 덧셈 테스트 (성공 예상)
    num1, num2, op = 10.0, 5.0, 'add'
    result = calculate(num1, num2, op)
    
    if isinstance(result, float) and result == 15.0:
        print(f"✅ 테스트 성공: {num1} {op} {num2} = {result}")
    else:
        print(f"❌ 테스트 실패: 예상 결과(15.0)와 다름. 결과: {result}")
        return False

    # 테스트 케이스 2: 0으로 나누기 예외 처리 테스트 (성공 예상)
    num1, num2, op = 10.0, 0.0, 'divide'
    result = calculate(num1, num2, op)
    
    if result == "오류: 0으로 나눌 수 없습니다.":
        print(f"✅ 테스트 성공: 0으로 나누기 예외 처리 확인 완료.")
    else:
        print(f"❌ 테스트 실패: 0으로 나누기 예외 처리가 제대로 작동하지 않습니다. 결과: {result}")
        return False

    print("\n🎉 모든 Smoke Test가 성공적으로 완료되었습니다.")
    return True


def main():
    """
    메인 실행 함수. 커맨드라인 인자를 파싱하고 적절한 기능을 실행합니다.
    """
    parser = argparse.ArgumentParser(
        description="간단한 계산기 모듈을 테스트하고 실행하는 엔트리포인트입니다.",
        epilog="예시: python main.py --smoke-test"
    )
    
    parser.add_argument(
        "--smoke-test", 
        action="store_true", 
        help="애플리케이션의 핵심 기능에 대한 간단한 통합 테스트를 실행합니다."
    )
    
    args = parser.parse_args()

    if args.smoke_test:
        success = run_smoke_test()
        if success:
            # 요구사항: smoke-test는 코드 0으로 종료해야 함
            sys.exit(0)
        else:
            # 테스트 실패 시 비정상 종료
            sys.exit(1)
    else:
        # --smoke-test 플래그가 없을 경우 사용법 안내
        print("="*60)
        print("📌 계산기 애플리케이션 시작")
        print("="*60)
        print("💡 사용법 안내:")
        print("   - 테스트 실행: python main.py --smoke-test")
        print("   - 실제 사용: (이 예제에서는 인자 없이 실행 시 기본 사용법 안내만 합니다.)")
        
        # 실제 사용 예시를 보여주기 위해 간단한 호출을 추가할 수 있습니다.
        try:
            result = calculate(20.0, 4.0, 'multiply')
            print(f"\n[데모] 20 * 4 계산 결과: {result}")
        except Exception as e:
            print(f"\n[데모] 기본 계산 시 오류 발생: {e}")


if __name__ == "__main__":
    main()