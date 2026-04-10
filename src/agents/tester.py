"""
Testing and validation agent.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import tomllib
from pathlib import Path
from typing import Optional

from .base import AgentBase
from ..orchestration.messages import AgentResult, AgentRole, AgentTask, Artifact


class TesterAgent(AgentBase):
    """Writes tests, runs checks, and summarizes validation results."""

    @property
    def system_prompt(self) -> str:
        return """당신은 테스트와 검증을 담당하는 QA 엔지니어입니다.

역할:
- 단위 테스트와 엣지 케이스 테스트를 설계합니다.
- pytest 기반 테스트 코드를 작성합니다.
- 변경된 구현에 대해 빠르고 신뢰할 수 있는 검증 전략을 제안합니다.
- 실패한 테스트 출력을 분석하고 원인을 설명합니다.

원칙:
- 테스트 이름은 의도가 드러나야 합니다.
- 정상 경로와 실패 경로를 모두 다룹니다.
- 구현 세부사항보다 관찰 가능한 동작을 검증합니다.
- 설명은 한국어로 작성합니다."""

    def run(self, task: AgentTask) -> AgentResult:
        return self._timed_run(task)

    def write_tests(
        self,
        code: str,
        file_path: str,
        test_types: Optional[list[str]] = None,
    ) -> Artifact:
        test_types = test_types or ["unit", "edge_cases"]
        prompt = f"""다음 코드를 검증할 pytest 테스트 파일을 작성하세요.

대상 파일: {file_path}
테스트 유형: {", ".join(test_types)}

코드:
```python
{code[:4000]}
```

지침:
1. pytest 사용
2. 의미 있는 test_ 함수 이름 사용
3. 핵심 동작과 엣지 케이스 포함
4. 필요한 import 포함
5. 가능한 경우 fixture 사용

