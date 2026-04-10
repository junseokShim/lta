제공해주신 코드를 바탕으로, 전문적이고 명확한 `README.md` 파일을 작성하였습니다.

---

# 👋 Python Greeting CLI

![Python Version](https://img.shields.io/badge/python-3.7%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

사용자로부터 이름을 입력받아 맞춤형 인사말을 출력하는 간단하고 실행 가능한 **Python CLI(Command Line Interface) 도구**입니다. 이 프로젝트는 명령행 인자(Argument) 처리와 대화형 입력 모드를 모두 지원하여 유연한 사용성을 제공합니다.

## 🎯 프로젝트 소개 및 목적

이 프로젝트의 목적은 파이으로 작성된 CLI 애플리케이션의 기본 구조를 보여주는 것입니다. `argparse` 라이브러리를 활용하여 명령행 인자를 처리하고, 입력값이 없을 경우 사용자에게 직접 입력을 요구하는 인터랙티브한 경험을 제공합니다. 또한, 프로그램의 무결성을 검증할 수 있는 **Smoke Test** 기능을 포함하고 있습니다.

## ✨ 주요 기능 (Features)

- 👤 **맞춤형 인사**: 입력된 이름을 바탕으로 친근한 인사말을 생성합니다.
- ⌨️ **두 가지 실행 모드**:
    - **Argument 모드**: `--name` 인자를 사용하여 즉시 이름을 전달할 수 있습니다.
    - **Interactive 모드**: 인자가 없을 경우, 프로그램이 실행된 후 사용자에게 이름을 직접 입력받습니다.
- 🧪 **Smoke Test 지원**: `--smoke-test` 플래그를 통해 핵심 로직(`greet` 함수)이 정상적으로 작동하는지 즉시 확인할 수 있습니다.
- 🛠️ **견고한 예외 처리**: 이름이 비어있거나 공백만 있는 경우를 대비한 기본값 처리가 포함되어 있습니다.

## 🚀 설치 방법 (Installation)

이 프로젝트는 Python 환경만 있으면 별도의 외부 라이브러리 설치 없이 바로 실행 가능합니다.

1. **저장소 복제 (Clone the repository)**
   ```bash
   git clone https://github.com/your-username/greeting-cli.git
   cd greeting-cli
   ```

2. **(선치 사항) 가상 환경 생성 및 활성화**
   ```bash
   python -m venv venv
   # Windows
   source venv/Scripts/activate
   # macOS/Linux
   source venv/bin/activate
   ```

## 📖 사용 방법 (Usage)

### 1. 명령행 인자를 사용하는 방법
`--name` 옵션 뒤에 인사할 이름을 입력합니다.
```bash
python main.py --name "홍길동"
# 출력: 안녕하세요, 홍글동님! 만나서 반갑습니다.
```

### 2. 대화형 모드로 사용하는 방법
인자 없이 실행하면 프로그램이 실행된 후 이름을 입력받을 수 있는 프롬프트가 나타납니다.
```bash
python main.py
# 실행 후 프롬프트에 이름 입력
# 출력: 안녕하세요, [입력한 이름]님! 만나서 반갑습니다.
```

### 3. 기능 검증 (Smoke Test)
프로그램의 핵심 로직이 정상적으로 동작하는지 테스트 모드를 실행합니다.
```bash
python main.py --smoke-test
# 출력:
# Running smoke test...
# Smoke test passed successfully!
```

## 📂 프로젝트 구조 (Project Structure)

```text
.
├── main.py          # 핵심 로직(greet) 및 CLI 실행 엔진(run_cli) 포함
├── __init__.py      # 패키지 초기화 및 run_cli 인터페이스 노출
└── README.md        # 프로젝트 문서
```

## 🤝 기여 방법 (Contributing)

버그 수정이나 새로운 기능 제안은 언제나 환영합니다! 
1. 이 저장소를 **Fork** 합니다.
2. 새로운 기능 브랜치를 생성합니다 (`git checkout -b feature/AmazingFeature`).
3. 변경 사항을 **Commit** 합니다 (`git commit -m 'Add some AmazingFeature'`).
4. 브랜치에 **Push** 합니다 (`git push origin feature/AmazingFeature`).
5. 해당 저장소에 **Pull Request**를 생성합니다.

## 📄 라이선스 (License)

이 프로젝트는 **MIT License**를 따릅니다. 자세한 내용은 `LICENSE` 파일을 참고하세요.

---
*Created with ❤️ by a Python Developer.*