# Local Team Agent

Local Team Agent는 로컬 LLM을 기반으로 여러 역할의 에이전트를 조합해 코딩 작업을 수행하는 CLI/UI 도구입니다.

현재 버전 기준 핵심 목표는 다음 세 가지입니다.

- 현재 작업 중인 프로젝트 폴더에 바로 붙어서 에이전트처럼 동작할 것
- Claude Code처럼 CMD에서 대화형으로 계속 이어서 사용할 수 있을 것
- 문서 읽기, PPT 생성, 인터넷 검색, 코드 수정, 검증까지 한 흐름으로 다룰 것

이 프로젝트는 이제 단순한 "다단계 프롬프트 실행기"가 아니라, 프로젝트 컨텍스트를 읽고, 파일을 실제로 반영하고, 검증까지 수행하는 Team-agent 형태에 더 가깝게 동작합니다.

## 현재 버전 요약

- 기본 실행 모드는 `attached project` 입니다.
- 별도 옵션이 없으면 현재 폴더를 작업 대상 프로젝트로 사용합니다.
- `lta chat` 또는 `python run.py --chat` 로 대화형 세션을 시작할 수 있습니다.
- 같은 프로젝트에서 다시 `chat` 를 실행하면 최근 대화 이력을 자동으로 복원합니다.
- 에이전트는 `AGENTS.md`, `CLAUDE.md`, `README.md`, `.github/copilot-instructions.md` 같은 프로젝트 지침 파일을 자동으로 읽습니다.
- 코드 산출물은 실제 프로젝트 파일로 저장될 수 있습니다.
- 저장 후에는 가능한 경우 문법 검사와 타깃 테스트를 자동 검증합니다.
- PDF 읽기, DOCX 읽기, PPTX 생성, 웹 검색 CLI를 지원합니다.
- Ollama 응답이 길어져 타임아웃이 발생하면 재시도 시 더 작은 출력과 fast model 폴백을 시도합니다.

## 주요 기능

### 1. Team-agent 오케스트레이션

아래 역할들이 조합되어 동작합니다.

- `Manager`: 작업 분석, 흐름 조율, 최종 결과 통합
- `Planner`: 작업을 단계로 나누고 실행 계획 생성
- `Researcher`: 코드베이스 분석, 관련 파일 탐색, 필요 시 인터넷 검색
- `Coder`: 구현 및 수정
- `Reviewer`: 코드 품질과 위험 검토
- `Tester`: 테스트 코드 작성, 변경 후 검증
- `DocumentAgent`: README/리포트 같은 문서 산출물 생성
- `VisionAgent`: 이미지 분석

### 2. 프로젝트 폴더 부착형 사용

이제 별도 `workspaces/...` 안에서만 작업하는 구조가 아니라, 실제 진행 중인 프로젝트 폴더 자체에 붙어서 사용할 수 있습니다.

- 현재 폴더를 기본 작업 루트로 사용
- 메타데이터는 프로젝트 내부 `.lta/` 에 저장
- 결과 파일은 실제 프로젝트 루트에 직접 반영 가능
- 프로젝트별 대화 이력 유지

### 3. 대화형 채팅 모드 (chat mode / agent mode 이중 지원)

CMD에서 Claude Code처럼 이어서 사용할 수 있습니다.

```bash
python run.py --chat              # 기본: chat 모드로 시작
python run.py --chat --mode chat  # chat 모드 명시적 지정
python run.py --chat --mode agent # agent 모드로 시작
```

#### chat 모드 vs agent 모드

| 구분 | chat 모드 (기본) | agent 모드 |
|------|-----------------|-----------|
| 동작 방식 | LLM에 직접 질문, 즉시 답변 | 전체 multi-agent 오케스트레이션 |
| 처리 속도 | 빠름 (단일 LLM 호출) | 느림 (Manager→Planner→Coder→Reviewer→Tester) |
| 적합한 요청 | 질문, 설명, 간단한 조언 | 파일 생성/수정, 코드 구현, 테스트 작성, 리팩토링 |
| 파일 변경 | 없음 | 있음 (실제 프로젝트 파일 반영) |

#### 모드 전환

세션 중에도 언제든지 전환 가능합니다.

```
/mode           현재 활성 모드 확인
/mode chat      chat 모드로 전환 (경량 직접 대화)
/mode agent     agent 모드로 전환 (전체 오케스트레이션)
```

#### chat 모드에서 escalation

chat 모드에서 요청이 파일 생성·코드 수정 등 복잡한 작업을 포함하는 경우, 시스템이 자동으로 `/mode agent` 전환을 제안합니다.

#### 공통 특징

- 최근 대화를 프로젝트별로 저장, 다음 실행 시 자동 복원
- `/quick` 로 특정 에이전트만 빠르게 호출 가능
- `/clear` 로 현재 프로젝트 대화 이력 초기화 가능

### 4. 문서 처리

지원 범위:

- 읽기: `txt`, `md`, `py`, `json`, `yaml`, `csv`, `html`, `css`, `sh`, `toml`, `pdf`, `docx`
- 생성: `pptx`, `md`, `html`, `txt`

CLI 기능:

- `read-doc`: PDF/DOCX/Markdown/Text 문서 읽기
- `make-ppt`: 문서를 PowerPoint로 변환

