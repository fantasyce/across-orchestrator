from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from across_agents_assistant.task_manager.models import Job
from across_agents_assistant.workspace_hygiene import is_workspace_noise_path
from .requirements import is_runtime_data_path_hint


API_SOURCE_EXTENSIONS = (
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".java",
    ".kt",
)
TEST_SOURCE_EXTENSIONS = (
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".swift",
    ".go",
    ".rs",
)


@dataclass
class ValidationError:
    error_type: str
    message: str
    target: Optional[str] = None


@dataclass
class ValidationReport:
    passed: bool
    errors: List[ValidationError] = field(default_factory=list)


class ContractValidator:
    def __init__(self, state: Any = None):
        self._state = state

    @staticmethod
    def _canonical_subtask_id(subtask_id: str) -> str:
        """Strip remediation suffixes to find the canonical business subtask ID."""
        return re.sub(r"-(?:fix-\d+|v\d+)$", "", subtask_id)

    def validate(self, job: Job) -> ValidationReport:
        errors: List[ValidationError] = []
        if self._state is None:
            return ValidationReport(passed=True, errors=[])

        task = self._state.get_task_by_subtask(job.subtask_id)
        if task is None:
            return ValidationReport(passed=True, errors=[])

        subtask = next((st for st in task.subtasks if st.subtask_id == job.subtask_id), None)
        project_dir = os.path.realpath(task.project_dir) if task.project_dir else None
        output_file = getattr(subtask, "output_file", None) if subtask else None

        # Phase 3: For remediation subtasks, do NOT extract declared files from the
        # noisy fix prompt — validate against the canonical contract instead.
        canonical_id = self._canonical_subtask_id(job.subtask_id)
        is_remediation = canonical_id != job.subtask_id

        if is_remediation:
            declared_files = []
            resolved_declared_files: List[str] = []
        else:
            declared_files = self._extract_declared_files(job.task_description)
            resolved_declared_files = self._resolve_declared_files(declared_files, project_dir)

        if output_file:
            resolved_output = self._resolve_candidate_path(output_file, project_dir)
            if not resolved_output or not os.path.exists(resolved_output):
                errors.append(
                    ValidationError(
                        error_type="missing_output_file",
                        message=f"Recorded output file does not exist: {output_file}",
                        target=output_file,
                    )
                )
            elif project_dir and not self._is_within_project_dir(resolved_output, project_dir):
                errors.append(
                    ValidationError(
                        error_type="output_outside_project_dir",
                        message=f"Output file is outside project_dir: {resolved_output}",
                        target=resolved_output,
                    )
                )

        for declared_path in resolved_declared_files:
            if not os.path.exists(declared_path):
                errors.append(
                    ValidationError(
                        error_type="missing_declared_deliverable",
                        message=f"Declared deliverable was not created: {declared_path}",
                        target=declared_path,
                    )
                )
            elif project_dir and not self._is_within_project_dir(declared_path, project_dir):
                errors.append(
                    ValidationError(
                        error_type="deliverable_outside_project_dir",
                        message=f"Declared deliverable is outside project_dir: {declared_path}",
                        target=declared_path,
                    )
                )

        if (
            self._looks_like_clarification_without_delivery(job.result or "")
            and not output_file
            and not resolved_declared_files
        ):
            errors.append(
                ValidationError(
                    error_type="non_delivery_clarification",
                    message="Agent output asked clarifying questions instead of delivering the requested work.",
                    target=job.subtask_id,
                )
            )

        errors.extend(
            self._validate_contract_deliverables(
                task=task,
                subtask_id=job.subtask_id,
                project_dir=project_dir,
                output_file=output_file,
                job=job,
            )
        )

        return ValidationReport(passed=not errors, errors=errors)

    def _validate_contract_deliverables(
        self,
        task: Any,
        subtask_id: str,
        project_dir: Optional[str],
        output_file: Optional[str],
        job: Job,
    ) -> List[ValidationError]:
        persistence = getattr(self._state, "_persistence", None)
        if persistence is None:
            return []

        try:
            contracts = persistence.get_task_contracts(task.task_id)
        except Exception:
            return []

        # Phase 3: For remediation subtasks, also include the canonical contract.
        # This ensures fix/reassign work is judged by the original requirements.
        contract_subtask_ids = [subtask_id]
        canonical_id = self._canonical_subtask_id(subtask_id)
        if canonical_id != subtask_id:
            contract_subtask_ids.insert(0, canonical_id)

        subtask_contracts = [
            contract
            for contract in contracts
            if contract.get("level") == "subtask"
            and contract.get("subtask_id") in contract_subtask_ids
        ]
        if not subtask_contracts:
            return []

        refs = self._collect_output_refs(job=job, output_file=output_file, project_dir=project_dir)
        errors: List[ValidationError] = []
        check_type_to_artifact_type = {
            "packaged_app_exists": "macos_app_bundle",
            "api_source_exists": "api_service_source",
            "container_config_exists": "dockerfile",
            "frontend_source_exists": "frontend_source",
            "test_suite_exists": "test_suite",
        }

        for contract in subtask_contracts:
            for deliverable in contract.get("expected_deliverables", []):
                if not deliverable.get("required", True):
                    continue
                path_hint = deliverable.get("path_hint")
                artifact_type = deliverable.get("artifact_type")
                if path_hint:
                    context_text = "\n".join(
                        str(part or "")
                        for part in (
                            contract.get("goal"),
                            deliverable.get("description"),
                            getattr(job, "task_description", ""),
                        )
                    )
                    if is_runtime_data_path_hint(context_text, path_hint):
                        continue
                    resolved = self._resolve_candidate_path(path_hint, project_dir)
                    if not resolved or not os.path.exists(resolved):
                        # Try shared safe resolver for bare basename → nested file fallback
                        from .project_acceptance import first_existing_candidate
                        resolved = first_existing_candidate(path_hint, project_dir)
                    if not resolved or not os.path.exists(resolved):
                        errors.append(
                            ValidationError(
                                error_type="missing_contract_deliverable",
                                message=f"Required contract deliverable missing: {path_hint}",
                                target=path_hint,
                            )
                        )
                    continue
                if artifact_type == "file" and not path_hint:
                    metadata = getattr(job, "result_metadata", {}) or {}
                    recorded_files = []
                    for key in ("created_files", "modified_files"):
                        recorded_files.extend(metadata.get(key, []) or [])
                    if recorded_files and not refs:
                        errors.append(
                            ValidationError(
                                error_type="missing_contract_artifact_type",
                                message="Required deliverable type not produced: file",
                                target="file",
                            )
                        )
                    continue
                if artifact_type and not self._deliverable_type_satisfied(artifact_type, refs, project_dir):
                    errors.append(
                        ValidationError(
                            error_type="missing_contract_artifact_type",
                            message=f"Required deliverable type not produced: {artifact_type}",
                            target=artifact_type,
                        )
                    )

            for check in contract.get("acceptance_checks", []):
                if not check.get("required", True):
                    continue
                artifact_type = check_type_to_artifact_type.get(check.get("check_type"))
                if artifact_type and not self._deliverable_type_satisfied(artifact_type, refs, project_dir):
                    errors.append(
                        ValidationError(
                            error_type="failed_acceptance_check",
                            message=f"Required acceptance check failed: {check.get('check_type')}",
                            target=check.get("check_type"),
                        )
                    )
        return errors

    def _collect_output_refs(
        self,
        job: Job,
        output_file: Optional[str],
        project_dir: Optional[str],
    ) -> Set[str]:
        refs: Set[str] = set()
        if output_file:
            resolved = self._resolve_candidate_path(output_file, project_dir)
            if resolved:
                refs.add(resolved)

        metadata = getattr(job, "result_metadata", {}) or {}
        for key in ("created_files", "modified_files"):
            for candidate in metadata.get(key, []) or []:
                resolved = self._resolve_candidate_path(candidate, project_dir)
                if resolved and not self._is_workspace_metadata_path(resolved, project_dir):
                    refs.add(resolved)
        return refs

    def _deliverable_type_satisfied(
        self,
        artifact_type: str,
        refs: Set[str],
        project_dir: Optional[str],
    ) -> bool:
        def has_ref(predicate) -> bool:
            if any(predicate(ref) for ref in refs):
                return True
            if not project_dir or not os.path.isdir(project_dir):
                return False
            for root, dirs, files in os.walk(project_dir):
                candidates = [os.path.join(root, name) for name in dirs + files]
                if any(predicate(path) for path in candidates):
                    return True
            return False

        if artifact_type == "macos_app_bundle":
            return has_ref(lambda path: path.endswith(".app") and os.path.exists(path))
        if artifact_type == "api_service_source":
            return has_ref(lambda path: path.endswith(API_SOURCE_EXTENSIONS) and os.path.exists(path))
        if artifact_type == "dockerfile":
            return has_ref(
                lambda path: os.path.basename(path).lower() in {"dockerfile", "containerfile"}
                and os.path.exists(path)
            )
        if artifact_type == "frontend_source":
            return has_ref(
                lambda path: path.endswith((".html", ".css", ".tsx", ".ts", ".jsx", ".js", ".vue"))
                and os.path.exists(path)
            )
        if artifact_type == "test_suite":
            return has_ref(self._looks_like_test_source_path)
        if artifact_type == "file":
            return bool(refs)
        return False

    @staticmethod
    def _looks_like_test_source_path(path: str) -> bool:
        if not os.path.exists(path):
            return False
        basename = os.path.basename(path).lower()
        if basename in {"conftest.py", "pytest.ini"}:
            return True
        if not path.endswith(TEST_SOURCE_EXTENSIONS):
            return False
        parts = {part.lower() for part in os.path.normpath(path).split(os.sep)}
        if parts & {"tests", "test", "__tests__"}:
            return True
        return (
            basename.startswith("test_")
            or basename.endswith("_test.py")
            or basename.endswith(".test.js")
            or basename.endswith(".test.mjs")
            or basename.endswith(".spec.js")
            or basename.endswith(".spec.mjs")
        )

    @staticmethod
    def _is_workspace_metadata_path(path: str, project_dir: Optional[str]) -> bool:
        resolved = os.path.realpath(path)
        if is_workspace_noise_path(resolved, project_dir):
            return True
        rel_path = os.path.relpath(resolved, os.path.realpath(project_dir)) if project_dir else resolved
        parts = set(rel_path.split(os.sep))
        metadata_dirs = {
            ".claude",
            ".codex",
            ".cursor",
            ".idea",
            ".vscode",
        }
        if parts & metadata_dirs:
            return True
        basename = os.path.basename(resolved)
        return basename in {".DS_Store"}

    @staticmethod
    def _extract_declared_files(text: str) -> List[str]:
        if not text:
            return []

        candidates: List[str] = []
        patterns = [
            r'`([^`\n]+\.\w+)`',
            r'(?:(?:file|path|create|write|written|save|saved|output|deliverable)s?(?:\s+to)?[:\s]+)([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)',
            r'((?:/Users|/tmp|/var|/etc|/home)/[^\s`]+\.\w+)',
        ]
        ignore = {"python3", "python", "json"}

        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                candidate = match.group(1).strip().rstrip(".,:;!?)]}")
                if (
                    not candidate
                    or candidate.lower() in ignore
                    or "/" not in candidate and "." not in candidate
                    or "(" in candidate
                    or ")" in candidate
                    or not ContractValidator._is_probable_path_candidate(candidate)
                ):
                    continue
                if candidate not in candidates:
                    candidates.append(candidate)
        return candidates

    @staticmethod
    def _is_probable_path_candidate(candidate: str) -> bool:
        lowered = candidate.lower()
        if lowered.startswith(("http://", "https://")):
            return False
        if "://" in candidate:
            return False
        basename = os.path.basename(candidate)
        # Absolute project directories commonly contain dot-prefixed folders such
        # as ``.across_agents``.  Those are directories, not file deliverables.
        # Keep real dotfiles with extensions like ``.env.example`` eligible.
        if basename.startswith(".") and "." not in basename[1:]:
            return False
        if "/" not in candidate and re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+){1,}", candidate):
            known_tlds = {
                "com", "cn", "net", "org", "io", "ai", "dev", "app",
                "co", "edu", "gov", "info", "me", "tech",
            }
            suffix = candidate.rsplit(".", 1)[-1].lower()
            if suffix in known_tlds:
                return False
        return True

    @staticmethod
    def _looks_like_clarification_without_delivery(text: str) -> bool:
        if not text:
            return False

        lowered = text.lower()
        question_mark_count = text.count("?")
        clarification_markers = [
            "clarifying question",
            "let me ask",
            "first question",
            "which of these",
            "best matches your intent",
            "what should",
            "which option",
            "please choose",
        ]
        delivery_markers = [
            "created",
            "updated",
            "implemented",
            "wrote",
            "saved",
            "modified",
            "completed",
            "generated",
            "file:",
            "files:",
        ]

        has_clarification = any(marker in lowered for marker in clarification_markers)
        has_delivery = any(marker in lowered for marker in delivery_markers)

        option_list = bool(re.search(r"(^|\n)\s*-\s+\*\*[A-Z]\.", text))
        return (has_clarification or question_mark_count >= 2 or option_list) and not has_delivery

    def _resolve_declared_files(self, candidates: List[str], project_dir: Optional[str]) -> List[str]:
        from .project_acceptance import first_existing_candidate

        resolved: List[str] = []
        for candidate in candidates:
            path = self._resolve_candidate_path(candidate, project_dir)
            if path and os.path.exists(path):
                if path not in resolved:
                    resolved.append(path)
                continue
            # Try safe resolver for bare basename → nested file fallback
            if path is None or not os.path.exists(path):
                safe = first_existing_candidate(candidate, project_dir)
                if safe and safe not in resolved:
                    resolved.append(safe)
        return resolved

    @staticmethod
    def _resolve_candidate_path(candidate: str, project_dir: Optional[str]) -> Optional[str]:
        if not candidate:
            return None
        if os.path.isabs(candidate):
            return os.path.realpath(candidate)
        if project_dir:
            return os.path.realpath(os.path.join(project_dir, candidate))
        return None

    @staticmethod
    def _is_within_project_dir(path: str, project_dir: str) -> bool:
        normalized_path = os.path.realpath(path)
        normalized_project_dir = os.path.realpath(project_dir)
        return (
            normalized_path == normalized_project_dir
            or normalized_path.startswith(normalized_project_dir + os.sep)
        )


