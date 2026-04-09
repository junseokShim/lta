"""
Ollama 백엔드 구현
로컬에서 실행 중인 Ollama 서버와 통신합니다.
https://ollama.ai
"""

import json
import time
import base64
from pathlib import Path
from typing import Iterator, Optional

import requests

from .base import LLMBackend, BackendConfig, GenerateRequest, GenerateResponse
from ..logging_utils import get_logger

logger = get_logger("backend.ollama")


class OllamaBackend(LLMBackend):
    """
    Ollama 로컬 LLM 백엔드
    Ollama가 로컬에 설치되어 실행 중이어야 합니다.

    지원 모델 예시:
    - llama3.1:8b, llama3.2:3b
    - mistral:7b
    - codellama:7b
    - llava:7b (비전)
    - qwen2.5-coder:7b
    """

    def __init__(self, config: BackendConfig):
        super().__init__(config)
        # Ollama 서버 주소 설정
        self.host = config.extra.get("host", "http://localhost:11434")
        self.vision_model = config.extra.get("vision_model", None)
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    def initialize(self) -> bool:
        """Ollama 서버 연결 확인 및 초기화"""
        try:
            if not self.is_available():
                logger.error("Ollama 서버에 연결할 수 없습니다. 'ollama serve'를 실행하세요.")
                return False

            # 모델이 로컬에 있는지 확인
            models = self.list_models()
            if self.config.model not in models:
                logger.warning(
                    f"모델 '{self.config.model}'이 로컬에 없습니다. "
                    f"'ollama pull {self.config.model}'을 실행하세요."
                )
                # 자동으로 pull 시도
                logger.info(f"모델 '{self.config.model}' 자동 다운로드 시도...")
                self._pull_model(self.config.model)

            self._initialized = True
            logger.info(f"Ollama 백엔드 초기화 완료: {self.config.model}")
            return True

        except Exception as e:
            logger.error(f"Ollama 초기화 실패: {e}")
            return False

    def is_available(self) -> bool:
        """Ollama 서버 가용성 확인"""
        try:
            response = self._session.get(f"{self.host}/api/tags", timeout=5)
            return response.status_code == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        """로컬에 설치된 모델 목록 반환"""
        try:
            response = self._session.get(f"{self.host}/api/tags", timeout=10)
            response.raise_for_status()
            data = response.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception as e:
            logger.error(f"모델 목록 조회 실패: {e}")
            return []

    def generate(self, request: GenerateRequest) -> GenerateResponse:
        """동기 텍스트 생성"""
        start_time = time.time()
        model = request.model_override or self.config.model
        context_length = request.context_length_override or self.config.context_length
        payload = {}

        try:
            messages = self._build_messages(request)

            # 이미지가 있으면 비전 모델로 전환
            if request.images:
                if self.vision_model:
                    model = self.vision_model
                else:
                    logger.warning("비전 모델이 설정되지 않았습니다. 이미지를 무시합니다.")
                    request.images = []

            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": request.temperature_override or self.config.temperature,
                    "top_p": self.config.top_p,
                    "num_predict": request.max_tokens_override or self.config.max_tokens,
                    "num_ctx": context_length,
                },
            }

            # 이미지 데이터를 메시지에 포함
            if request.images:
                payload["messages"] = self._add_images_to_messages(messages, request.images)

            response = self._session.post(
                f"{self.host}/api/chat",
                json=payload,
                timeout=(10, self.config.timeout),
            )
            response.raise_for_status()
            data = response.json()

            duration_ms = (time.time() - start_time) * 1000
            content = data.get("message", {}).get("content", "")

            # 토큰 사용량 추출
            eval_count = data.get("eval_count", 0)
            prompt_eval_count = data.get("prompt_eval_count", 0)

            return GenerateResponse(
                content=content,
                model=model,
                tokens_input=prompt_eval_count,
                tokens_output=eval_count,
                duration_ms=duration_ms,
                success=True,
                raw_response=data,
            )

        except requests.Timeout:
            return GenerateResponse(
                success=False,
                error=(
                    f"Ollama 요청 타임아웃 ({self.config.timeout}초). "
                    f"model={model}, num_predict={payload.get('options', {}).get('num_predict')}, "
                    f"num_ctx={payload.get('options', {}).get('num_ctx')}"
                ),
                duration_ms=(time.time() - start_time) * 1000,
            )
        except requests.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else "unknown"
            detail = self._extract_error_detail(e.response)
            return GenerateResponse(
                success=False,
                error=(
                    f"Ollama 요청 오류 ({status_code}): {detail} "
                    f"[model={model}, num_predict={payload.get('options', {}).get('num_predict')}, "
                    f"num_ctx={payload.get('options', {}).get('num_ctx')}]"
                ),
                duration_ms=(time.time() - start_time) * 1000,
            )
        except requests.RequestException as e:
            return GenerateResponse(
                success=False,
                error=(
                    f"Ollama 요청 오류: {str(e)} "
                    f"[model={model}, num_predict={payload.get('options', {}).get('num_predict')}, "
                    f"num_ctx={payload.get('options', {}).get('num_ctx')}]"
                ),
                duration_ms=(time.time() - start_time) * 1000,
            )
        except Exception as e:
            return GenerateResponse(
                success=False,
                error=f"예상치 못한 오류: {str(e)}",
                duration_ms=(time.time() - start_time) * 1000,
            )

    def generate_stream(self, request: GenerateRequest) -> Iterator[str]:
        """스트리밍 텍스트 생성"""
        model = request.model_override or self.config.model
        messages = self._build_messages(request)

        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": request.temperature_override or self.config.temperature,
                "top_p": self.config.top_p,
                "num_predict": request.max_tokens_override or self.config.max_tokens,
                "num_ctx": self.config.context_length,
            },
        }

        try:
            with self._session.post(
                f"{self.host}/api/chat",
                json=payload,
                stream=True,
                timeout=(10, self.config.timeout),
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line:
                        try:
                            data = json.loads(line)
                            chunk = data.get("message", {}).get("content", "")
                            if chunk:
                                yield chunk
                            # 완료 신호
                            if data.get("done", False):
                                break
                        except json.JSONDecodeError:
                            continue

        except Exception as e:
            logger.error(f"스트리밍 오류: {e}")
            yield f"\n[오류: {str(e)}]"

    def supports_vision(self) -> bool:
        """비전 모델 설정 여부로 판단"""
        return self.vision_model is not None

    def _pull_model(self, model_name: str) -> bool:
        """모델 자동 다운로드"""
        try:
            logger.info(f"'{model_name}' 다운로드 중... (시간이 걸릴 수 있습니다)")
            response = self._session.post(
                f"{self.host}/api/pull",
                json={"name": model_name},
                stream=True,
                timeout=600,  # 다운로드는 10분 허용
            )
            for line in response.iter_lines():
                if line:
                    data = json.loads(line)
                    status = data.get("status", "")
                    if "pulling" in status or "verifying" in status:
                        completed = data.get("completed", 0)
                        total = data.get("total", 1)
                        if total > 0:
                            pct = (completed / total) * 100
                            logger.info(f"다운로드 중: {pct:.1f}%")
            logger.info(f"'{model_name}' 다운로드 완료")
            return True
        except Exception as e:
            logger.error(f"모델 다운로드 실패: {e}")
            return False

    def _add_images_to_messages(self, messages: list[dict], image_paths: list[str]) -> list[dict]:
        """메시지에 이미지 데이터를 base64로 포함"""
        updated_messages = messages.copy()
        image_data_list = []

        for img_path in image_paths:
            path = Path(img_path)
            if path.exists():
                with open(path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode("utf-8")
                    image_data_list.append(img_b64)

        # 마지막 사용자 메시지에 이미지 추가
        if image_data_list and updated_messages:
            for i in range(len(updated_messages) - 1, -1, -1):
                if updated_messages[i]["role"] == "user":
                    updated_messages[i]["images"] = image_data_list
                    break

        return updated_messages

    def get_model_info(self, model_name: Optional[str] = None) -> dict:
        """모델 상세 정보 조회"""
        model = model_name or self.config.model
        try:
            response = self._session.post(
                f"{self.host}/api/show",
                json={"name": model},
                timeout=10,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"모델 정보 조회 실패: {e}")
            return {}

    def _extract_error_detail(self, response: Optional[requests.Response]) -> str:
        """HTTP 오류 응답에서 사람이 읽기 좋은 세부 메시지를 꺼낸다."""
        if response is None:
            return "응답 본문 없음"

        try:
            data = response.json()
            if isinstance(data, dict):
                detail = data.get("error") or data.get("message")
                if detail:
                    return str(detail)
        except ValueError:
            pass

        text = (response.text or "").strip()
        if text:
            return text[:500]

        return response.reason or "알 수 없는 HTTP 오류"
