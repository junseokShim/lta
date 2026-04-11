"""
계산기 핵심 로직을 담고 있는 모듈입니다.
순수 함수로 구성되어 있어 테스트 용이성이 높습니다.
"""

from typing import Union

# 연산자 매핑 딕셔너리 정의
OPERATIONS = {
    'add': lambda a, b: a + b,
    'subtract': lambda a, b: a - b,
    'multiply': lambda a, b: a * b,
    'divide': lambda a, b: a / b,
}

def calculate(num1: float, num2: float, operation: str) -> Union[float, str]:
    """
    두 숫자와 연산자에 따라 계산을 수행합니다.

    Args:
        num1: 첫 번째 피연산자.
        num2: 두 번째 피연산자.
        operation: 수행할 연산자 ('add', 'subtract', 'multiply', 'divide').

    Returns:
        계산 결과 (float) 또는 오류 메시지 (str).
    """
    operation_key = operation.lower()
    
    if operation_key not in OPERATIONS:
        return f"오류: 지원하지 않는 연산자입니다. 지원 목록: {', '.join(OPERATIONS.keys())}"

    try:
        # 0으로 나누기 예외 처리
        if operation_key == 'divide' and num2 == 0:
            return "오류: 0으로 나눌 수 없습니다."
            
        # 해당 연산자에 맞는 람다 함수를 가져와 실행
        operation_func = OPERATIONS[operation_key]
        result = operation_func(num1, num2)
        return result
    except Exception as e:
        # 예상치 못한 오류 처리
        return f"계산 중 알 수 없는 오류가 발생했습니다: {e}"

# 타입 힌트 및 명확성을 위해 연산자 목록을 상수화합니다.
SUPPORTED_OPERATIONS = list(OPERATIONS.keys())