def extract_routers(code_dir: str) -> Dict[str, List[str]]:
    routers: Dict[str, List[str]] = {}
    router_pattern = re.compile(r"@router\.(get|post|put|delete|patch|head|options)\([\"']([^\"']+)[\"']")
    api_router_pattern = re.compile(r"(\w+)\s*=\s*APIRouter\(")

    for root, _, files in os.walk(code_dir):
        for filename in files:
            if not filename.endswith(".py"):
                continue
            filepath = os.path.join(root, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    source = f.read()
            except Exception:
                continue

            router_names = set(api_router_pattern.findall(source))
            if not router_names:
                continue

            for match in router_pattern.finditer(source):
                method = match.group(1).upper()
                path = match.group(2)
                key = f"{method} {path}"
                routers.setdefault(key, []).append(filepath)

    return routers


def extract_model_fields(code_dir: str) -> Dict[str, Dict[str, str]]:
    models: Dict[str, Dict[str, str]] = {}

    for root, _, files in os.walk(code_dir):
        for filename in files:
            if not filename.endswith(".py"):
                continue
            filepath = os.path.join(root, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    source = f.read()
            except Exception:
                continue

            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    is_base_model = any(
                        (isinstance(base, ast.Name) and base.id == "BaseModel")
                        or (isinstance(base, ast.Attribute) and base.attr == "BaseModel")
                        for base in node.bases
                    )
                    if not is_base_model:
                        continue

                    fields: Dict[str, str] = {}
                    for item in node.body:
                        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                            field_name = item.target.id
                            type_hint = _ast_to_str(item.annotation)
                            fields[field_name] = type_hint
                        elif isinstance(item, ast.Assign):
                            for target in item.targets:
                                if isinstance(target, ast.Name):
                                    fields[target.id] = "Any"
                    models[node.name] = fields

    return models


def _ast_to_str(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.Attribute):
        return f"{_ast_to_str(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return f"{_ast_to_str(node.value)}[{_ast_to_str(node.slice)}]"
    if isinstance(node, ast.Index):
        return _ast_to_str(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{_ast_to_str(node.left)} | {_ast_to_str(node.right)}"
    if isinstance(node, ast.List):
        elements = ", ".join(_ast_to_str(e) for e in node.elts)
        return f"[{elements}]"
    if isinstance(node, ast.Tuple):
        elements = ", ".join(_ast_to_str(e) for e in node.elts)
        return f"({elements})"
    return "Any"


def validate_endpoint_coverage(spec: Dict[str, Any], code_dir: str) -> ValidationReport:
    errors: List[ValidationError] = []
    routers = extract_routers(code_dir)
    spec_paths = spec.get("paths", {})

    for path, methods in spec_paths.items():
        for method in methods.keys():
            if method.lower() in {"parameters", "servers", "summary", "description", "tags"}:
                continue
            key = f"{method.upper()} {path}"
            if key not in routers:
                errors.append(
                    ValidationError(
                        error_type="missing_endpoint",
                        message=f"Endpoint {key} is declared in spec but not found in code",
                        target=path,
                    )
                )

    return ValidationReport(passed=not errors, errors=errors)


def validate_model_fields(spec: Dict[str, Any], code_dir: str) -> ValidationReport:
    errors: List[ValidationError] = []
    models = extract_model_fields(code_dir)
    schemas = spec.get("components", {}).get("schemas", {})

    for schema_name, schema_def in schemas.items():
        required = set(schema_def.get("required", []))
        model_name = schema_name
        if model_name not in models:
            continue
        model_fields = models[model_name]
        for field_name in required:
            if field_name not in model_fields:
                errors.append(
                    ValidationError(
                        error_type="missing_field",
                        message=f"Required field '{field_name}' missing in model '{model_name}'",
                        target=f"{model_name}.{field_name}",
                    )
                )

    return ValidationReport(passed=not errors, errors=errors)


def validate_response_format(spec: Dict[str, Any], code_dir: str) -> ValidationReport:
    errors: List[ValidationError] = []
    spec_paths = spec.get("paths", {})

    for path, methods in spec_paths.items():
        for method, operation in methods.items():
            if method.lower() in {"parameters", "servers", "summary", "description", "tags"}:
                continue
            responses = operation.get("responses", {})
            for status_code, response in responses.items():
                if status_code.startswith("2"):
                    content = response.get("content", {})
                    for media_type, media_def in content.items():
                        schema = media_def.get("schema", {})
                        if schema.get("type") == "object":
                            props = set(schema.get("properties", {}).keys())
                            if "success" in props and "data" in props:
                                if not _router_returns_wrapped(code_dir, path, method):
                                    errors.append(
                                        ValidationError(
                                            error_type="response_format_mismatch",
                                            message=(
                                                f"Expected wrapped response {{success, data}} "
                                                f"for {method.upper()} {path}, but router may return bare data"
                                            ),
                                            target=f"{method.upper()} {path}",
                                        )
                                    )

    return ValidationReport(passed=not errors, errors=errors)


def _router_returns_wrapped(code_dir: str, path: str, method: str) -> bool:
    method_upper = method.upper()
    path_escaped = re.escape(path)
    decorator_pattern = re.compile(
        rf"@router\.{re.escape(method.lower())}\([\"']{path_escaped}[\"']"
    )

    for root, _, files in os.walk(code_dir):
        for filename in files:
            if not filename.endswith(".py"):
                continue
            filepath = os.path.join(root, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    source = f.read()
            except Exception:
                continue

            if not decorator_pattern.search(source):
                continue

            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    has_decorator = any(
                        (
                            isinstance(dec, ast.Attribute)
                            and dec.attr == method.lower()
                            and isinstance(dec.value, ast.Name)
                            and dec.value.id == "router"
                        )
                        or (
                            isinstance(dec, ast.Call)
                            and isinstance(dec.func, ast.Attribute)
                            and dec.func.attr == method.lower()
                            and isinstance(dec.func.value, ast.Name)
                            and dec.func.value.id == "router"
                        )
                        for dec in node.decorator_list
                    )
                    if not has_decorator:
                        continue

                    for stmt in ast.walk(node):
                        if isinstance(stmt, ast.Return):
                            if isinstance(stmt.value, ast.Dict):
                                keys = []
                                for k in stmt.value.keys:
                                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                        keys.append(k.value)
                                    elif isinstance(k, ast.Str):
                                        keys.append(k.s)
                                if "success" in keys and "data" in keys:
                                    return True
            return False
    return False


def validate_type_consistency(spec: Dict[str, Any], code_dir: str) -> ValidationReport:
    errors: List[ValidationError] = []
    models = extract_model_fields(code_dir)
    schemas = spec.get("components", {}).get("schemas", {})

    type_mapping = {
        "string": ["str", "Optional[str]", "str | None"],
        "integer": ["int", "Optional[int]", "int | None"],
        "number": ["float", "int", "Optional[float]", "Optional[int]", "float | None", "int | None"],
        "boolean": ["bool", "Optional[bool]", "bool | None"],
        "array": ["list", "List", "Optional[list]", "Optional[List]", "list | None", "List | None"],
        "object": ["dict", "Dict", "Optional[dict]", "Optional[Dict]", "dict | None", "Dict | None"],
    }

    for schema_name, schema_def in schemas.items():
        if schema_name not in models:
            continue
        model_fields = models[schema_name]
        properties = schema_def.get("properties", {})

        for field_name, field_spec in properties.items():
            if field_name not in model_fields:
                continue
            spec_type = field_spec.get("type")
            if spec_type is None:
                continue
            code_type = model_fields[field_name]
            allowed = type_mapping.get(spec_type, [])
            if code_type not in allowed:
                errors.append(
                    ValidationError(
                        error_type="type_inconsistency",
                        message=(
                            f"Field '{field_name}' in '{schema_name}' "
                            f"spec type '{spec_type}' does not match code type '{code_type}'"
                        ),
                        target=f"{schema_name}.{field_name}",
                    )
                )

    return ValidationReport(passed=not errors, errors=errors)