### 5. 인터넷 검색

CLI와 리서처 에이전트에서 웹 검색을 사용할 수 있습니다.

- 기본 검색 공급자: DuckDuckGo HTML
- 선택적 공급자: SerpAPI
- 설정: `WEB_SEARCH_PROVIDER`, `SERPAPI_API_KEY`

### 6. 로컬/백엔드 설정

현재 백엔드는 다음을 지원합니다.

- `ollama`
- `transformers`

기본 설정 파일은 [config/default.yaml](config/default.yaml) 에 있고, `.env` 가 이를 덮어씁니다.

## 동작 모드

### Attached mode

가장 추천하는 모드입니다.

- 현재 작업 폴더를 프로젝트 루트로 사용
- `.lta/` 에 메타데이터, 로그, 보고서, 채팅 이력 저장
- 코드 변경이 실제 프로젝트에 바로 반영될 수 있음

예시:

```bash
cd C:\work\my-app
lta chat
```

### Managed mode

기존 `workspaces` 기반의 별도 관리형 프로젝트 모드입니다.

- 여러 실험 프로젝트를 분리해서 저장할 때 유용
- 실제 저장 위치는 기본적으로 `./workspaces`

예시:

```bash
lta new-project "demo-api" --desc "FastAPI experiment"
lta chat --managed --workspace ./workspaces --project demo_api_20260409_120000
```

## 빠른 시작

### 1. 저장소 설치

```bash
git clone <repository_url>
cd local-team-agent
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

CLI 명령으로 어디서든 쓰고 싶다면:

```bash
pip install -e .
```

설치 후에는 `lta` 명령을 사용할 수 있습니다.

### 2. 환경 변수 설정

```bash
copy .env.example .env
```

최소 권장 설정 예시:

```env
DEFAULT_BACKEND=ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_DEFAULT_MODEL=llama3.1:8b
BACKEND_TIMEOUT=120
WEB_SEARCH_PROVIDER=duckduckgo
LOG_LEVEL=INFO
```

참고:

- [config/default.yaml](config/default.yaml) 의 기본값은 `llama3.1:8b`, `llama3.2:3b`, `llava:13b` 기준입니다.
- `.env.example` 값은 예시이므로, 실제 사용할 모델에 맞게 조정하는 편이 좋습니다.

### 3. Ollama 준비

```bash
ollama serve
ollama pull llama3.1:8b
ollama pull llama3.2:3b
ollama pull llava:13b
```

### 4. 백엔드 확인

```bash
python -m src.main check-backend
```

또는:

```bash
lta check-backend
```

## 사용법

### 가장 추천하는 사용 흐름

```bash
cd C:\path\to\your-project
lta chat
```

그 다음 같은 세션 안에서 자연어로 이어서 요청합니다.

예시:

```text
이 프로젝트 구조를 분석해줘
로그인 관련 코드와 테스트를 찾아줘
그 흐름을 유지하면서 버그를 수정해줘
수정한 파일 기준으로 검증 결과도 보여줘
README 사용법도 업데이트해줘
```

이 방식이 가장 Team-agent답게 동작합니다.

### CLI 명령 목록

현재 지원 명령:

- `run`
- `chat`
- `new-project`
- `list-projects`
- `inspect`
- `check-backend`
- `read-doc`
- `make-ppt`
- `search-web`
- `ui`

## 대화형 채팅 사용법

### 기본 시작

현재 폴더를 프로젝트로 붙여서 시작:

```bash
lta chat
```

래퍼 스크립트 사용:

```bash
python run.py --chat
```

특정 폴더를 명시해서 붙이기:

```bash
lta chat --project-dir C:\path\to\your-project
```

기존 대화를 복원하지 않고 새로 시작:

```bash
lta chat --fresh
python run.py --chat --fresh
```

managed 모드로 시작:

```bash
lta chat --managed --workspace ./workspaces --project my_project_id
```

### 채팅 중 명령

```text
/help
/multi
/history
/clear
/project
/quick coder 테스트 추가해줘
/exit
```

설명:

- `/help`: 채팅 내 명령 목록 보기
- `/multi`: 여러 줄 프롬프트 입력 모드 시작
- `/history`: 최근 대화 확인
- `/clear`: 현재 프로젝트 대화 이력 삭제
- `/project`: 현재 연결된 프로젝트 확인
- `/quick <agent> <task>`: 특정 에이전트만 빠르게 호출
- `/exit`: 종료

긴 프롬프트를 붙여넣고 싶다면 다음처럼 사용합니다.

```text
/multi
You are an expert local coding agent working on an industrial AI project.
Please review the repository and summarize the spindle load prediction flow.
/end
```

중간에 취소하려면 `/cancel` 을 입력하면 됩니다.

### 채팅 이력 동작 방식

- 프로젝트별 이력은 `.lta/logs/chat_history.jsonl` 에 저장됩니다.
- 같은 폴더에서 다시 `lta chat` 를 실행하면 최근 대화를 자동 복원합니다.
- attached mode 와 managed mode 모두 프로젝트 단위 이력을 유지합니다.

## 일회성 실행

한 번만 실행하고 결과를 받고 끝내고 싶다면 `run` 을 사용합니다.

```bash
lta run "현재 프로젝트 구조를 분석해줘"
python run.py "FastAPI 엔드포인트 테스트를 추가해줘"
python -m src.main run "README를 최신 사용법 기준으로 정리해줘"
```

### quick 모드

단일 에이전트만 빠르게 호출하고 싶을 때:

```bash
lta run "pytest 테스트를 추가해줘" --quick --agent coder
```

가능한 에이전트 예시:

- `coder`
- `researcher`
- `reviewer`
- `tester`
- `document`
- `manager`
- `planner`
- `vision`

## Attached mode 자세한 사용법

별도 옵션이 없으면 현재 폴더가 attached project 로 처리됩니다.

예시:

```bash
cd C:\work\real-project
lta run "테스트를 추가해줘"
```

이 경우:

- 실제 작업 루트는 `C:\work\real-project`
- 메타데이터 저장 위치는 `C:\work\real-project\.lta`
- 문서/로그/리포트도 `.lta` 아래에 저장
- 코드 산출물은 실제 프로젝트 파일로 저장 가능

특정 폴더를 명시할 수도 있습니다.

```bash
lta run "버그를 수정해줘" --project-dir C:\work\other-project
```

## Managed mode 자세한 사용법

관리형 프로젝트를 새로 만들고 싶을 때:

```bash
lta new-project "demo-api" --desc "FastAPI 실험용 프로젝트"
```

프로젝트 목록 보기:

```bash
lta list-projects --managed --workspace ./workspaces
```

프로젝트 상세 보기:

```bash
lta inspect demo_api_20260409_120000 --managed --workspace ./workspaces
```

기존 managed 프로젝트에 이어서 작업:

```bash
lta run "테스트를 추가해줘" --managed --workspace ./workspaces --project demo_api_20260409_120000
```

## 문서 기능 사용법

### PDF / DOCX / Markdown / Text 읽기

```bash
lta read-doc docs\spec.pdf
lta read-doc docs\proposal.docx
lta read-doc README.md
```

다른 프로젝트 기준 경로로 읽기:

```bash
lta read-doc docs\spec.pdf --project-dir C:\work\my-project
```

미리보기 길이 조정:

```bash
lta read-doc docs\spec.pdf --preview-chars 8000
```

### PPT 생성

문서 파일을 PowerPoint로 변환:

```bash
lta make-ppt docs\proposal.md outputs\proposal.pptx
lta make-ppt docs\spec.pdf outputs\spec.pptx
lta make-ppt docs\proposal.docx outputs\proposal.pptx
```

제목과 부제목 지정:

```bash
lta make-ppt docs\proposal.md outputs\proposal.pptx --title "제안서" --subtitle "2026 Q2"
```

## 인터넷 검색 사용법

CLI 검색:

```bash
lta search-web "fastapi dependency injection official docs"
```

검색 결과 수 지정:

```bash
lta search-web "ollama structured output" --max-results 10
```

리서처 에이전트도 작업 성격에 따라 웹 검색을 사용할 수 있습니다.

검색이 특히 유용한 경우:

- 최신 라이브러리 문서 확인
- 공식 문서 위치 찾기
- 최근 릴리스/변경사항 확인
- 로컬 코드베이스 외부의 참고 자료 확인

## 웹 UI 사용법

기본 실행:

```bash
lta ui
```

또는:

```bash
python run_ui.py
```

포트 지정:

```bash
lta ui --port 8502
python run_ui.py --port 8502
```

managed 모드 UI:

```bash
lta ui --managed --workspace ./workspaces
```

UI도 기본적으로 현재 폴더를 attached project 로 붙입니다.

## Python API 사용법

직접 엔진을 생성해서 쓸 수도 있습니다.

```python
from src.setup import create_engine

