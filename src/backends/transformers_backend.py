"""
HuggingFace Transformers 백엔드 구현
로컬에서 HF 모델을 직접 로드하여 실행합니다.
CUDA / MPS (Apple Silicon) / CPU 환경을 자동으로 감지하여 최적 설정으로 동작합니다.
"""

import time
from typing import Iterator, Optional

from .base import LLMBackend, BackendConfig, GenerateRequest, GenerateResponse
from .device_utils import (
    resolve_device,
    supports_quantization,
    get_torch_dtype,
    log_memory_stats,
    clear_device_cache,
)
from ..logging_utils import get_logger

logger = get_logger("backend.transformers")


class TransformersBackend(LLMBackend):
    """
    HuggingFace Transformers 로컬 백엔드

    디바이스 지원:
    - CUDA  : 4bit/8bit 양자화(bitsandbytes) + float16, device_map="auto"
    - MPS   : 양자화 없이 float32, 명시적 디바이스 배치 (Apple Silicon M1/M2/M3)
    - CPU   : 양자화 없이 float32

    config.extra 키:
    - device        : "auto" | "cuda" | "mps" | "cpu"  (기본: "auto")
    - load_in_4bit  : bool  (CUDA 전용, MPS/CPU에서는 무시됨)
    - load_in_8bit  : bool  (CUDA 전용, MPS/CPU에서는 무시됨)
    """

    def __init__(self, config: BackendConfig):
        super().__init__(config)
        self.model_id = config.model

        # "auto"이면 런타임에 최적 디바이스 선택 (CUDA → MPS → CPU)
        requested_device = config.extra.get("device", "auto")
        self.device = resolve_device(requested_device)

        # 양자화는 CUDA에서만 동작합니다
        self.load_in_4bit = config.extra.get("load_in_4bit", True) and supports_quantization(self.device)
        self.load_in_8bit = config.extra.get("load_in_8bit", False) and supports_quantization(self.device)

        if config.extra.get("load_in_4bit") and not supports_quantization(self.device):
            logger.info(
                f"4bit 양자화는 CUDA 전용입니다. "
                f"디바이스={self.device}에서는 전체 정밀도로 로드합니다."
            )

        # 모델과 토크나이저는 지연 로드 (lazy loading)
        self._model = None
        self._tokenizer = None
        self._pipeline = None

    def initialize(self) -> bool:
        """모델 로드 및 초기화"""
        try:
            logger.info(f"모델 로드 중: {self.model_id}")
            logger.info(
                f"설정: device={self.device}, "
                f"4bit={self.load_in_4bit}, 8bit={self.load_in_8bit}"
            )

            # 지연 임포트 (설치되지 않은 경우 에러 방지)
            import torch
            from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline

            torch_dtype = get_torch_dtype(self.device)

            # 양자화 설정 (CUDA 전용)
            quantization_config = None
            if self.load_in_4bit or self.load_in_8bit:
                from transformers import BitsAndBytesConfig
                if self.load_in_4bit:
                    quantization_config = BitsAndBytesConfig(
                        load_in_4bit=True,
                        bnb_4bit_use_double_quant=True,
                        bnb_4bit_quant_type="nf4",
                        bnb_4bit_compute_dtype=torch.float16,
                    )
                else:
                    quantization_config = BitsAndBytesConfig(load_in_8bit=True)

            # 토크나이저 로드
            self._tokenizer = AutoTokenizer.from_pretrained(
                self.model_id,
                trust_remote_code=True,
            )

            # 모델 로드 — 디바이스별 전략이 다릅니다
            model_kwargs = {"trust_remote_code": True}

            if quantization_config:
                # CUDA 양자화: device_map="auto"로 VRAM 자동 배분
                model_kwargs["quantization_config"] = quantization_config
                model_kwargs["device_map"] = "auto"
            elif self.device == "cuda":
                # CUDA 비양자화: device_map="auto" + float16
                model_kwargs["torch_dtype"] = torch_dtype
                model_kwargs["device_map"] = "auto"
            elif self.device == "mps":
                # MPS: device_map은 "auto"가 MPS를 지원하지 않으므로 명시적 배치
                model_kwargs["torch_dtype"] = torch_dtype
                # device_map 없이 로드 후 .to(device) 사용
            else:
                # CPU
                model_kwargs["torch_dtype"] = torch_dtype

            self._model = AutoModelForCausalLM.from_pretrained(
                self.model_id,
                **model_kwargs,
            )

            # MPS/CPU는 device_map을 쓰지 않았으므로 명시적으로 디바이스에 올림
            if self.device in ("mps", "cpu"):
                self._model = self._model.to(torch.device(self.device))

            # 파이프라인 생성
            pipeline_kwargs: dict = {
                "model": self._model,
                "tokenizer": self._tokenizer,
            }
            if self.device == "cuda" or quantization_config:
                pipeline_kwargs["device_map"] = "auto"
            else:
                # MPS/CPU는 device 번호 대신 device 객체로 지정
                pipeline_kwargs["device"] = torch.device(self.device)

            self._pipeline = pipeline("text-generation", **pipeline_kwargs)

            self._initialized = True
            logger.info(f"모델 로드 완료: {self.model_id} (device={self.device})")
            log_memory_stats(self.device)
            return True

        except ImportError as e:
            logger.error(
                f"필요한 패키지가 없습니다: {e}. "
                f"'pip install transformers torch'를 실행하세요. "
                f"CUDA 양자화에는 추가로 bitsandbytes가 필요합니다."
            )
            return False
        except Exception as e:
            logger.error(f"모델 로드 실패: {e}")
            return False

    def generate(self, request: GenerateRequest) -> GenerateResponse:
        """텍스트 생성"""
        if not self._initialized or self._pipeline is None:
            if not self.initialize():
                return GenerateResponse(success=False, error="모델이 초기화되지 않았습니다.")

        start_time = time.time()

        try:
            messages = self._build_messages(request)
            prompt = self._messages_to_prompt(messages)

            max_new_tokens = request.max_tokens_override or self.config.max_tokens
            temperature = request.temperature_override or self.config.temperature

            outputs = self._pipeline(
                prompt,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_p=self.config.top_p,
                do_sample=temperature > 0,
                pad_token_id=self._tokenizer.eos_token_id,
                return_full_text=False,
            )

            content = outputs[0]["generated_text"]
            duration_ms = (time.time() - start_time) * 1000

            return GenerateResponse(
                content=content,
                model=self.model_id,
                duration_ms=duration_ms,
                success=True,
            )

        except Exception as e:
            return GenerateResponse(
                success=False,
                error=f"생성 오류: {str(e)}",
                duration_ms=(time.time() - start_time) * 1000,
            )

    def generate_stream(self, request: GenerateRequest) -> Iterator[str]:
        """스트리밍 생성 (단어 단위 시뮬레이션)"""
        response = self.generate(request)
        if response.success:
            words = response.content.split()
            for i, word in enumerate(words):
                yield word + (" " if i < len(words) - 1 else "")
        else:
            yield f"[오류: {response.error}]"

    def is_available(self) -> bool:
        """패키지 설치 여부 확인"""
        try:
            import transformers  # noqa: F401
            import torch  # noqa: F401
            return True
        except ImportError:
            return False

    def list_models(self) -> list[str]:
        """로컬 캐시된 모델 목록 (간단 구현)"""
        return [self.model_id]

    def _messages_to_prompt(self, messages: list[dict]) -> str:
        """메시지 리스트를 단일 프롬프트 문자열로 변환"""
        if self._tokenizer and hasattr(self._tokenizer, "apply_chat_template"):
            try:
                return self._tokenizer.apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=True,
                )
            except Exception:
                pass

        # 폴백: 수동 포맷팅
        prompt_parts = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if role == "system":
                prompt_parts.append(f"<|system|>\n{content}</s>")
            elif role == "user":
                prompt_parts.append(f"<|user|>\n{content}</s>")
            elif role == "assistant":
                prompt_parts.append(f"<|assistant|>\n{content}</s>")
        prompt_parts.append("<|assistant|>")
        return "\n".join(prompt_parts)

    def unload(self) -> None:
        """메모리 해제"""
        try:
            if self._model is not None:
                del self._model
                self._model = None
            if self._pipeline is not None:
                del self._pipeline
                self._pipeline = None
            clear_device_cache(self.device)
            self._initialized = False
            logger.info("모델 언로드 완료")
        except Exception as e:
            logger.error(f"모델 언로드 오류: {e}")