완전한 테스트 파일 전체를 반환하세요."""

        response = self.generate(prompt)
        code_match = re.search(r"```(?:python)?\n(.*?)```", response, re.DOTALL)
        test_code = code_match.group(1) if code_match else response

        original_name = Path(file_path).stem
        test_filename = f"test_{original_name}.py"
        return Artifact(
            name=test_filename,
            artifact_type="code",
            content=test_code.strip(),
            file_path=f"tests/{test_filename}",
            language="python",
            metadata={"test_for": file_path, "test_types": test_types},
        )

    def run_tests(self, test_path: str = "tests/") -> dict:
        if not self.shell:
            return {"error": "쉘 도구가 없습니다.", "passed": 0, "failed": 0}

        result = self.shell.run_tests(test_path)
        return self._parse_test_output(result.stdout + result.stderr, result.return_code)

    def run_python_entrypoint(
        self,
        entrypoint_path: str,
        args: Optional[list[str]] = None,
        timeout: int = 30,
        env_override: Optional[dict] = None,
    ) -> dict:
        """Execute a Python entrypoint file and capture stdout/stderr."""
        if not self.shell:
            return {
                "success": False,
                "entrypoint": entrypoint_path,
                "args": args or [],
                "stdout": "",
                "stderr": "shell tool unavailable",
                "return_code": -1,
                "command": "",
            }

        result = self.shell.run_python_file(
            entrypoint_path,
            args=args or [],
            timeout=timeout,
            env_override=env_override,
        )
        return {
            "success": result.success,
            "entrypoint": entrypoint_path,
            "args": args or [],
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.return_code,
            "command": result.command,
            "duration_ms": result.duration_ms,
            "error": result.error,
        }

    def _extract_required_cli_args(self, output: str) -> list[str]:
        match = re.search(r"the following arguments are required:\s*(.+)", output or "", re.IGNORECASE)
        if not match:
            return []
        raw_args = [item.strip() for item in match.group(1).split(",")]
        return [item for item in raw_args if item.startswith("-")]

    def _build_smoke_arg_values(self, required_args: list[str]) -> list[str]:
        values = []
        for arg in required_args:
            values.append(arg)
            lowered = arg.lower()
            if any(token in lowered for token in ["count", "num", "size", "port", "limit", "age"]):
                values.append("1")
            elif "path" in lowered or "file" in lowered or "dir" in lowered:
                values.append(".")
            else:
                values.append("smoke")
        return values

    def run_python_smoke_test(
        self,
        entrypoint_path: str,
        timeout: int = 30,
        smoke_args: Optional[list[str]] = None,
    ) -> dict:
        base_args = smoke_args or ["--smoke-test"]
        first = self.run_python_entrypoint(entrypoint_path, args=base_args, timeout=timeout)
        first["attempts"] = [{"args": base_args, "success": first.get("success", False)}]
        first["used_args"] = base_args
        if first.get("success", False):
            return first

        required_args = self._extract_required_cli_args((first.get("stderr") or "") + "\n" + (first.get("stdout") or ""))
        if not required_args:
            return first

        fallback_args = base_args + self._build_smoke_arg_values(required_args)
        second = self.run_python_entrypoint(entrypoint_path, args=fallback_args, timeout=timeout)
        second["attempts"] = first["attempts"] + [{"args": fallback_args, "success": second.get("success", False)}]
        second["used_args"] = fallback_args
        return second

    def _project_root(self) -> Path:
        if self.shell and getattr(self.shell, "workspace_root", None):
            return Path(self.shell.workspace_root).resolve()
        return Path(".").resolve()

    def _pyproject_search_roots(self, pyproject_data: dict) -> list[Path]:
        project_root = self._project_root()
        roots = [project_root]

        tool_data = pyproject_data.get("tool") or {}
        setuptools_data = tool_data.get("setuptools") or {}
        packages_data = setuptools_data.get("packages") or {}
        find_data = packages_data.get("find") or {}
        where_values = find_data.get("where") or []
        if isinstance(where_values, str):
            where_values = [where_values]

        for item in where_values:
            if not item:
                continue
            roots.append((project_root / item).resolve())

        unique_roots: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root)
            if key in seen:
                continue
            seen.add(key)
            unique_roots.append(root)
        return unique_roots

    def _resolve_module_candidate_paths(self, module_name: str, search_roots: list[Path]) -> list[Path]:
        parts = [part for part in module_name.split(".") if part]
        candidates: list[Path] = []
        for root in search_roots:
            candidates.append(root.joinpath(*parts).with_suffix(".py"))
            candidates.append(root.joinpath(*parts, "__init__.py"))
        return candidates

    def _module_defines_callable(self, module_path: Path, callable_name: str) -> bool:
        try:
            tree = ast.parse(module_path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            return False
        except OSError:
            return False

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == callable_name:
                return True
        return False

    def _find_spec_safe(self, module_name: str):
        try:
            return importlib.util.find_spec(module_name)
        except (ImportError, ModuleNotFoundError, ValueError):
            return None

    def verify_python_packaging(self, entrypoint_path: str) -> dict:
        """Validate pyproject entrypoints and packaging metadata against real files."""
        project_root = self._project_root()
        pyproject_path = project_root / "pyproject.toml"
        if not pyproject_path.exists():
            return {
                "success": True,
                "summary": "packaging skipped=no pyproject",
                "issues": [],
                "pyproject_path": "pyproject.toml",
            }

        try:
            pyproject_data = tomllib.loads(pyproject_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            return {
                "success": False,
                "summary": "packaging failed=pyproject parse error",
                "issues": [
                    {
                        "type": "pyproject_parse_error",
                        "message": f"pyproject.toml parse failed: {exc}",
                    }
                ],
                "pyproject_path": "pyproject.toml",
            }

        issues = []

        build_system = pyproject_data.get("build-system") or {}
        build_backend = (build_system.get("build-backend") or "").strip()
        if build_backend:
            backend_module = build_backend.split(":", 1)[0].strip()
            if backend_module and self._find_spec_safe(backend_module) is None:
                issues.append(
                    {
                        "type": "build_backend_missing",
                        "message": f"build backend '{build_backend}' is not importable",
                        "backend": build_backend,
                    }
                )

        scripts = (pyproject_data.get("project") or {}).get("scripts") or {}
        search_roots = self._pyproject_search_roots(pyproject_data)
        for script_name, target in scripts.items():
            if not isinstance(target, str) or ":" not in target:
                issues.append(
                    {
                        "type": "script_target_invalid",
                        "message": f"script '{script_name}' has invalid target '{target}'",
                        "script": script_name,
                        "target": target,
                    }
                )
                continue

            module_name, callable_name = [item.strip() for item in target.split(":", 1)]
            candidates = self._resolve_module_candidate_paths(module_name, search_roots)
            module_path = next((path for path in candidates if path.exists()), None)
            if not module_path:
                relative_candidates = []
                for path in candidates:
                    try:
                        relative_candidates.append(path.relative_to(project_root).as_posix())
                    except ValueError:
                        relative_candidates.append(path.as_posix())
                issues.append(
                    {
                        "type": "script_target_module_missing",
                        "message": (
                            f"script '{script_name}' points to missing module '{module_name}' "
                            f"(expected one of: {', '.join(relative_candidates)})"
                        ),
                        "script": script_name,
                        "target": target,
                        "module": module_name,
                        "callable": callable_name,
                        "expected_paths": relative_candidates,
                    }
                )
                continue

            if not self._module_defines_callable(module_path, callable_name):
                try:
                    module_label = module_path.relative_to(project_root).as_posix()
                except ValueError:
                    module_label = module_path.as_posix()
                issues.append(
                    {
                        "type": "script_target_callable_missing",
                        "message": (
                            f"script '{script_name}' points to callable '{callable_name}' "
                            f"which is not defined in '{module_label}'"
                        ),
                        "script": script_name,
                        "target": target,
                        "module": module_name,
                        "callable": callable_name,
                        "module_path": module_label,
                    }
                )

        success = not issues
        return {
            "success": success,
            "summary": "packaging=ok" if success else f"packaging failed={len(issues)}",
            "issues": issues,
            "pyproject_path": "pyproject.toml",
            "entrypoint": entrypoint_path,
        }

    def verify_changed_files(
        self,
        changed_files: list[str],
        run_targeted_tests: bool = True,
    ) -> dict:
        """
        Run lightweight verification for changed files.

        - Python files: syntax check
        - Generated or changed test files: targeted pytest
        """
        if not self.shell:
            return {
                "success": False,
                "summary": "쉘 도구가 없어 검증을 실행할 수 없습니다.",
                "syntax_checks": [],
                "test_result": None,
                "changed_files": changed_files,
            }

        syntax_checks = []
        python_files = [
            path
            for path in changed_files
            if path.endswith(".py") and not Path(path).name.startswith(".")
        ]
        for file_path in python_files[:20]:
            result = self.shell.check_python_syntax(file_path)
            syntax_checks.append(
                {
                    "file": file_path,
                    "success": result.success,
                    "stderr": (result.stderr or "").strip(),
                }
            )

        test_targets = [
            path
            for path in changed_files
            if path.endswith(".py") and (path.startswith("tests/") or Path(path).name.startswith("test_"))
        ]

        test_result = None
        if run_targeted_tests and test_targets:
            pytest_result = self.shell.run_pytest(test_targets=test_targets, verbose=False)
            test_result = self._parse_test_output(pytest_result.stdout + pytest_result.stderr, pytest_result.return_code)
            test_result["targets"] = test_targets

        syntax_failures = [item for item in syntax_checks if not item["success"]]
        tests_failed = bool(test_result) and not test_result.get("success", False)
        success = not syntax_failures and not tests_failed

        summary_parts = []
        if syntax_checks:
            summary_parts.append(
                f"Python syntax checked={len(syntax_checks)}, failures={len(syntax_failures)}"
            )
        if test_result:
            summary_parts.append(
                f"targeted pytest passed={test_result.get('passed', 0)} failed={test_result.get('failed', 0)}"
            )
        if not summary_parts:
            summary_parts.append("적용 가능한 검증 단계가 없었습니다.")

        return {
            "success": success,
            "summary": " | ".join(summary_parts),
            "syntax_checks": syntax_checks,
            "test_result": test_result,
            "changed_files": changed_files,
        }

    def final_verify_python_project(
        self,
        entrypoint_path: str,
        python_files: list[str],
        has_tests: bool,
        smoke_args: Optional[list[str]] = None,
    ) -> dict:
        """Run the final verification pass for a generated Python project."""
        smoke_result = self.run_python_smoke_test(
            entrypoint_path,
            timeout=30,
            smoke_args=smoke_args or ["--smoke-test"],
        )

        syntax_checks = []
        for file_path in python_files:
            result = self.shell.check_python_syntax(file_path) if self.shell else None
            syntax_checks.append(
                {
                    "file": file_path,
                    "success": bool(result and result.success),
                    "stderr": ((result.stderr if result else "shell tool unavailable") or "").strip(),
                }
            )

        pytest_result = None
        if has_tests and self.shell:
            result = self.shell.run_pytest(verbose=False)
            pytest_result = self._parse_test_output(result.stdout + result.stderr, result.return_code)

        syntax_failures = [item for item in syntax_checks if not item["success"]]
        tests_failed = bool(pytest_result) and not pytest_result.get("success", False)
        packaging_result = self.verify_python_packaging(entrypoint_path)
        packaging_failed = not packaging_result.get("success", False)

        # 테스트가 없으면 엔트리포인트 스모크 실행과 전체 문법 검사를 최종 기준으로 삼는다.
        success = smoke_result.get("success", False) and not syntax_failures and not tests_failed and not packaging_failed

        summary_parts = [
            f"smoke={'ok' if smoke_result.get('success') else 'failed'}",
            f"python_files={len(python_files)}",
            f"syntax_failures={len(syntax_failures)}",
            packaging_result.get("summary", "packaging=unknown"),
        ]
        if pytest_result:
            summary_parts.append(
                f"pytest passed={pytest_result.get('passed', 0)} failed={pytest_result.get('failed', 0)}"
            )
        else:
            summary_parts.append("pytest skipped=no tests")

        return {
            "success": success,
            "summary": " | ".join(summary_parts),
            "entrypoint": entrypoint_path,
            "smoke_result": smoke_result,
            "syntax_checks": syntax_checks,
            "test_result": pytest_result,
            "packaging_result": packaging_result,
            "verification_mode": "pytest" if pytest_result else "smoke",
        }

    def generate_verification_report(self, verification: dict) -> str:
        lines = [
            "## 변경 사항 검증",
            verification.get("summary", "검증 결과 없음"),
        ]

        syntax_checks = verification.get("syntax_checks") or []
        if syntax_checks:
            lines.append("")
            lines.append("### 문법 검사")
            for item in syntax_checks:
                status = "OK" if item.get("success") else "FAILED"
                detail = f" - {item['stderr']}" if item.get("stderr") else ""
                lines.append(f"- {status}: {item['file']}{detail}")

        test_result = verification.get("test_result")
        if test_result:
            lines.append("")
            lines.append("### 타깃 테스트")
            lines.append(
                f"- passed={test_result.get('passed', 0)}, failed={test_result.get('failed', 0)}, success={test_result.get('success')}"
            )
            for error in (test_result.get("errors") or [])[:10]:
                lines.append(f"- {error}")

        return "\n".join(lines)

    def analyze_test_failure(
        self,
        test_output: str,
        source_code: Optional[str] = None,
    ) -> str:
        code_str = f"\n\n테스트 코드:\n```python\n{source_code[:3000]}\n```" if source_code else ""
        prompt = f"""다음 테스트 실패를 분석하고 수정 방향을 제안하세요.