def on_status(agent, message):
    print(f"[{agent}] {message}")

engine = create_engine(
    project_root=r"C:\work\my-project",
    on_status_update=on_status,
)

state = engine.run(
    user_task="로그인 관련 테스트를 추가해줘",
)

print(state.final_output)
```

quick 호출 예시:

```python
result = engine.run_quick(
    "현재 프로젝트의 pytest 구조를 설명해줘",
    agent_role="researcher",
)
print(result)
```

## 프로젝트 컨텍스트는 어떻게 수집되나

attached project 또는 managed project 에 연결되면, 에이전트는 가능한 경우 다음 정보를 자동으로 읽습니다.

- `AGENTS.md`
- `CLAUDE.md`
- `.cursorrules`
- `.github/copilot-instructions.md`
- `.cursor/rules/*.mdc`
- `README.md`
- `CONTRIBUTING.md`
- `pyproject.toml`
- `package.json`
- `requirements.txt`

또한 git 저장소인 경우:

- `git status --short --branch`

채팅 모드인 경우 추가로:

- 최근 대화 이력

이 정보들은 플래닝, 파일 선택, 코드 생성, 리뷰, 테스트, 최종 응답에 공통 컨텍스트로 주입됩니다.

## 결과물과 저장 위치

### Attached mode

프로젝트 루트:

```text
your-project/
  .lta/
    .project.json
    .history.db
    task_history.db
    artifacts/
    logs/
    reports/
```

추가 설명:

- 실제 수정 대상 파일은 프로젝트 루트 안에 저장
- 메타데이터와 로그는 `.lta/` 로 분리
- 채팅 이력은 `.lta/logs/chat_history.jsonl`

### Managed mode

기본 구조:

```text
workspaces/
  project_id/
    .project.json
    .history.db
    artifacts/
    inputs/
    logs/
    reports/
```

## 자동 검증 동작

코드 산출물이 실제 프로젝트 파일로 저장된 뒤, 가능한 경우 다음 검증이 실행됩니다.

- 변경된 Python 파일 문법 검사
- 변경된 테스트 파일 대상 `pytest` 실행

검증 결과는 최종 응답과 리포트에 반영됩니다.

### 새 Python 프로젝트 생성 시 보강된 검증 흐름

이 이슈의 핵심은 LTA 자신의 엔트리포인트가 아니라, LTA가 새로 만들어 주는 Python 프로젝트가 종종 실행 가능한 구조를 갖추지 못했다는 점이었습니다.

이제 새 Python 프로젝트 생성 흐름에서는 다음 규칙을 강제합니다.

- 생성 산출물에 실행 가능한 엔트리포인트가 없으면 오케스트레이터가 `main.py` 경로를 우선 보정합니다.
- 엔트리포인트는 실제 애플리케이션 흐름에 연결되어 있어야 하며, 더미 placeholder 파일만으로는 완료 처리되지 않습니다.
- 생성 직후 `python main.py --smoke-test` 같은 실제 Python 실행을 수행합니다.
- 실행 실패 시 stdout, stderr, traceback을 수집해 코더에게 다시 전달하고 파일을 수정합니다.
- 수정된 프로젝트를 다시 저장하고 다시 실행합니다.
- 이 반복은 실행 성공 또는 명확한 치명적 실패가 확인될 때까지 계속됩니다.

즉, 이제 완료 기준은 "파일이 생겼는가"가 아니라 "새로 생성된 프로젝트가 실제로 실행되는가"입니다.

### 최종 완료 조건

새 Python 프로젝트는 아래 단계를 모두 통과해야 생성 성공으로 간주됩니다.

- 엔트리포인트 스모크 실행 성공
- 프로젝트 내 Python 파일 전체 문법 검사 성공
- 테스트가 있으면 전체 `pytest` 성공
- 테스트가 없으면 스모크 실행과 문법 검사를 최종 검증 기준으로 사용

이 검증이 실패하면 LTA는 성공으로 응답하지 않고, 실패 원인을 바탕으로 수리(repair) 루프를 계속 시도합니다.

## 설정 가이드

### 설정 우선순위

1. CLI 인수
2. 환경 변수 `.env`
3. [config/default.yaml](config/default.yaml)

### 자주 쓰는 환경 변수

```env
WORKSPACE_ROOT=./workspaces

DEFAULT_BACKEND=ollama
BACKEND_TIMEOUT=120

OLLAMA_HOST=http://localhost:11434
OLLAMA_DEFAULT_MODEL=llama3.1:8b
OLLAMA_VISION_MODEL=llava:13b

WEB_SEARCH_PROVIDER=duckduckgo
# SERPAPI_API_KEY=

LOG_LEVEL=INFO
```

### Ollama가 느리거나 타임아웃이 나는 경우

추천 조치:

- `BACKEND_TIMEOUT` 값을 늘리기
- `OLLAMA_DEFAULT_MODEL` 을 더 작은 모델로 변경
- `config/default.yaml` 의 `fast_model` 활용

현재 구현에는 다음 개선이 들어가 있습니다.

- 재시도 시 더 작은 출력 길이 시도
- 재시도 시 temperature 완화
- 가능하면 fast model 폴백
- 더 구체적인 timeout 에러 메시지

## 트러블슈팅

### Ollama 연결 실패

```bash
ollama serve
ollama list
lta check-backend
```

### 웹 검색이 실패함

가능한 원인:

- 네트워크 제한 환경
- DuckDuckGo 응답 실패
- `beautifulsoup4` 미설치

대안:

- `requirements.txt` 재설치
- `SERPAPI_API_KEY` 설정 후 SerpAPI 사용

### PPT 생성이 실패함

`python-pptx` 가 설치되어 있어야 합니다.

```bash
pip install -r requirements.txt
```

### PDF 읽기가 실패함

`PyPDF2` 가 필요합니다.

```bash
pip install -r requirements.txt
```

### 테스트가 이 환경에서 오래 멈출 때

이 저장소는 Windows 환경에서 일부 `pytest` 실행이 temp 디렉터리 이슈로 지연될 수 있습니다. 유지보수 시에는 아래 순서로 짧게 검증하는 편이 안전합니다.

```bash
venv\Scripts\python.exe -m py_compile src\main.py run.py
venv\Scripts\python.exe -m src.main --help
venv\Scripts\python.exe -m src.main chat --help
```

필요할 때만 특정 테스트 파일을 짧게 실행하는 방식을 권장합니다.

## 유지보수 포인트

이 섹션은 "어디를 보면 무엇을 고칠 수 있는지" 중심으로 정리했습니다.

### CLI와 실행 흐름

- [src/main.py](src/main.py)
  - 모든 Typer CLI 명령 정의
  - `run`, `chat`, `read-doc`, `make-ppt`, `search-web`, `ui`
  - 채팅 명령(`/help`, `/history`, `/clear`, `/project`, `/quick`) 처리
- [run.py](run.py)
  - 간단한 CMD 래퍼
  - `--chat`, `--fresh`, current-folder attach 기본 동작
- [run_ui.py](run_ui.py)
  - Streamlit UI 런처

### Team-agent 오케스트레이션

- [src/orchestration/engine.py](src/orchestration/engine.py)
  - 전체 실행 흐름의 핵심
  - 프로젝트 바인딩
  - guidance 파일 주입
  - git status 주입
  - 웹 검색 판단
  - artifact 저장
  - post-change validation 실행
  - 새 Python 프로젝트 엔트리포인트 보장
  - 실행-수정-재실행 검증 루프

수정 포인트 예시:

- 새로운 단계 추가
- 특정 태스크에서 어떤 에이전트를 먼저 쓸지 변경
- 저장 후 검증 정책 변경
- quick mode 컨텍스트 강화

### 프로젝트/세션 저장 구조

- [src/workspace/manager.py](src/workspace/manager.py)
  - attached / managed 모드 분기
  - `.lta` 저장 구조
  - `.project.json`, `.history.db`, `chat_history.jsonl`
  - guidance 파일 수집

수정 포인트 예시:

- 자동으로 읽을 프로젝트 규칙 파일 추가
- 대화 이력 저장 정책 변경
- 아티팩트 저장 위치 변경

### 에이전트 역할과 프롬프트

- [src/agents](src/agents)
  - 역할별 system prompt 와 실행 로직

특히 자주 보게 될 파일:

- `manager.py`
- `planner.py`
- `researcher.py`
- `coder.py`
- `reviewer.py`
- `tester.py`
- `document_agent.py`
- `vision_agent.py`

수정 포인트 예시:

- 특정 역할의 말투/출력 형식 수정
- researcher 의 검색 전략 수정
- tester 의 검증 리포트 강화

### 도구 모듈

- [src/tools/document.py](src/tools/document.py)
  - PDF/DOCX 읽기
  - PPT 생성
  - 보고서 생성
- [src/tools/web_search.py](src/tools/web_search.py)
  - DuckDuckGo / SerpAPI 검색
- [src/tools/shell.py](src/tools/shell.py)
  - 안전한 셸 실행
  - Python 문법 검사
  - pytest 실행
  - git status 수집
- [src/tools/filesystem.py](src/tools/filesystem.py)
  - 파일 읽기/쓰기
- [src/tools/image.py](src/tools/image.py)
  - 이미지 처리/분석

### 백엔드

- [src/setup.py](src/setup.py)
  - 엔진 생성
  - 설정 로드
  - 도구/에이전트/백엔드 wiring
- [src/backends/base.py](src/backends/base.py)
  - 재시도 정책
  - timeout fallback 로직
- [src/backends/ollama_backend.py](src/backends/ollama_backend.py)
  - Ollama 요청/응답 처리
- [src/backends/transformers_backend.py](src/backends/transformers_backend.py)
  - Transformers 백엔드

수정 포인트 예시:

- 새로운 백엔드 추가
- timeout / retry 전략 조정
- 모델별 기본 파라미터 조정

### UI

- [src/ui/app.py](src/ui/app.py)
  - Streamlit UI 본체

수정 포인트 예시:

- 프로젝트 선택 UX 개선
- 문서 미리보기 확장
- 채팅 히스토리 표시 강화

### 테스트

- [tests/test_attached_and_documents.py](tests/test_attached_and_documents.py)
  - attached mode, 문서 기능, 웹 검색, backend fallback 검증
- [tests/test_agent_effectiveness.py](tests/test_agent_effectiveness.py)
  - guidance 수집, 채팅 이력, quick mode 컨텍스트 검증

## 유지보수 체크리스트

새 기능을 추가하거나 수정할 때는 아래 순서를 권장합니다.

1. CLI 진입점이 필요한지 확인
2. attached / managed 모드 모두에서 경로가 맞는지 확인
3. 에이전트 컨텍스트에 guidance / git / chat history 가 들어가야 하는지 확인
4. 결과물이 프로젝트 파일인지, `.lta` 산출물인지 구분
5. 저장 후 검증 단계가 필요한지 확인
6. README 사용 예시를 함께 갱신
7. 최소한 `py_compile` 과 help 명령으로 스모크 체크

## 추천 개선 방향

앞으로 더 효과적인 Agent로 발전시키려면 아래를 추가 검토하면 좋습니다.

- 프로젝트별 명령 허용 정책을 더 세밀하게 설정
- 장기 메모리 요약 기능 추가
- patch/diff 중심 출력 강화
- test selection 개선
- git diff 기반 변경 요약 자동화
- UI에서 chat history 복원/검색 기능 강화

## 재시도(Retry) 전략

### 이전 동작의 문제점

변경 전에는 오케스트레이션 엔진이 스텝 실패에 매우 취약했습니다.

- **스텝 실패 시**: 딱 1번만 재시도한 뒤, 두 번째도 실패하면 즉시 `RuntimeError`를 발생시켜 전체 실행을 FAILED 상태로 종료
- **LLM 백엔드 실패 시**: `config.retry_attempts`(기본 3회) 시도 후 고정 `2**attempt`초 대기, 지터 없음, 치명적/일시적 오류 구분 없음
- **결과**: 타임아웃·서버 과부하 등 일시적 오류 한 번만 발생해도 전체 작업이 즉시 중단

### 현재 재시도 전략

`src/retry_policy.py` 모듈을 중심으로 통합된 재시도 정책이 적용됩니다.

#### 재시도 정책 파라미터

| 파라미터 | 설명 | 기본값 (백엔드) | 기본값 (스텝) |
|---|---|---|---|
| `max_attempts` | 최대 시도 횟수. 0=무제한 | 3 | 5 |
| `base_interval` | 첫 번째 재시도 대기 시간(초) | 1.0 | 2.0 |
| `backoff_factor` | 지수 백오프 배율 | 2.0 | 2.0 |
| `max_interval` | 최대 대기 시간 상한(초) | 30.0 | 60.0 |
| `jitter` | ±25% 무작위 흔들림 | True | True |
| `fatal_stop` | 치명적 오류 즉시 중단 | True | True |
| `total_timeout` | 전체 루프 타임아웃(초) | None | None |

#### 치명적 오류 처리

아래 키워드가 포함된 오류는 **재시도 없이 즉시 중단**합니다.

- `model not found` / 모델 없음
- `authentication failed` / 인증 실패
- `invalid api key`
- `permission denied`
- `invalid request`

#### 회복 가능한 오류 처리

아래 키워드가 포함된 오류는 **재시도** 대상으로 분류합니다.

- `timeout` / 타임아웃
- `500`, `502`, `503`, `504` 서버 오류
- `out of memory`, `resource exhausted`
- `connection` / 연결 오류
- `rate limit`, `too many requests`

#### 지수 백오프 공식

```
대기 시간 = min(base_interval * (backoff_factor ** attempt), max_interval)
지터 적용: 대기 시간 * uniform(0.75, 1.25)
최소 대기: 0.1초
```

### 재시도 동작 검증 방법

```bash
# 단위 테스트 실행 (LLM 없이 동작)
venv/bin/python -m pytest tests/test_retry.py -v

# 전체 테스트 실행
venv/bin/python -m pytest tests/test_basic.py tests/test_retry.py -v
```

### 재시도 설정 커스터마이징

환경 변수로 백엔드 재시도 횟수를 조절할 수 있습니다.

```env
BACKEND_RETRY_ATTEMPTS=5   # 기본 3
BACKEND_TIMEOUT=180        # 기본 120초
```

또는 `config/default.yaml`에서:

```yaml
backend:
  retry_attempts: 5
  timeout: 180
```

코드에서 직접 정책을 주입하려면:

```python
from src.retry_policy import RetryPolicy

custom_policy = RetryPolicy(
    max_attempts=10,
    base_interval=2.0,
    backoff_factor=2.0,
    max_interval=120.0,
    jitter=True,
    fatal_stop=True,
)

response = backend.generate_with_retry(request, policy=custom_policy)
```

### 알려진 제한 사항

- 현재 `total_timeout` 기본값은 `None` (무제한)으로, 이론상 무한 루프 가능성 있음. 실무에서는 `total_timeout`을 명시적으로 설정하는 것을 권장합니다.
- 오류 분류는 키워드 기반으로 동작하므로, 백엔드가 표준 오류 메시지를 반환하지 않으면 분류가 `unknown`으로 처리됩니다.

---

## 오케스트레이션 개선 이력 (v4 — 증분 저장 + 검증 기반 완료)

### 이전 오케스트레이션의 문제점

이전 버전에서는 다음과 같은 구조적 결함이 있었습니다.

1. **메모리 내 완료 문제**: 모든 태스크가 끝날 때까지 아티팩트를 메모리에만 보관하고, 전체 실행이 완료된 후에야 한꺼번에 디스크에 저장했습니다. 중간에 오류가 발생하면 그 전까지의 모든 작업이 유실되었으며, 하위 에이전트는 이전 에이전트가 생성한 파일을 참조할 수 없었습니다.

2. **가짜 완료 상태**: `phase=entrypoint` 실패 시 오류 메시지가 `phase=entrypoint` 한 줄에 그쳐 무엇이 잘못됐는지 알 수 없었습니다. 오케스트레이터는 아무 파일도 실행하지 않고도 "완료"를 선언할 수 있었습니다.

3. **에이전트 간 통신 부재**: 에이전트 간 공유 컨텍스트에는 이전 결과의 텍스트 요약만 있었고, 실제 저장된 파일 목록은 포함되지 않았습니다. 하위 에이전트는 어떤 파일이 이미 존재하는지 알 수 없어 잘못된 import 경로나 중복 파일을 생성했습니다.

4. **재시작 없음**: 검증이 실패하면 전체 실행이 FAILED 상태로 종료되고 재시도가 없었습니다.

---

### A. 태스크별 즉시 아티팩트 저장 (증분 지속성)

각 태스크가 완료되는 즉시 `_save_step_artifacts()` 메서드가 호출되어 아티팩트를 디스크에 저장합니다.

```
[단계 완료] → _prepare_generated_project_artifacts() → _save_step_artifacts()
                                                           ↓
                                                 파일이 즉시 디스크에 기록됨
                                                           ↓
                                                 state.metadata["saved_artifacts"] 레지스트리 갱신
                                                           ↓
                                               다음 에이전트 프롬프트에 파일 목록 삽입
```

- `Artifact.persisted = True` 플래그로 이미 저장된 아티팩트를 추적합니다.
- `_save_artifacts()` (일괄 저장)는 `persisted=True` 아티팩트를 건너뜁니다.
- 로그에 `[즉시 저장] AgentName → filepath` 형태로 저장 시점이 기록됩니다.

---

### E. 에이전트 간 통신

에이전트 간 통신은 두 가지 경로로 구현됩니다.

**1. 워크스페이스 파일 레지스트리** (`state.metadata["saved_artifacts"]`):
- 각 아티팩트가 저장될 때 레지스트리에 `경로 → {agent, role, task_id, saved_at}` 항목이 추가됩니다.
- `OrchestrationState.get_context_summary()`가 이 레지스트리를 읽어 모든 에이전트의 컨텍스트 요약에 삽입합니다.
- `_execute_step()` 프롬프트에도 현재 워크스페이스에 저장된 파일 목록이 명시적으로 삽입됩니다.

**2. 실패 컨텍스트 전파** (재빌드 시):
- `state.metadata["prior_failure"]`에 이전 실패 요약, 시도 로그, 발견된 엔트리포인트 정보가 기록됩니다.
- 플래너 단계에서 이 정보가 `plan_context["prior_failure_summary"]`로 주입됩니다.
- 코더 프롬프트에 `[REBUILD CONTEXT]` 섹션이 삽입됩니다.

---

### C. 최종 워크스페이스 실행 검증

모든 태스크 완료 후 `_run_python_project_validation_loop()`이 실제로 프로젝트를 실행합니다.

```
1. _discover_python_entrypoint() — 3단계 탐색:
   단계 1: main.py, app.py, cli.py, run.py 등 명시적 후보
   단계 2: __main__.py, manage.py 패턴 매칭
   단계 3: 파일 내용 스캔 (__name__ == '__main__' 패턴)

2. python {entrypoint} --smoke-test 실행

3. final_verify_python_project() — 문법 검사, 테스트 실행, 패키징 검증

4. 모든 검사 통과 시에만 COMPLETED 선언
```

---

### D. 검증 실패 시 재시작/재빌드

검증이 실패하면 다음 흐름이 시작됩니다.

```
검증 루프 내 수리 (최대 5회):
  1. _maybe_apply_common_runtime_repair() — src-layout 경로 문제 자동 수정
  2. _maybe_apply_common_packaging_repair() — pyproject.toml/entry point 불일치 수정
  3. _repair_python_project_from_validation() — 코더에게 실패 정보 전달 후 재구현 요청

전체 재빌드 사이클 (최대 2회):
  - blocking=True인 검증 실패에만 적용
  - 동일한 project_id/워크스페이스를 재사용
  - prior_failure 컨텍스트를 다음 사이클에 주입
  - 맹목적 재시도가 아니라 실패 정보 기반 재계획
```

재시작은 다음 경우에만 발동됩니다:
- `state.metadata["project_validation"]["blocking"] == True`
- `rebuild_cycle < MAX_REBUILD_CYCLES (2)`

---

### F. phase=entrypoint 실패 해결

이전에는 `Orchestration error: phase=entrypoint`가 발생하면 아무 진단 정보 없이 실패했습니다.

**개선 사항:**

1. **3단계 탐색**: 단순 파일명 매칭 → 경로 패턴 → 파일 내용 스캔 순서로 엔트리포인트를 탐색합니다.

2. **상세 진단 로그**: 탐색 실패 시 다음 정보를 출력합니다:
   ```
   엔트리포인트 탐색 실패!
     스캔한 Python 파일 (N개): [...]
     워크스페이스 전체 파일 (M개): [...]
     저장된 아티팩트: [...]
     해결 방법: 코더에게 main.py (또는 app.py)를 생성하도록 지시하세요.
   ```

3. **수리 루프 연동**: `_repair_python_project_from_validation()`이 `phase=entrypoint` 실패를 받으면 코더에게 명시적으로 `main.py`를 생성하도록 지시합니다.

4. **후보 목록 확장**: `main.py`, `app.py`, `cli.py`, `run.py`, `server.py`, `start.py` 모두를 탐색합니다.

---

### 실행 및 검증 방법

```bash
# 새 Python 프로젝트 생성 (증분 저장 + 검증 확인)
python run.py "간단한 Python CLI 계산기 앱 만들어줘" --managed

# 출력에서 확인할 것:
# [즉시 저장] Coder → calculator.py       ← 증분 저장
# [즉시 저장] Coder → requirements.txt   ← 증분 저장
# 엔트리포인트 발견 [1단계]: main.py       ← 엔트리포인트 탐색
# 명령 실행: python main.py --smoke-test  ← 실제 실행 검증
# 최종 결과  [COMPLETED]                  ← 검증 기반 완료

# 생성된 프로젝트 직접 실행
python workspaces/{project_id}/main.py --smoke-test
```

---

## 오케스트레이션 개선 이력 (v5 — 0 Python files 게이트 + 재빌드 루프 완성)

### 관찰된 실패 패턴

```
Orchestration error: phase=entrypoint
diagnosis: entrypoint not found
Scanned Python files: 0
```

워크스페이스에 파일들이 있지만 Python 구현 파일(.py)이 하나도 없는 상태에서 엔트리포인트를 탐색하다 실패하고, 최대 재빌드 사이클을 소진한 뒤 FAILED 반환.

---

### 루트 원인 분석

| # | 원인 | 설명 |
|---|------|------|
| 1 | **0 Python files에서 엔트리포인트 탐색 진행** | Python 파일이 없으면 엔트리포인트 탐색이 반드시 실패함에도 gate 없이 탐색을 시도했음 |
| 2 | **RuntimeError가 재빌드 루프를 건너뜀** | 코더 스텝이 5회 모두 실패해 RuntimeError가 발생하면 `project_validation` 미설정 → `blocking=False` → 재빌드 안 됨 |
| 3 | **블루프린트 검사 과다 거부** | `"## "`와 `"### "`을 tree_markers로 분류해, 마크다운 헤더가 있는 정상적인 코드 응답이 블루프린트로 거부됨 |
| 4 | **app.py 등을 1단계 무조건 엔트리포인트로 인정** | `app.py`, `cli.py`, `run.py`가 1단계 primary_candidates에 있어, 라이브러리 모듈인 `app.py`가 엔트리포인트로 잘못 선택됨 |
| 5 | **핸드오프 검증 없음** | 코더가 아티팩트 생성을 "완료"로 보고해도 실제 디스크 존재 여부를 확인하지 않았음 |

---

### C. 사전 검증 게이트 (pre-entrypoint gate)

`_run_python_project_validation_loop()` 에서 엔트리포인트 탐색 전에 Python 파일 존재 여부를 먼저 확인합니다.

```
[검증 루프 각 iteration]
  1. _collect_project_python_files() → python_files
  2. if not python_files:               ← [새 게이트]
       로그: "[검증 게이트] 0 Python files"
       _request_missing_python_implementation() 호출
       아티팩트 즉시 저장 후 continue
  3. _discover_python_entrypoint()      ← Python 파일이 있을 때만 실행
  4. smoke test → final verify
```

- `_request_missing_python_implementation()`: 0 Python files 전용 수정 요청 메서드
  - LLM에게 명시적으로 `main.py` + `if __name__ == '__main__':` 생성 요청
  - `--smoke-test` 인수 지원 필수 조건 포함
  - 현재 워크스페이스의 비-Python 파일을 컨텍스트로 전달

---

### D. RuntimeError → 재빌드 루프 연결 보완

```python
# _run_impl except block
except Exception as exc:
    state.status = TaskStatus.FAILED
    ...
    # 이전에는 project_validation 미설정 → blocking=False → 재빌드 안 됨
    if not state.metadata.get("project_validation"):
        state.metadata["project_validation"] = {
            "success": False,
            "blocking": True,       ← 재빌드 루프 진입 허용
            "failure_summary": str(exc)[:600],
        }
```

코더 스텝 5회 실패(RuntimeError)도 `blocking=True`로 재빌드 트리거됩니다.

---

### F. 엔트리포인트 탐색 정책 수정

**1단계(unconditional)**에서 `app.py`, `cli.py`, `run.py`를 제거했습니다.

```
1단계 (이름만으로 인정): main.py, src/main.py, __main__.py 만
2단계 (경로 패턴): /__main__.py, manage.py
3단계 (내용 스캔): if __name__ == '__main__' 패턴 — app.py 등도 여기서 발견 가능
```

- `app.py`가 실제 `if __name__ == '__main__':` 블록이 있으면 3단계에서 발견됨
- 순수 라이브러리 `app.py`는 탐색 실패 → repair loop → main.py 생성

---

### 팀-에이전트 핸드오프 검증

`_save_step_artifacts()` 에서 아티팩트 저장 직후 파일 존재 여부를 확인합니다.

```
[즉시 저장 + 핸드오프 확인] Coder → main.py (1234 bytes, lang=python)
[핸드오프 검증 실패] 저장 직후 파일이 존재하지 않음: main.py  ← 숨겨진 오류 표면화
```

---

### 블루프린트 검사 완화

`tree_markers`에서 `"## "`와 `"### "`를 제거했습니다.

- 이전: 마크다운 헤더(`## Usage`)가 있으면 blueprint 플래그 → 정상 코드 거부
- 이후: 실제 tree 문자(├── 등)와 구체적인 blueprint 용어만 검사

---

### 검증 로그에서 확인할 것 (v5)

```
[검증 게이트] Python 파일 없음 (attempt 1/3) — 코더에게 구현 파일 생성 요청
[0 Python files 수정 요청] attempt=1, artifacts=2, valid=True
[즉시 저장 + 핸드오프 확인] Coder → main.py (856 bytes, lang=python)
엔트리포인트 발견 [1단계 명시적 후보]: main.py
명령 실행: python main.py --smoke-test
최종 결과 [COMPLETED]
```

Python 파일이 없으면 게이트에서 차단하고 즉시 수정 요청 → 저장 확인 → 엔트리포인트 발견 순서로 진행합니다.

---

## 라이선스

MIT License
