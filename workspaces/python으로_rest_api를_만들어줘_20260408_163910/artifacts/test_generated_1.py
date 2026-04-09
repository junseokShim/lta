아래 예시는 **`todo_api`** 프로젝트를 기준으로 작성한 **pytest** 테스트 스위트입니다.  
- **FastAPI** 애플리케이션과 **SQLAlchemy**를 사용한 CRUD 로직을 테스트합니다.  
- **인메모리 SQLite** 를 이용해 실제 DB를 사용하지 않고 테스트를 수행합니다.  
- `get_db` 의존성을 **오버라이드** 하여 테스트용 세션을 주입합니다.  
- `TestClient` 를 이용해 HTTP 엔드포인트를 검증하고, `crud` 모듈의 함수를 직접 호출해 단위 테스트를 수행합니다.  
- 정상 케이스와 함께 **엣지 케이스**(빈 입력, 경계값, 오류 입력)를 모두 포함했습니다.  

> **주의**  
> 실제 프로젝트 구조와 모듈명이 조금 다를 수 있으니, import 경로를 필요에 따라 조정하세요.  

```python
tests/test_main.py
"""
pytest 테스트 스위트
- FastAPI 애플리케이션(라우터, 엔드포인트) 테스트
- CRUD 함수 단위 테스트
- 스키마 검증 및 엣지 케이스 테스트
"""

import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# --------------------------------------------------------------------------- #
# 1. 테스트용 데이터베이스 세팅
# --------------------------------------------------------------------------- #
# 1-1. 실제 프로젝트에서 사용되는 Base, SessionLocal, get_db 를 import
#      (프로젝트 구조에 맞게 경로를 수정하세요)
from todo_api.app.database import Base, get_db, SessionLocal
from todo_api.app import crud, schemas, models

# 1-2. 인메모리 SQLite 엔진 생성
engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 1-3. 테스트용 Base에 대한 테이블 생성
Base.metadata.create_all(bind=engine)

# --------------------------------------------------------------------------- #
# 2. pytest fixture: FastAPI TestClient
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def client() -> TestClient:
    """
    FastAPI 애플리케이션에 테스트용 DB 세션을 주입한 TestClient
    """
    # 2-1. get_db 의존성 오버라이드
    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    # 2-2. app import (프로젝트 구조에 맞게 경로를 수정하세요)
    from todo_api.app.main import app

    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)
    yield client

    # 2-3. 의존성 오버라이드 정리
    app.dependency_overrides.clear()

# --------------------------------------------------------------------------- #
# 3. pytest fixture: DB 세션 (CRUD 함수 테스트용)
# --------------------------------------------------------------------------- #
@pytest.fixture
def db_session() -> sessionmaker:
    """
    CRUD 함수 단위 테스트를 위해 직접 DB 세션을 반환
    """
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

# --------------------------------------------------------------------------- #
# 4. 테스트: FastAPI 라우터 엔드포인트
# --------------------------------------------------------------------------- #
def test_root_endpoint(client: TestClient):
    """GET / (root) 엔드포인트는 200 OK 를 반환해야 함"""
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Todo API is running!"}


def test_get_items_empty(client: TestClient):
    """데이터가 없을 때 GET /items 는 빈 리스트를 반환"""
    response = client.get("/items")
    assert response.status_code == 200
    assert response.json() == []


def test_create_item_success(client: TestClient):
    """유효한 데이터로 아이템을 생성하면 200 OK 와 생성된 아이템이 반환"""
    payload = {"title": "Test Item", "description": "A test item", "price": 10.5, "quantity": 5}
    response = client.post("/items", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == payload["title"]
    assert data["description"] == payload["description"]
    assert data["price"] == payload["price"]
    assert data["quantity"] == payload["quantity"]
    assert "id" in data


def test_create_item_missing_field(client: TestClient):
    """필수 필드가 빠지면 422 Unprocessable Entity 를 반환"""
    payload = {"title": "Missing description"}  # price, quantity 필수
    response = client.post("/items", json=payload)
    assert response.status_code == 422
    assert "price" in response.text
    assert "quantity" in response.text


def test_create_item_invalid_type(client: TestClient):
    """가격이 문자열이면