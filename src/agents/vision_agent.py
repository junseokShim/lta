"""
비전 에이전트
이미지 파일을 분석하고 이해하는 역할을 담당합니다.
비전 모델이 설정되지 않은 경우 메타데이터 분석만 수행합니다.
"""

from pathlib import Path
from typing import Optional

from .base import AgentBase
from ..orchestration.messages import AgentTask, AgentResult, AgentRole, Artifact
from ..tools.image import ImageTool, ImageAnalysisResult


class VisionAgent(AgentBase):
    """
    비전 에이전트
    - 이미지 파일을 로드하고 분석합니다.
    - 다이어그램, 스크린샷, UI를 이해합니다.
    - 이미지에서 텍스트를 추출합니다 (OCR).
    - 비전 모델이 없으면 메타데이터 분석으로 폴백합니다.
    """

    def __init__(self, *args, image_tool: Optional[ImageTool] = None, **kwargs):
        super().__init__(*args, **kwargs)
        # ImageTool은 부모의 self.img를 사용
        if image_tool:
            self.img = image_tool

    @property
    def system_prompt(self) -> str:
        return """당신은 이미지 분석 전문가입니다.

역할:
- 이미지의 내용을 정확하고 상세하게 설명합니다.
- UI 스크린샷, 다이어그램, 코드 스크린샷을 분석합니다.
- 이미지에서 중요한 정보를 추출합니다.
- 이미지의 기술적 컨텍스트를 이해합니다.

분석 원칙:
- 보이는 것을 객관적으로 설명합니다.
- 텍스트, 구조, 색상, 레이아웃을 설명합니다.
- 프로그래밍/기술 맥락에서 이미지를 해석합니다.
- 불확실한 내용은 명시적으로 표시합니다.

한국어로 응답하세요."""

    def run(self, task: AgentTask) -> AgentResult:
        """비전 태스크 실행"""
        return self._timed_run(task)

    def analyze_image(
        self,
        image_path: str,
        question: str = "이 이미지를 자세히 설명해주세요.",
        context: Optional[str] = None,
    ) -> ImageAnalysisResult:
        """
        이미지 분석
        Args:
            image_path: 이미지 파일 경로
            question: 이미지에 대한 질문
            context: 분석 컨텍스트 (선택적)
        Returns: ImageAnalysisResult
        """
        if not self.img:
            from ..tools.image import ImageAnalysisResult, ImageInfo
            return ImageAnalysisResult(
                file_path=image_path,
                description="이미지 도구가 초기화되지 않았습니다.",
                metadata=ImageInfo(image_path, "UNKNOWN", 0, 0, "UNKNOWN", 0, 0, False),
                analysis_type="error",
                error="ImageTool not initialized",
            )

        context_question = question
        if context:
            context_question = f"{context}\n\n{question}"

        return self.img.analyze_image(image_path, context_question)

    def analyze_ui_screenshot(self, image_path: str) -> str:
        """
        UI 스크린샷 분석 (특화된 프롬프트 사용)
        Args:
            image_path: UI 스크린샷 경로
        Returns: 분석 결과
        """
        question = """이 UI 스크린샷을 분석하세요:
1. 어떤 종류의 화면인가요? (웹앱, 데스크톱, 모바일 등)
2. 주요 UI 요소들은 무엇인가요? (버튼, 폼, 메뉴, 차트 등)
3. 화면의 주요 기능은 무엇인가요?
4. 사용자 흐름이나 인터랙션을 설명하세요.
5. 디자인 패턴이나 레이아웃 구조를 설명하세요.
6. 발견된 텍스트 내용을 나열하세요."""

        result = self.analyze_image(image_path, question)
        return result.description

    def analyze_architecture_diagram(self, image_path: str) -> str:
        """
        아키텍처 다이어그램 분석
        Args:
            image_path: 다이어그램 이미지 경로
        Returns: 분석 결과
        """
        question = """이 아키텍처 다이어그램을 분석하세요:
1. 전체적인 시스템 아키텍처는 무엇인가요?
2. 어떤 컴포넌트들이 있나요?
3. 컴포넌트 간의 관계와 통신 방식은?
4. 데이터 흐름을 설명하세요.
5. 어떤 기술 스택이 사용되었나요? (표시된 경우)
6. 아키텍처의 특징이나 패턴은?"""

        result = self.analyze_image(image_path, question)
        return result.description

    def extract_text_from_image(self, image_path: str) -> Optional[str]:
        """
        이미지에서 텍스트 추출 (OCR)
        Args:
            image_path: 이미지 파일 경로
        Returns: 추출된 텍스트 또는 None
        """
        if not self.img:
            return None

        # OCR 시도
        ocr_text = self.img.extract_text_ocr(image_path)
        if ocr_text:
            return ocr_text

        # OCR 실패 시 비전 모델로 텍스트 추출
        result = self.analyze_image(
            image_path,
            "이 이미지에서 모든 텍스트를 정확히 추출하세요. 텍스트만 반환하고 다른 설명은 제외하세요."
        )
        return result.description if result.description else None

    def batch_analyze(self, image_paths: list[str], question: str = "") -> list[dict]:
        """
        여러 이미지 배치 분석
        Args:
            image_paths: 이미지 파일 경로 목록
            question: 각 이미지에 대한 질문
        Returns: 분석 결과 목록
        """
        results = []
        default_question = question or "이 이미지를 설명해주세요."

        for path in image_paths:
            self.logger.info(f"이미지 분석 중: {path}")
            result = self.analyze_image(path, default_question)
            results.append({
                "path": path,
                "name": Path(path).name,
                "description": result.description,
                "type": result.analysis_type,
                "width": result.metadata.width,
                "height": result.metadata.height,
                "format": result.metadata.format,
            })

        return results

    def generate_image_report(
        self,
        image_paths: list[str],
        project_context: str = "",
    ) -> Artifact:
        """
        이미지 분석 종합 보고서 생성
        Args:
            image_paths: 분석할 이미지 경로 목록
            project_context: 프로젝트 컨텍스트
        Returns: 보고서 Artifact
        """
        from datetime import datetime

        analyses = self.batch_analyze(image_paths)

        # 분석 결과 요약 프롬프트
        analyses_str = ""
        for a in analyses:
            analyses_str += f"\n### {a['name']} ({a['format']}, {a['width']}x{a['height']})\n{a['description']}\n"

        context_str = f"\n프로젝트 컨텍스트: {project_context}\n" if project_context else ""

        prompt = f"""다음 이미지 분석 결과를 종합하여 보고서를 작성하세요:{context_str}

{analyses_str}

보고서에 포함할 내용:
1. 전체 이미지 분석 요약
2. 주요 발견 사항
3. 이미지들 간의 관계 (있다면)
4. 프로젝트에 대한 인사이트
5. 권장 사항 (있다면)

마크다운 형식으로 작성하세요."""

        report_content = self.generate(prompt)

        # 보고서에 원본 이미지 분석 추가
        full_report = f"""# 이미지 분석 보고서

> 생성일: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
> 분석된 이미지: {len(image_paths)}개

{report_content}

---

## 개별 이미지 분석 상세

{analyses_str}
"""

        return Artifact(
            name="image_analysis_report.md",
            artifact_type="document",
            content=full_report,
            metadata={
                "doc_type": "image_report",
                "image_count": len(image_paths),
            },
        )
