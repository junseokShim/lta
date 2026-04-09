"""
이미지 처리 도구
로컬 이미지 파일을 로드하고 기본 분석을 수행합니다.
비전 모델이 설정된 경우 이미지 이해 요청을 처리합니다.
"""

import base64
from pathlib import Path
from typing import Optional, Any
from dataclasses import dataclass

from ..logging_utils import get_logger

logger = get_logger("tools.image")

SUPPORTED_FORMATS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff"}


@dataclass
class ImageInfo:
    """이미지 메타데이터"""
    file_path: str
    format: str
    width: int
    height: int
    mode: str  # RGB, RGBA, L (그레이스케일) 등
    file_size_bytes: int
    channels: int
    has_transparency: bool
    base64_data: Optional[str] = None  # 비전 모델에 전달하기 위한 base64 데이터


@dataclass
class ImageAnalysisResult:
    """이미지 분석 결과"""
    file_path: str
    description: str
    metadata: ImageInfo
    analysis_type: str  # "metadata_only", "vision_model", "ocr"
    text_content: Optional[str] = None  # OCR 결과
    error: Optional[str] = None


class ImageTool:
    """
    이미지 로딩 및 분석 도구

    비전 모델 없이도 메타데이터 추출은 항상 동작합니다.
    비전 모델이 설정된 경우 이미지 이해가 활성화됩니다.
    """

    def __init__(self, workspace_root: str, vision_backend: Optional[Any] = None):
        """
        Args:
            workspace_root: 워크스페이스 루트 경로
            vision_backend: 비전 지원 LLM 백엔드 (선택적)
        """
        self.workspace_root = Path(workspace_root)
        self.vision_backend = vision_backend

    def set_workspace_root(self, workspace_root: str) -> None:
        """작업 루트를 동적으로 바꾼다."""
        self.workspace_root = Path(workspace_root).resolve()

    def load_image(self, image_path: str) -> ImageInfo:
        """
        이미지 파일 로드 및 메타데이터 추출
        Args:
            image_path: 이미지 파일 경로
        Returns: ImageInfo
        """
        path = self._resolve_path(image_path)

        if not path.exists():
            raise FileNotFoundError(f"이미지 파일을 찾을 수 없습니다: {image_path}")

        ext = path.suffix.lower()
        if ext not in SUPPORTED_FORMATS:
            raise ValueError(f"지원하지 않는 이미지 형식: {ext}")

        file_size = path.stat().st_size

        try:
            from PIL import Image
            with Image.open(path) as img:
                width, height = img.size
                mode = img.mode
                fmt = img.format or ext.lstrip(".")

                # 투명도 확인
                has_transparency = mode in ("RGBA", "LA", "PA") or (
                    mode == "P" and "transparency" in img.info
                )

                # 채널 수
                channels_map = {"L": 1, "P": 1, "RGB": 3, "RGBA": 4, "CMYK": 4, "LA": 2}
                channels = channels_map.get(mode, 3)

                # base64 인코딩 (비전 모델 전달용)
                with open(path, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode("utf-8")

                info = ImageInfo(
                    file_path=str(path),
                    format=fmt.upper(),
                    width=width,
                    height=height,
                    mode=mode,
                    file_size_bytes=file_size,
                    channels=channels,
                    has_transparency=has_transparency,
                    base64_data=b64_data,
                )
                logger.debug(f"이미지 로드: {image_path} ({width}x{height} {mode})")
                return info

        except ImportError:
            logger.warning("Pillow가 설치되지 않았습니다. 기본 메타데이터만 반환합니다.")
            # Pillow 없이 기본 정보만 반환
            with open(path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("utf-8")

            return ImageInfo(
                file_path=str(path),
                format=ext.lstrip(".").upper(),
                width=0,
                height=0,
                mode="UNKNOWN",
                file_size_bytes=file_size,
                channels=0,
                has_transparency=False,
                base64_data=b64_data,
            )

    def analyze_image(
        self,
        image_path: str,
        question: str = "이 이미지를 자세히 설명해주세요.",
    ) -> ImageAnalysisResult:
        """
        이미지 분석
        비전 모델이 있으면 LLM 분석, 없으면 메타데이터만 반환합니다.

        Args:
            image_path: 이미지 파일 경로
            question: 이미지에 대한 질문
        Returns: ImageAnalysisResult
        """
        try:
            image_info = self.load_image(image_path)
        except Exception as e:
            return ImageAnalysisResult(
                file_path=image_path,
                description="",
                metadata=ImageInfo(image_path, "UNKNOWN", 0, 0, "UNKNOWN", 0, 0, False),
                analysis_type="error",
                error=str(e),
            )

        # 비전 모델이 없으면 메타데이터 설명만 반환
        if self.vision_backend is None or not self.vision_backend.supports_vision():
            description = self._describe_metadata(image_info)
            return ImageAnalysisResult(
                file_path=image_path,
                description=description,
                metadata=image_info,
                analysis_type="metadata_only",
            )

        # 비전 모델로 분석
        try:
            from ..backends.base import GenerateRequest
            request = GenerateRequest(
                prompt=question,
                system_prompt="당신은 이미지 분석 전문가입니다. 이미지를 자세하고 정확하게 설명해주세요.",
                images=[image_path],
            )
            response = self.vision_backend.generate(request)

            if response.success:
                return ImageAnalysisResult(
                    file_path=image_path,
                    description=response.content,
                    metadata=image_info,
                    analysis_type="vision_model",
                )
            else:
                logger.warning(f"비전 모델 분석 실패: {response.error}")
                description = self._describe_metadata(image_info)
                return ImageAnalysisResult(
                    file_path=image_path,
                    description=description,
                    metadata=image_info,
                    analysis_type="metadata_only",
                    error=response.error,
                )

        except Exception as e:
            logger.error(f"이미지 분석 오류: {e}")
            description = self._describe_metadata(image_info)
            return ImageAnalysisResult(
                file_path=image_path,
                description=description,
                metadata=image_info,
                analysis_type="metadata_only",
                error=str(e),
            )

    def extract_text_ocr(self, image_path: str) -> Optional[str]:
        """
        OCR로 이미지에서 텍스트 추출 (pytesseract 필요)
        Args:
            image_path: 이미지 파일 경로
        Returns: 추출된 텍스트 또는 None
        """
        try:
            import pytesseract
            from PIL import Image

            path = self._resolve_path(image_path)
            with Image.open(path) as img:
                # 그레이스케일 변환으로 OCR 품질 향상
                if img.mode != "L":
                    img = img.convert("L")
                text = pytesseract.image_to_string(img, lang="kor+eng")
                logger.info(f"OCR 완료: {image_path} ({len(text)} 문자)")
                return text.strip()

        except ImportError:
            logger.warning("pytesseract가 설치되지 않았습니다. OCR을 사용할 수 없습니다.")
            return None
        except Exception as e:
            logger.error(f"OCR 오류: {e}")
            return None

    def resize_image(
        self,
        image_path: str,
        output_path: str,
        max_width: int = 1024,
        max_height: int = 1024,
    ) -> Optional[str]:
        """
        이미지 크기 조정 (비전 모델 입력 최적화)
        Args:
            image_path: 원본 이미지 경로
            output_path: 출력 이미지 경로
            max_width: 최대 너비
            max_height: 최대 높이
        Returns: 저장된 파일 경로 또는 None
        """
        try:
            from PIL import Image

            src_path = self._resolve_path(image_path)
            dst_path = self._resolve_path(output_path)
            dst_path.parent.mkdir(parents=True, exist_ok=True)

            with Image.open(src_path) as img:
                img.thumbnail((max_width, max_height), Image.LANCZOS)
                img.save(dst_path)
                logger.info(f"이미지 리사이즈: {image_path} -> {img.size}")
                return str(dst_path)

        except ImportError:
            logger.warning("Pillow가 설치되지 않았습니다.")
            return None
        except Exception as e:
            logger.error(f"이미지 리사이즈 오류: {e}")
            return None

    def list_images(self, directory: str = ".") -> list[str]:
        """
        디렉토리 내 이미지 파일 목록
        Args:
            directory: 검색 디렉토리
        Returns: 이미지 파일 경로 목록
        """
        dir_path = self._resolve_path(directory)
        images = []
        for fmt in SUPPORTED_FORMATS:
            images.extend(dir_path.rglob(f"*{fmt}"))
            images.extend(dir_path.rglob(f"*{fmt.upper()}"))
        return [str(p.relative_to(self.workspace_root)) for p in sorted(set(images))]

    def get_image_for_prompt(self, image_path: str) -> dict:
        """
        비전 모델 프롬프트에 사용할 이미지 데이터 반환
        Returns: {"type": "image", "path": "...", "base64": "..."}
        """
        info = self.load_image(image_path)
        return {
            "type": "image",
            "path": image_path,
            "format": info.format,
            "size": f"{info.width}x{info.height}",
            "base64": info.base64_data,
        }

    def _describe_metadata(self, info: ImageInfo) -> str:
        """이미지 메타데이터를 사람이 읽기 좋은 설명으로 변환"""
        size_kb = info.file_size_bytes / 1024
        size_mb = size_kb / 1024

        desc_parts = [
            f"이미지 파일: {Path(info.file_path).name}",
            f"형식: {info.format}",
        ]

        if info.width > 0:
            desc_parts.append(f"크기: {info.width} x {info.height} 픽셀")

        if info.mode != "UNKNOWN":
            mode_desc = {
                "RGB": "RGB 컬러",
                "RGBA": "RGBA 컬러 (투명도 포함)",
                "L": "그레이스케일",
                "CMYK": "CMYK 컬러",
                "P": "팔레트 컬러",
            }.get(info.mode, info.mode)
            desc_parts.append(f"색상 모드: {mode_desc}")

        if size_mb >= 1:
            desc_parts.append(f"파일 크기: {size_mb:.2f} MB")
        else:
            desc_parts.append(f"파일 크기: {size_kb:.1f} KB")

        if info.has_transparency:
            desc_parts.append("투명도: 있음")

        desc_parts.append("\n[비전 모델이 설정되지 않아 메타데이터만 표시됩니다. llava 등 비전 모델을 설정하면 이미지 내용을 분석할 수 있습니다.]")

        return "\n".join(desc_parts)

    def _resolve_path(self, file_path: str) -> Path:
        """경로 해석"""
        path = Path(file_path)
        if path.is_absolute():
            return path
        return self.workspace_root / file_path
