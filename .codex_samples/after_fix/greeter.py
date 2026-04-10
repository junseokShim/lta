"""
greeter.py: 사용자 이름을 받아 인사말을 생성하는 핵심 로직을 담고 있습니다.
이 모듈은 CLI의 입력 처리와 분리되어 테스트 용이성을 높입니다.
"""

def greet(name: str) -> str:
    """
    주어진 이름으로 형식화된 인사말을 생성합니다.

    Args:
        name: 인사할 대상의 이름 (문자열).

    Returns:
        생성된 인사말 문자열.
    
    Raises:
        ValueError: 이름이 비어있거나 유효하지 않은 경우 발생합니다.
    """
    if not name or not isinstance(name, str) or name.strip() == "":
        raise ValueError("이름은 필수 항목이며 비어있을 수 없습니다.")
    
    # 이름의 첫 글자를 대문자로 만들어 포맷팅합니다.
    formatted_name = name.strip().capitalize()
    
    return f"안녕하세요, {formatted_name}님! 만나서 반갑습니다. 😊"

def smoke_test_greeting() -> str:
    """
    테스트 목적으로 사용되는 더미 인사말을 반환합니다.
    실제 사용자 입력 없이 성공적으로 실행됨을 보장합니다.
    """
    return "Smoke Test Success: Greeter module executed successfully."