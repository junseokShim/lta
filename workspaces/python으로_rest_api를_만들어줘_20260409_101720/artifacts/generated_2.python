import logging
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException, status
from schemas import Item, ItemCreate

# 로깅 설정
logging깅 = logging.getLogger("api_logger")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Python REST API",
    description="FastAPI를 이용한 아이템 관리 API입니다.",
    version="1.0.0"
)

# 인메모리 데이터베이스 (실제 환경에서는 DB 사용 권장)
db: Dict[int, Item] = {}
id_counter: int = 1

@app.post("/items/", response_model=Item, status_code=status.HTTP_201_CREATED)
async def create_item(item_data: ItemCreate) -> Item:

    """
    새로운 아이템을 생성합니다.
    """
    global id_counter
    new_item = Item(id=id_counter, **item_data.model_dump())
    db[id_counter] = new_item
    id_counter += 1
    
    logger.info(f"Item created: {new_item.id} - {new_item.name}")
    return new_item

@app.get("/items/", response_model=List[Item])
async def read_items() -> List[Item]:
    """
    모든 아이템 목록을 조회합니다.
    """
    logger.info("Fetching all items")
    return list(db.values())

@app.get("/items/{item_id}", response_model=Item)
async def read_item(item_id: int) -> Item:
    """
    특정 ID의 아이템을 조회합니다.
    """
    if item_id not in db:
        logger.error(f"Item not found: {item_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"Item with ID {item_id} does not exist"
        )
    return db[item_id]

@app.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(item_id: int) -> None:
    """
    특정 ID의 아이orum을 삭제합니다.
    """
    if item_id not in db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Item not found"
        )
    del db[item_id]
    logger.info(f"Item deleted: {item_id}")
    return None