테스트 출력:
```
{test_output[:3000]}
```{code_str}

다음을 포함하세요:
1. 실패 원인
2. 문제 가능성이 높은 코드 위치
3. 수정 방향
4. 추가 확인 사항"""
        return self.generate(prompt)

    def validate_implementation(
        self,
        code: str,
        requirements: list[str],
    ) -> dict:
        req_str = "\n".join(f"{index + 1}. {item}" for index, item in enumerate(requirements))
        prompt = f"""다음 구현이 요구사항을 얼마나 충족하는지 JSON 으로 평가하세요.

요구사항:
{req_str}

구현 코드:
```python
{code[:4000]}
```

형식:
{{
  "satisfied": ["..."],
  "missing": ["..."],
  "partial": ["..."],
  "overall_score": 8,
  "notes": "..."
}}
"""

        response = self.generate(prompt)
        try:
            clean = response.strip()
            if "```" in clean:
                for part in clean.split("```"):
                    if "{" in part:
                        clean = part.lstrip("json").strip()
                        break
            return json.loads(clean)
        except json.JSONDecodeError:
            return {
                "satisfied": [],
                "missing": requirements,
                "partial": [],
                "overall_score": 5,
                "notes": response[:500],
            }

    def _parse_test_output(self, output: str, return_code: int) -> dict:
        passed = 0
        failed = 0
        errors = []

        summary_match = re.search(r"(\d+) passed(?:, (\d+) failed)?(?:, (\d+) error)?", output)
        if summary_match:
            passed = int(summary_match.group(1) or 0)
            failed = int(summary_match.group(2) or 0)

        for line in output.splitlines():
            if "FAILED" in line or "ERROR" in line:
                errors.append(line.strip())

        return {
            "passed": passed,
            "failed": failed,
            "errors": errors[:10],
            "output": output[:3000],
            "success": return_code == 0,
        }

    def generate_test_report(
        self,
        test_results: dict,
        project_name: str = "",
    ) -> str:
        passed = test_results.get("passed", 0)
        failed = test_results.get("failed", 0)
        total = passed + failed
        success_rate = (passed / total * 100) if total > 0 else 0
        status = "통과" if test_results.get("success") else "실패"

        lines = [
            f"## 테스트 결과 보고서: {status}",
            f"**프로젝트**: {project_name or '이름 없음'}",
            "",
            "### 요약",
            f"- 전체: {total}개",
            f"- 통과: {passed}개",
            f"- 실패: {failed}개",
            f"- 성공률: {success_rate:.1f}%",
        ]
        if test_results.get("errors"):
            lines.extend(["", "### 실패 목록"])
            for error in test_results["errors"][:10]:
                lines.append(f"- {error}")
        return "\n".join(lines)
