from __future__ import annotations

import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .requirements import (
    canonical_requirement_key,
    extract_forbidden_path_hints,
    extract_required_path_hints,
    has_negative_container_constraint,
)
from .quality_gates import ProbeAdapterRegistry

ALLOWED_DELIVERY_TASK_TYPES = {"functional", "artifact"}


def normalize_delivery_task_types(task_types: List[str]) -> Tuple[List[str], str]:
    values: List[str] = []
    for item in task_types or []:
        value = str(item).strip().lower()
        if value not in ALLOWED_DELIVERY_TASK_TYPES:
            raise ValueError(f"Unsupported task type: {value}")
        if value not in values:
            values.append(value)
    if not values:
        raise ValueError("At least one task type must be selected")
    return values, values[0] if len(values) == 1 else "composite"


def _contains_no_docker_constraint(description: str) -> bool:
    return has_negative_container_constraint(description)


def _extract_required_agent_mix(description: str) -> Dict[str, int]:
    text = description or ""
    lowered = text.lower()
    if "required agent execution mix" not in lowered and "actual agent mix" not in lowered:
        return {}

    def value(patterns: List[str], default: int) -> int:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                try:
                    return max(0, int(match.group(1)))
                except (TypeError, ValueError):
                    return default
        return default

    return {
        "min_distinct_agents": value([
            r"At least\s+(\d+)\s+distinct\s+non-owner\s+agents?",
            r"min[_ -]?distinct[_ -]?agents?\s*[:=]\s*(\d+)",
        ], 2),
        "min_local_agents": value([
            r"At least\s+(\d+)\s+local\s+agents?",
            r"min[_ -]?local[_ -]?agents?\s*[:=]\s*(\d+)",
        ], 1),
        "min_cloud_agents": value([
            r"At least\s+(\d+)\s+cloud\s+LLMs?",
            r"At least\s+(\d+)\s+cloud\s+agents?",
            r"min[_ -]?cloud[_ -]?agents?\s*[:=]\s*(\d+)",
        ], 1),
    }


def _forbidden_file_scope(description: str, path_hint: str) -> str:
    """Infer whether a forbidden file applies only at project root.

    Bare filenames are recursive by default, but phrases such as
    ``root __init__.py`` or ``project root setup.py`` mean only the root-level
    helper file is forbidden.  Nested package/test markers should remain valid
    unless the user explicitly says no such file anywhere.
    """
    normalized = (path_hint or "").strip().replace("\\", "/").strip("/")
    if not normalized or "/" in normalized:
        return "exact"

    lowered = (description or "").lower()
    escaped = re.escape(normalized.lower())
    root_phrase = r"(?:root|root-level|root level|project root|repository root|repo root)"
    anywhere_phrase = r"(?:anywhere|any|all|recursive|every)"
    if re.search(anywhere_phrase + r".{0,40}" + escaped, lowered):
        return "recursive"
    if re.search(root_phrase + r".{0,40}" + escaped, lowered) or re.search(
        escaped + r".{0,40}" + root_phrase,
        lowered,
    ):
        return "project_root"
    if normalized.lower() in {"run.py", "runner.py", "run_tests.py", "setup_test_env.py"}:
        return "project_root"
    return "recursive"


def _requires_exact_file_set(description: str) -> bool:
    lowered = (description or "").lower()
    return bool(
        re.search(r"\b(exactly|only|single)\b.{0,80}\b(file|files|deliverable|deliverables)\b", lowered)
        or re.search(r"\b(file|files|deliverable|deliverables)\b.{0,80}\b(exactly|only|single)\b", lowered)
        or re.search(r"\bdo\s+not\b.{0,60}\b(any\s+)?other\s+files?\b", lowered)
    )


def _extract_allowed_documentation_files(description: str) -> List[str]:
    """Return explicit documentation files when the user limits docs to them."""
    text = description or ""
    lowered = text.lower()
    doc_scope_limited = bool(
        re.search(r"\b(docs?|documentation)\b.{0,80}\b(only|just|limited to)\b", lowered)
        or re.search(r"\b(only|just|limited to)\b.{0,80}\b(docs?|documentation)\b", lowered)
        or re.search(r"(文档|说明文档).{0,40}(只需要|仅需要|只需|仅需|只保留|只能|不要生成大量|不要(增加|新增|创建|生成).{0,10}额外)", text)
        or re.search(r"除.{0,80}外.{0,20}不要(增加|新增|创建|生成).{0,10}(额外|其他|其它).{0,10}(文档|说明文档)", text)
        or re.search(r"(额外|其他|其它).{0,10}(文档|说明文档).{0,20}(不要|禁止)", text)
        or _requires_exact_file_set(text)
    )
    if not doc_scope_limited:
        return []
    docs = [
        path for path in extract_required_path_hints(text)
        if str(path).lower().endswith((".md", ".rst", ".txt"))
    ]
    for match in re.findall(r"[\w./-]+\.(?:md|rst|txt)\b", text, flags=re.IGNORECASE):
        docs.append(match)
    seen = set()
    result: List[str] = []
    for path in docs:
        key = canonical_requirement_key(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _extract_forbidden_runner_script_files(description: str) -> List[str]:
    """Infer root helper scripts forbidden by a negative runner/test-script clause."""
    text = description or ""
    clauses = re.split(r"(?<=[;!?。；！？，])\s*|(?<=[.])\s+|[\n\r]+", text)
    negative = (
        r"(?:\b(?:do\s+not|don't|dont|without|avoid|forbid(?:den)?|prohibit(?:ed)?|"
        r"disallow(?:ed)?|must\s+not|should\s+not|no)\b|不要|不得|禁止|禁用|不能|不可|不允许|避免)"
    )
    runner_terms = (
        r"(?:\b(?:runner|run_tests?|test\s+runner|diagnostic\s+scripts?|temporary\s+diagnostic|"
        r"setup_test_env)\b|runner\s*脚本|run_tests\s*脚本|setup_test_env\s*脚本|诊断脚本|临时诊断脚本)"
    )
    forbidden: List[str] = []
    for clause in clauses:
        lowered = clause.lower()
        if re.search(negative, lowered) and re.search(runner_terms, lowered):
            forbidden.extend([
                "run.py",
                "runner.py",
                "run_tests.py",
                "setup_test_env.py",
            ])
            break
    seen = set()
    result: List[str] = []
    for path in forbidden:
        key = canonical_requirement_key(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _explicitly_requests_auth(description: str) -> bool:
    text = description or ""
    lowered = text.lower()
    negative_patterns = (
        r"\b(no|without)\b.{0,60}\b(auth|authentication|authorization|login|sign[- ]?in|user\s+accounts?|accounts?|password|jwt|oauth)\b",
        r"\b(do\s+not|don't|dont|must\s+not|should\s+not)\b.{0,80}\b(auth|authentication|authorization|login|sign[- ]?in|user\s+accounts?|accounts?|password|jwt|oauth)\b",
        r"(不得|不要|无需|不需要|禁止).{0,40}(登录|认证|鉴权|用户|账号|密码|jwt|oauth)",
    )
    if any(re.search(pattern, lowered if not pattern.startswith("(") else text, re.IGNORECASE) for pattern in negative_patterns):
        return False
    positive_patterns = (
        r"\b(auth|authentication|authorization|login|sign[- ]?in|sign[- ]?up|register|user\s+accounts?|account\s+login|password|jwt|oauth)\b",
        r"(登录|认证|鉴权|用户系统|账号|密码|注册|权限|角色)",
    )
    return any(re.search(pattern, lowered if not pattern.startswith("(") else text, re.IGNORECASE) for pattern in positive_patterns)


def _has_negative_auth_constraint(description: str) -> bool:
    text = description or ""
    lowered = text.lower()
    patterns = (
        r"\b(no|without)\b.{0,80}\b(auth|authentication|authorization|login|sign[- ]?in|user\s+accounts?|accounts?|password|jwt|oauth)\b",
        r"\b(do\s+not|don't|dont|must\s+not|should\s+not)\b.{0,100}\b(auth|authentication|authorization|login|sign[- ]?in|user\s+accounts?|accounts?|password|jwt|oauth)\b",
        r"(不得|不要|无需|不需要|禁止).{0,60}(登录|认证|鉴权|用户|账号|密码|jwt|oauth)",
    )
    return any(re.search(pattern, lowered if not pattern.startswith("(") else text, re.IGNORECASE) for pattern in patterns)


def _is_negated_capability(description: str, keyword: str) -> bool:
    lowered = description.lower()
    escaped = re.escape(keyword)
    return bool(
        re.search(rf"\b(no|without)\b[^.;\n]{{0,80}}\b{escaped}\b", lowered)
        or re.search(rf"\b(do\s+not|don't|dont|must\s+not|should\s+not)\b[^.;\n]{{0,100}}\b{escaped}\b", lowered)
        or re.search(rf"\b{escaped}\b[^.;\n]{{0,60}}\b(not|required\s+false|optional\s+only)\b", lowered)
    )


def _has_positive_capability(description: str, keyword: str) -> bool:
    lowered = description.lower()
    escaped = re.escape(keyword)
    if _is_negated_capability(description, keyword):
        return False
    if keyword == "complete":
        patterns = [
            rf"\b(support|supports|command|commands|feature|features|can|must|allow|allows|enable|enables)\b[^.;\n]{{0,80}}\b{escaped}\b",
            rf"\b{escaped}\b[^.;\n]{{0,40}}\b(todo|todos|task|tasks|item|items|note|notes|command|commands)\b",
        ]
        return any(re.search(pattern, lowered) for pattern in patterns)
    if keyword in {"add", "list", "duplicate"}:
        patterns = [
            rf"\b(support|supports|command|commands|feature|features|can|must|allow|allows|enable|enables)\b[^.;\n]{{0,80}}\b{escaped}\b",
            rf"\b{escaped}\b[^.;\n]{{0,40}}\b(command|commands|todo|todos|task|tasks|item|items|note|notes|id|ids)\b",
        ]
        return any(re.search(pattern, lowered) for pattern in patterns)
    if keyword == "persistence":
        return bool(re.search(r"\b(persist|persists|persistent|persistence|store|stores|storage|save|saves)\b", lowered))
    if keyword == "api":
        return _has_non_negated_pattern(
            description,
            r"\b(api|rest\s*api|api\s+service|endpoint|endpoints|backend\s+service)\b",
        )
    if keyword == "cli":
        return _has_non_negated_pattern(description, r"\b(cli|command[- ]line)\b")
    return bool(re.search(rf"\b{escaped}\b", lowered))


def _has_negative_mention(lowered: str, pattern: str, *, window: int = 80) -> bool:
    negative = r"\b(?:do\s+not|don't|dont|no|without|avoid|must\s+not|should\s+not|forbid(?:den)?|disallow(?:ed)?)\b"
    return bool(re.search(negative + rf"[^.;\n]{{0,{window}}}" + pattern, lowered))


def _split_semantic_clauses(description: str) -> List[str]:
    return [
        clause.strip()
        for clause in re.split(r"(?<=[.!?])\s+|[;\n\r]+|[。；！？]", description or "")
        if clause.strip()
    ]


def _is_negative_clause(clause: str) -> bool:
    lowered = (clause or "").lower()
    return bool(
        re.search(
            r"\b(?:do\s+not|don't|dont|no|without|avoid|must\s+not|should\s+not|"
            r"forbid(?:den)?|disallow(?:ed)?|exclude|not\s+required)\b",
            lowered,
        )
        or re.search(r"(不要|不得|禁止|无需|不需要|不能|不可|避免)", clause or "")
    )


def _has_non_negated_pattern(description: str, pattern: str) -> bool:
    for clause in _split_semantic_clauses(description):
        if re.search(pattern, clause.lower(), re.IGNORECASE) and not _is_negative_clause(clause):
            return True
    return False


def _mentions_packaged_macos_app(description: str) -> bool:
    """Return True only for an actual desktop app/bundle requirement.

    Phrases such as "macOS aesthetic" or "macOS productivity app style" are
    visual direction for a web UI, not a requirement to produce a .app bundle.
    """
    for clause in _split_semantic_clauses(description):
        if _is_negative_clause(clause):
            continue
        lowered = clause.lower()
        if re.search(r"\.app\b", lowered):
            return True
        if re.search(r"\b(swiftui|appkit)\b", lowered):
            return True
        if re.search(r"\b(packaged|bundle|bundled|notarized|signed)\b[^.;\n]{0,80}\b(macos|ios|desktop)?[^.;\n]{0,40}\b(app|application|bundle)\b", lowered):
            return True
        if re.search(
            r"\b(macos|ios|desktop)\s+(app|application|bundle)\b(?!\s+(aesthetic|style|look|feel|visual|inspired))",
            lowered,
        ):
            return True
    return False


def _mentions_api_service(description: str) -> bool:
    positive_api_pattern = r"\b(rest\s*api|api\s+service|backend|fastapi|flask|django|endpoint|endpoints|controller|handler|server)\b"
    if _has_non_negated_pattern(description, positive_api_pattern):
        return True
    route_pattern = (
        r"\b(api|http|backend|endpoint|endpoints)\b[^.;\n]{0,40}\broutes?\b"
        r"|\broutes?\b[^.;\n]{0,40}\b(api|http|backend|endpoint|endpoints)\b"
    )
    return _has_non_negated_pattern(description, route_pattern)


def _mentions_test_suite(description: str) -> bool:
    test_pattern = (
        r"\b(pytest|test\s+suite|unit\s+tests?|integration\s+tests?|e2e\s+tests?|automated\s+tests?)\b"
        r"|\b(write|create|implement|add|build|run)\b[^.;\n]{0,80}\btests?\b"
    )
    return _has_non_negated_pattern(description, test_pattern)


def _mentions_node_web(description: str, path_hints: set[str]) -> bool:
    lowered = (description or "").lower()
    if re.search(r"\b(react|next\.js|vue|angular|vite|npm)\b", lowered):
        return True
    package_pattern = r"\bpackage\.json\b"
    if "package.json" in path_hints:
        return True
    if _has_negative_mention(lowered, package_pattern):
        return False
    return _has_non_negated_pattern(
        description,
        r"\b(create|include|write|add|use|with|requires?)\b[^.;\n]{0,60}\bpackage\.json\b",
    )


def _delivery_path_hints(manifest: Dict[str, Any]) -> set[str]:
    return {
        str(item.get("path_hint") or "").lower()
        for item in (manifest or {}).get("deliverables", []) or []
        if item.get("path_hint")
    }


def _without_absolute_paths(description: str) -> str:
    """Remove workspace paths so temp directory names do not imply a stack."""
    text = description or ""
    text = re.sub(r"(?<!\w)/(?:[^\s,;]+)", " ", text)
    text = re.sub(r"\b[A-Za-z]:\\[^\s,;]+", " ", text)
    return text


def _has_python_delivery_signal(description: str, manifest: Dict[str, Any]) -> bool:
    lowered = _without_absolute_paths(description).lower()
    path_hints = _delivery_path_hints(manifest)
    return any(path.endswith(".py") for path in path_hints) or bool(
        re.search(r"\b(python|fastapi|starlette|asgi|uvicorn|pytest)\b", lowered)
    )


def _has_static_web_delivery_signal(description: str, manifest: Dict[str, Any]) -> bool:
    lowered = (description or "").lower()
    path_hints = _delivery_path_hints(manifest)
    return bool(
        any(path.endswith((".html", ".css", ".js")) for path in path_hints)
        or re.search(
            r"\b(static\s+web|web\s+page|index\.html|html|css|javascript|vanilla\s+js|file://|browser)\b",
            lowered,
        )
    )


def _requires_static_web_smoke(description: str, manifest: Dict[str, Any]) -> bool:
    lowered = (description or "").lower()
    path_hints = _delivery_path_hints(manifest)
    has_html_entry = any(path.endswith(".html") for path in path_hints) or bool(
        re.search(r"\b(index\.html|html|web\s+page|static\s+web|file://)\b", lowered)
    )
    has_frontend_behavior = any(path.endswith((".css", ".js")) for path in path_hints) or bool(
        re.search(r"\b(css|javascript|localstorage|local\s+storage|browser|responsive|frontend|front-end)\b", lowered)
    )
    direct_static_run = bool(
        re.search(r"\b(open|load|run|runnable)\b.{0,50}\bindex\.html\b", lowered)
        or "file://" in lowered
        or "static web" in lowered
        or "web page only" in lowered
    )
    return bool(has_html_entry and (has_frontend_behavior or direct_static_run))


def _requires_browser_e2e_probe(description: str, manifest: Dict[str, Any]) -> bool:
    if not _requires_static_web_smoke(description, manifest):
        return False
    lowered = (description or "").lower()
    path_hints = _delivery_path_hints(manifest)
    has_script = any(path.endswith((".js", ".ts", ".tsx", ".jsx")) for path in path_hints)
    has_user_path = bool(
        re.search(
            r"\b(canvas|animation|interactive|click|button|toggle|tab|radio|checkbox|checklist|"
            r"localstorage|local\s+storage|route\s+evidence|recompute|form|textarea|modal|"
            r"keyboard|responsive|drag|drop)\b",
            lowered,
        )
    )
    return bool(has_script or has_user_path)


def _extract_capabilities_with_diagnostics(
    description: str,
    manifest: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    lowered = description.lower()
    manifest = manifest or {"deliverables": []}
    static_web_only = _has_static_web_delivery_signal(description, manifest) and not _has_python_delivery_signal(description, manifest)
    default_probe_ids = ["probe-static-web-smoke"] if static_web_only else ["probe-pytest"]
    frontend_probe_ids = ["probe-static-web-smoke"] if static_web_only else ["probe-python-web-smoke"]
    functional_probe_ids = (
        ["probe-static-web-smoke"]
        if static_web_only
        else ["probe-python-web-smoke", "probe-pytest"]
    )
    candidates = [
        ("add", "User can add items"),
        ("list", "User can list items"),
        ("complete", "User can complete items"),
        ("persistence", "Data persists locally across runs"),
        ("duplicate", "Duplicate identifiers are rejected"),
        ("api", "API behavior is available"),
        ("cli", "Command-line behavior is available"),
    ]
    capabilities: List[Dict[str, Any]] = []
    included: List[str] = []
    excluded: List[str] = []

    def add_capability(
        capability_id: str,
        text: str,
        *,
        probes: Optional[List[str]] = None,
        source: str = "owner_inferred",
    ) -> None:
        if any(item["id"] == capability_id for item in capabilities):
            return
        capabilities.append({
            "id": capability_id,
            "description": text,
            "required": True,
            "criticality": "core",
            "source": source,
            "minimum_evidence": "L2",
            "acceptance_probe_ids": probes or default_probe_ids,
        })
        included.append(capability_id)

    for keyword, text in candidates:
        capability_id = f"cap-{keyword}"
        if _has_positive_capability(description, keyword):
            add_capability(capability_id, text)
        elif keyword in lowered or _is_negated_capability(description, keyword):
            excluded.append(capability_id)

    expense_domain = bool(re.search(r"\b(expense|expenses|receipt|receipts)\b", lowered) or re.search(r"(支出|费用|票据|收据)", description))
    mentions_crud = bool(re.search(r"\bcrud\b", lowered))
    if expense_domain and (mentions_crud or re.search(r"\b(create|add|new)\b.{0,60}\bexpense", lowered) or re.search(r"(新增|创建|添加).{0,20}(支出|费用)", description)):
        add_capability("cap-expense-create", "User can create an expense record.", probes=functional_probe_ids)
    if expense_domain and (mentions_crud or re.search(r"\b(list|view|show)\b.{0,60}\bexpense", lowered) or re.search(r"(列表|展示|查看|最近).{0,20}(支出|费用)|(支出|费用).{0,20}(列表|展示|查看)", description)):
        add_capability("cap-expense-list", "User can list expense records.", probes=functional_probe_ids)
    if expense_domain and (mentions_crud or re.search(r"\b(update|edit)\b.{0,60}\bexpense", lowered) or re.search(r"(编辑|修改|更新).{0,20}(支出|费用)", description)):
        add_capability("cap-expense-update", "User can update an existing expense record.", probes=functional_probe_ids)
    if expense_domain and (mentions_crud or re.search(r"\b(delete|remove)\b.{0,60}\bexpense", lowered) or re.search(r"(删除|移除).{0,20}(支出|费用)", description)):
        add_capability("cap-expense-delete", "User can delete an expense record.", probes=functional_probe_ids)
    if expense_domain and (re.search(r"\bcsv\b.{0,60}\b(import|upload)|\b(import|upload)\b.{0,60}\bcsv\b", lowered) or re.search(r"csv.{0,20}(导入|上传)|(导入|上传).{0,20}csv", lowered)):
        add_capability("cap-csv-import", "User can import expenses from CSV data.", probes=functional_probe_ids)
    if re.search(r"\breceipts?\b.{0,80}\b(upload|attach|add)|\b(upload|attach|add)\b.{0,80}\breceipts?\b", lowered) or re.search(r"(票据|收据).{0,30}(上传|关联|添加)|(上传|关联|添加).{0,30}(票据|收据)", description):
        add_capability("cap-receipt-upload", "User can upload or attach receipt files.", probes=functional_probe_ids)
    if expense_domain and (re.search(r"\bfilter", lowered) or re.search(r"(筛选|过滤|按月份|按分类|按商户)", description)):
        if "month" in lowered or "monthly" in lowered or "月份" in description or "月度" in description:
            add_capability("cap-filter-by-month", "User can filter expenses by month.", probes=functional_probe_ids)
        if "category" in lowered or "分类" in description:
            add_capability("cap-filter-by-category", "User can filter expenses by category.", probes=functional_probe_ids)
        if "merchant" in lowered or "商户" in description:
            add_capability("cap-filter-by-merchant", "User can filter expenses by merchant.", probes=functional_probe_ids)
    if re.search(r"\bdashboard\b", lowered) or re.search(r"(仪表盘|汇总|总金额|统计)", description):
        add_capability("cap-dashboard-summary", "User can view dashboard summary metrics.", probes=functional_probe_ids)
        if "category" in lowered or "分类" in description:
            add_capability("cap-dashboard-category-breakdown", "User can view dashboard category breakdowns.", probes=functional_probe_ids)
    if re.search(r"\b(frontend|front-end|html|css|javascript|web\s+ui|browser)\b", lowered):
        add_capability("cap-frontend-loads", "User can load the frontend in a browser.", probes=frontend_probe_ids)
        if expense_domain and mentions_crud:
            add_capability("cap-frontend-create-expense", "User can create expenses through the frontend.", probes=frontend_probe_ids)
            add_capability("cap-frontend-edit-expense", "User can edit expenses through the frontend.", probes=frontend_probe_ids)
            add_capability("cap-frontend-delete-expense", "User can delete expenses through the frontend.", probes=frontend_probe_ids)
        if expense_domain and re.search(r"\bfilter", lowered):
            add_capability("cap-frontend-filter-expenses", "User can filter expenses through the frontend.", probes=frontend_probe_ids)
    if re.search(r"\b(sqlite|local\s+(?:storage|database)|persist|persistence)\b", lowered):
        add_capability("cap-local-persistence", "Data persists in local storage across app runs.", probes=functional_probe_ids)
    if _has_negative_auth_constraint(description):
        add_capability("cap-no-auth", "Application has no login, authentication, users, passwords, JWT, or OAuth flow.", probes=functional_probe_ids)
    if _extract_allowed_documentation_files(description):
        add_capability("cap-docs-limited", "Documentation is limited to the explicitly requested files.", probes=default_probe_ids, source="explicit_user_request")

    return capabilities, {
        "included_capability_ids": included,
        "excluded_capability_ids": excluded,
        "extractor": "heuristic_v2",
    }


def _extract_capabilities(description: str, manifest: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    capabilities, _diagnostics = _extract_capabilities_with_diagnostics(description, manifest)
    return capabilities


def _requires_python_web_smoke(description: str, manifest: Dict[str, Any]) -> Tuple[bool, bool]:
    """Infer whether a Python web delivery needs a runtime smoke probe.

    The second return value means ``GET /`` should serve HTML, which is a
    stronger requirement for web apps with an explicit frontend deliverable.
    """
    lowered = description.lower()
    deliverables = manifest.get("deliverables", []) or []
    path_hints = _delivery_path_hints(manifest)
    artifact_types = {str(item.get("artifact_type") or "").lower() for item in deliverables}
    mentions_python_web = bool(re.search(r"\b(fastapi|starlette|asgi|uvicorn|web\s*app|webapp)\b", lowered))
    has_python = _has_python_delivery_signal(description, manifest)
    has_frontend = (
        "frontend_source" in artifact_types
        or any(path.endswith((".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".vue")) for path in path_hints)
        or bool(re.search(r"\b(frontend|front-end|html|web\s+ui|browser|single[- ]page|dashboard)\b", lowered))
    )
    return bool(has_python and mentions_python_web), bool(has_frontend)


def _infer_delivery_facets(description: str, task_types: List[str], manifest: Dict[str, Any]) -> List[str]:
    lowered = description.lower()
    deliverables = manifest.get("deliverables", []) or []
    artifact_types = {str(item.get("artifact_type") or "").lower() for item in deliverables}
    path_hints = {str(item.get("path_hint") or "").lower() for item in deliverables}
    facets: List[str] = []

    def add(facet: str) -> None:
        if facet not in facets:
            facets.append(facet)

    if "functional" not in task_types:
        if any(path.endswith((".md", ".rst", ".txt")) for path in path_hints) or "documentation" in lowered or "docs" in lowered:
            add("documentation")
        return facets

    source_artifact_types = {
        "api_service_source",
        "frontend_source",
        "test_source",
        "source",
        "config_file",
        "macos_app_bundle",
    }
    has_source_signal = (
        bool(source_artifact_types & artifact_types)
        or bool(re.search(r"\b(api|fastapi|frontend|html|css|javascript|typescript|react|vue|cli|server|service|app|web\s*app|webapp|source|code)\b", lowered))
        or any(path.endswith((".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".swift", ".go", ".rs", ".java", ".kt")) for path in path_hints)
    )
    if "functional" in task_types or has_source_signal:
        add("source_project")
    if re.search(r"\b(app|application|web\s*app|webapp|service|server|cli|tool|dashboard)\b", lowered):
        add("runnable_app")
    if (
        "frontend_source" in artifact_types
        or any(path.endswith((".html", ".css", ".js", ".jsx", ".tsx", ".ts", ".vue")) for path in path_hints)
        or re.search(r"\b(frontend|front-end|html|css|javascript|web\s+ui|browser|dashboard)\b", lowered)
    ):
        add("web_ui")
    if "api_service_source" in artifact_types or _mentions_api_service(lowered):
        add("api_service")
    if re.search(r"\b(cli|command[- ]line|argparse)\b", lowered):
        add("cli_tool")
    if _mentions_packaged_macos_app(lowered):
        add("desktop_app")
    if re.search(r"\b(sqlite|database|db|local\s+storage|persist|persistence|json\s+file)\b", lowered):
        add("local_storage")
    if "artifact" in task_types or any(path.endswith((".md", ".rst", ".txt")) for path in path_hints) or "documentation" in lowered or "docs" in lowered:
        add("documentation")
    if _mentions_test_suite(lowered) or any("test" in path for path in path_hints):
        add("test_suite")
    if re.search(r"\b(config|configuration|settings|env)\b", lowered):
        add("configuration")
    return facets


def _infer_technology_hypotheses(description: str, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    lowered = _without_absolute_paths(description).lower()
    deliverables = manifest.get("deliverables", []) or []
    path_hints = {str(item.get("path_hint") or "").lower() for item in deliverables}
    hypotheses: List[Dict[str, Any]] = []

    def add(stack: str, confidence: float, signals: List[str]) -> None:
        if any(item["stack"] == stack for item in hypotheses):
            return
        hypotheses.append({
            "stack": stack,
            "confidence": confidence,
            "signals": signals,
        })

    if re.search(r"\bfastapi\b", lowered):
        add("python-fastapi", 0.9, ["FastAPI"])
    elif re.search(r"\b(starlette|asgi|uvicorn|pytest|python)\b", lowered) or any(path.endswith(".py") for path in path_hints):
        add("python", 0.7, ["Python"])
    if re.search(r"\bsqlite\b", lowered):
        add("sqlite", 0.85, ["SQLite"])
    if re.search(r"\b(html|css|javascript|native\s+html|vanilla\s+js)\b", lowered) or any(path.endswith((".html", ".css", ".js")) for path in path_hints):
        add("native-web", 0.8, ["HTML", "CSS", "JavaScript"])
    if _mentions_node_web(lowered, path_hints):
        add("node-web", 0.72, ["Node.js"])
    if (
        _has_non_negated_pattern(description, r"\b(swift|swiftui|appkit)\b")
        or "package.swift" in path_hints
        or _mentions_packaged_macos_app(description)
    ):
        add("swift-macos", 0.75, ["Swift", "macOS"])
    if re.search(r"\b(go|golang)\b", lowered) or "go.mod" in path_hints:
        add("go", 0.7, ["Go"])
    if re.search(r"\b(rust|cargo)\b", lowered) or "cargo.toml" in path_hints:
        add("rust", 0.7, ["Rust"])
    return hypotheses


def _infer_deliverable_groups(
    description: str,
    manifest: Dict[str, Any],
    facets: List[str],
    technology_hypotheses: List[Dict[str, Any]],
    allowed_docs: List[str],
) -> List[Dict[str, Any]]:
    stacks = {item["stack"] for item in technology_hypotheses}
    groups: List[Dict[str, Any]] = []
    doc_files = allowed_docs or [
        item.get("path_hint")
        for item in manifest.get("deliverables", []) or []
        if str(item.get("path_hint") or "").lower().endswith((".md", ".rst", ".txt"))
    ]

    if "api_service" in facets:
        if "python-fastapi" in stacks or "python" in stacks:
            groups.append({
                "id": "group-api-source",
                "kind": "api_service_source",
                "required": True,
                "description": "Backend API service source files.",
                "allowed_roots": ["app/", "src/", "backend/", "expense_app/", "."],
                "allowed_extensions": [".py"],
                "one_of_entrypoints": ["main.py", "app/main.py", "backend/main.py", "expense_app/main.py", "server.py", "app.py"],
                "min_file_count": 1,
                "max_file_count": 80,
            })
        else:
            groups.append({
                "id": "group-api-source",
                "kind": "api_service_source",
                "required": True,
                "description": "Backend API service source files.",
                "allowed_roots": ["api/", "app/", "src/", "server/", "backend/", "."],
                "allowed_extensions": [".js", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".rb", ".php", ".java", ".kt"],
                "one_of_entrypoints": [
                    "api/server.mjs",
                    "api/server.js",
                    "server.mjs",
                    "server.js",
                    "app.js",
                    "index.js",
                    "src/server.ts",
                    "src/main.ts",
                    "main.go",
                    "src/main.rs",
                ],
                "min_file_count": 1,
                "max_file_count": 120,
            })
    if "web_ui" in facets:
        groups.append({
            "id": "group-web-ui",
            "kind": "frontend_source",
            "required": True,
            "description": "User-facing frontend source files.",
            "allowed_roots": ["web/", "static/", "public/", "assets/", "src/", "app/", "."],
            "allowed_extensions": [".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"],
            "one_of_entrypoints": [
                "index.html",
                "web/index.html",
                "static/index.html",
                "app/static/index.html",
                "public/index.html",
                "src/App.jsx",
                "src/App.tsx",
                "app/page.tsx",
            ],
            "min_file_count": 1,
            "max_file_count": 120,
        })
    if "test_suite" in facets:
        groups.append({
            "id": "group-test-suite",
            "kind": "test_suite",
            "required": True,
            "description": "Automated tests for the delivered behavior.",
            "allowed_roots": ["tests/", "test/", "__tests__/", "src/"],
            "allowed_extensions": [".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".swift", ".go", ".rs"],
            "min_file_count": 1,
            "max_file_count": 80,
        })
    if (
        ("runnable_app" in facets or "source_project" in facets)
        and not _is_direct_static_web_without_install_metadata(description, manifest, technology_hypotheses)
    ):
        one_of = ["pyproject.toml", "requirements.txt", "package.json", "Package.swift", "Cargo.toml", "go.mod", "Makefile", "README.md"]
        groups.append({
            "id": "group-install-metadata",
            "kind": "install_metadata",
            "required": True,
            "description": "Install or run metadata for a clean environment.",
            "one_of": one_of,
        })
    if "documentation" in facets:
        groups.append({
            "id": "group-docs",
            "kind": "documentation",
            "required": bool(doc_files),
            "description": "User-facing documentation files.",
            "allowed_files": [path for path in doc_files if path],
            "forbid_extra_docs": bool(allowed_docs),
        })
    return groups


def _is_direct_static_web_without_install_metadata(
    description: str,
    manifest: Dict[str, Any],
    technology_hypotheses: List[Dict[str, Any]],
) -> bool:
    lowered = description.lower()
    path_hints = {
        str(item.get("path_hint") or "").lower()
        for item in (manifest.get("deliverables", []) or [])
        if item.get("path_hint")
    }
    stacks = {str(item.get("stack") or "") for item in technology_hypotheses}
    has_static_web_files = bool(path_hints & {"index.html", "styles.css", "app.js"}) or any(
        path.endswith((".html", ".css", ".js")) for path in path_hints
    )
    direct_file_run = bool(
        re.search(r"\b(open|opening|opened)\s+index\.html\s+directly\b", lowered)
        or "file://" in lowered
    )
    forbids_install_metadata = bool(
        re.search(r"\bno\s+package\s+managers?\b", lowered)
        or "no package.json" in lowered
        or "no generated dependencies" in lowered
        or "without a server" in lowered
    )
    has_backend_stack = bool(stacks & {"python-fastapi", "python", "node-web", "swift-macos", "go", "rust"})
    return (
        has_static_web_files
        and direct_file_run
        and forbids_install_metadata
        and "native-web" in stacks
        and not has_backend_stack
    )


def _infer_gate_plan(
    facets: List[str],
    technology_hypotheses: List[Dict[str, Any]],
    probes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    stacks = {item["stack"] for item in technology_hypotheses}
    probe_types = {probe["probe_type"] for probe in probes}
    gates: List[Dict[str, Any]] = []

    def add(gate_id: str, gate_type: str, description: str, *, probe_type: Optional[str] = None, required: bool = True) -> None:
        if any(item["id"] == gate_id for item in gates):
            return
        gate: Dict[str, Any] = {
            "id": gate_id,
            "gate_type": gate_type,
            "required": required,
            "description": description,
        }
        if probe_type:
            gate["probe_type"] = probe_type
        gates.append(gate)

    if "runnable_app" in facets or "source_project" in facets:
        if "python-fastapi" in stacks or "python" in stacks:
            add("gate-install", "install", "Install Python dependencies in a clean environment.", probe_type="python_install")
        elif "node-web" in stacks:
            add("gate-install", "install", "Install Node dependencies in a clean environment.", probe_type="node_install")
        else:
            add("gate-install", "install", "Verify clean-environment install or run instructions.", required=False)
    if "api_service" in facets or "web_ui" in facets:
        add(
            "gate-runtime-smoke",
            "runtime_smoke",
            "Start or import the app and exercise the runtime entry point.",
            probe_type="python_web_smoke" if "python_web_smoke" in probe_types else None,
        )
    if "web_ui" in facets:
        add(
            "gate-browser-e2e",
            "browser_e2e",
            "Exercise at least one user-visible browser path.",
            probe_type="browser_e2e" if "browser_e2e" in probe_types else None,
            required="browser_e2e" in probe_types,
        )
    if "test_suite" in facets:
        add("gate-tests", "test", "Run the generated automated tests.", probe_type="pytest" if "pytest" in probe_types else None)
    add("gate-constraint-scan", "constraint_scan", "Scan forbidden tooling and user constraints.")
    add("gate-workspace-hygiene", "workspace_hygiene", "Reject runtime caches, temporary scripts, and excessive output files.")
    return gates


def _infer_acceptance_probes(description: str, task_types: List[str], manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    lowered = description.lower()
    has_python = _has_python_delivery_signal(description, manifest)
    path_hints = _delivery_path_hints(manifest)
    artifact_types = {
        str(item.get("artifact_type") or "").lower()
        for item in manifest.get("deliverables", []) or []
    }
    wants_pytest = has_python and (_mentions_test_suite(description) or any(path.endswith(".py") for path in path_hints))
    probes: List[Dict[str, Any]] = []
    if "functional" in task_types and has_python:
        probes.append({
            "id": "probe-python-install",
            "probe_type": "python_install",
            "command": "python -m venv /tmp/... && pip install",
            "required": True,
            "source": "owner_inferred",
            "minimum_evidence": "L2",
        })
    needs_web_smoke, require_html_root = _requires_python_web_smoke(description, manifest)
    if "functional" in task_types and needs_web_smoke:
        probes.append({
            "id": "probe-python-web-smoke",
            "probe_type": "python_web_smoke",
            "command": "import ASGI app with TestClient and GET /",
            "required": True,
            "source": "owner_inferred",
            "minimum_evidence": "L2",
            "require_html_root": require_html_root,
        })
    if "functional" in task_types and _requires_static_web_smoke(description, manifest):
        probes.append({
            "id": "probe-static-web-smoke",
            "probe_type": "static_web_smoke",
            "command": "serve index.html from project root and validate requested static UI evidence",
            "required": True,
            "source": "owner_inferred",
            "minimum_evidence": "L2",
        })
    if "functional" in task_types and _requires_browser_e2e_probe(description, manifest):
        probes.append({
            "id": "probe-browser-e2e",
            "probe_type": "browser_e2e",
            "command": "open static entrypoint in a real browser and validate user-visible interactions",
            "required": True,
            "source": "owner_inferred",
            "minimum_evidence": "L3",
        })
    requires_node_api_probe = (
        "api/server.mjs" in path_hints
        or "api/server.js" in path_hints
        or "api/server.mjs" in lowered
        or "node.js built-in http server" in lowered
        or ("api_service_source" in artifact_types and any(path.endswith((".mjs", ".js")) for path in path_hints))
    )
    if "functional" in task_types and requires_node_api_probe:
        probes.append({
            "id": "probe-api-service",
            "probe_type": "api_service",
            "command": "start the Node API with PORT and verify /health, /api/agents, /api/route, and /api/report",
            "required": True,
            "source": "owner_inferred",
            "minimum_evidence": "L2",
        })
    requires_cli_probe = (
        "cli/quality-check.mjs" in path_hints
        or "cli/quality-check.mjs" in lowered
        or "quality-check.mjs" in lowered
    )
    if "functional" in task_types and requires_cli_probe:
        probes.append({
            "id": "probe-cli-generic",
            "probe_type": "cli_generic",
            "command": "node cli/quality-check.mjs",
            "required": True,
            "source": "owner_inferred",
            "minimum_evidence": "L2",
        })
    if "functional" in task_types and wants_pytest:
        probes.append({
            "id": "probe-pytest",
            "probe_type": "pytest",
            "command": "pytest",
            "required": True,
            "source": "owner_inferred",
            "minimum_evidence": "L2",
        })
    if (
        "functional" in task_types
        and "notes_cli.py" in path_hints
        and all(keyword in lowered for keyword in ("add", "list", "done", "search", "export"))
    ):
        probes.append({
            "id": "probe-notes-cli-smoke",
            "probe_type": "notes_cli_smoke",
            "command": "python3 notes_cli.py add/list/done/search/export",
            "required": True,
            "source": "owner_inferred",
            "minimum_evidence": "L2",
        })
    return probes


def build_owner_delivery_contract(
    *,
    task_id: str,
    description: str,
    task_types: List[str],
    project_dir: Optional[str],
    manifest: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    normalized_types, delivery_mode = normalize_delivery_task_types(task_types)
    manifest = manifest or {"deliverables": []}
    forbidden_paths = extract_forbidden_path_hints(description)
    for path_hint in _extract_forbidden_runner_script_files(description):
        if canonical_requirement_key(path_hint) not in {
            canonical_requirement_key(path) for path in forbidden_paths
        }:
            forbidden_paths.append(path_hint)
    forbidden_keys = {canonical_requirement_key(path) for path in forbidden_paths}

    deliverables: List[Dict[str, Any]] = []
    if "artifact" in normalized_types:
        for item in manifest.get("deliverables", []) or []:
            if not item.get("required", True):
                continue
            path_hint = item.get("path_hint")
            if not path_hint:
                continue
            if canonical_requirement_key(path_hint) in forbidden_keys:
                continue
            deliverables.append({
                "id": item.get("requirement_id") or f"del-{uuid.uuid4().hex[:8]}",
                "artifact_type": item.get("artifact_type") or "file",
                "path_hint": path_hint,
                "required": True,
                "source": item.get("source") or "explicit_user_request",
                "description": item.get("description") or f"Required deliverable: {path_hint}",
            })
    required_manifest_paths = [
        item.get("path_hint")
        for item in manifest.get("deliverables", []) or []
        if item.get("required", True) and item.get("path_hint")
    ]

    constraints: List[Dict[str, Any]] = []
    for path_hint in forbidden_paths:
        scope = _forbidden_file_scope(description, path_hint)
        constraints.append({
            "id": f"constraint-forbidden-file-{uuid.uuid4().hex[:8]}",
            "constraint_type": "forbidden_file",
            "value": path_hint,
            "scope": scope,
            "required": True,
            "source": "explicit_user_request",
            "description": (
                f"Do not create or require {path_hint} at project root."
                if scope == "project_root"
                else f"Do not create or require {path_hint}."
            ),
        })
    if _contains_no_docker_constraint(description):
        constraints.append({
            "id": "constraint-no-docker",
            "constraint_type": "forbidden_tooling",
            "value": "docker",
            "required": True,
            "source": "explicit_user_request",
            "description": "Do not require, generate, or validate Docker/container tooling.",
        })
    required_agent_mix = _extract_required_agent_mix(description)
    if required_agent_mix:
        constraints.append({
            "id": "constraint-agent-mix",
            "constraint_type": "agent_mix",
            "value": required_agent_mix,
            "required": True,
            "source": "explicit_user_request",
            "description": "Required actual execution mix across local agents and cloud LLMs.",
        })
    allowed_docs = _extract_allowed_documentation_files(description)
    if allowed_docs:
        constraints.append({
            "id": "constraint-allowed-documentation-files",
            "constraint_type": "allowed_documentation_files",
            "value": allowed_docs,
            "required": True,
            "source": "explicit_user_request",
            "description": "Only the explicitly requested documentation files may be produced.",
        })
    if "functional" in normalized_types and not _explicitly_requests_auth(description):
        constraints.append({
            "id": "constraint-no-unrequested-auth",
            "constraint_type": "forbidden_unrequested_auth",
            "value": "auth",
            "required": True,
            "source": "owner_policy",
            "description": "Do not implement authentication, login, users, password hashing, OAuth/JWT, or role systems unless explicitly requested.",
        })
    exact_allowed_paths = [item["path_hint"] for item in deliverables if item.get("path_hint")] or required_manifest_paths
    if _requires_exact_file_set(description) and exact_allowed_paths:
        constraints.append({
            "id": "constraint-allowed-files",
            "constraint_type": "allowed_files",
            "value": exact_allowed_paths,
            "required": True,
            "source": "explicit_user_request",
            "description": "Only the explicitly requested artifact files may be produced.",
        })

    capability_diagnostics: Dict[str, Any] = {
        "included_capability_ids": [],
        "excluded_capability_ids": [],
        "extractor": "heuristic_v2",
    }
    if "functional" in normalized_types:
        capabilities, capability_diagnostics = _extract_capabilities_with_diagnostics(description, manifest)
    else:
        capabilities = []
    probes = _infer_acceptance_probes(description, normalized_types, manifest)
    probe_ids = {probe.get("id") for probe in probes}
    if "probe-browser-e2e" in probe_ids:
        for capability in capabilities:
            capability_probe_ids = capability.setdefault("acceptance_probe_ids", [])
            if "probe-static-web-smoke" in capability_probe_ids and "probe-browser-e2e" not in capability_probe_ids:
                capability_probe_ids.append("probe-browser-e2e")
    allowed_docs = _extract_allowed_documentation_files(description)
    delivery_facets = _infer_delivery_facets(description, normalized_types, manifest)
    technology_hypotheses = _infer_technology_hypotheses(description, manifest)
    deliverable_groups = _infer_deliverable_groups(
        description,
        manifest,
        delivery_facets,
        technology_hypotheses,
        allowed_docs,
    )
    gate_plan = _infer_gate_plan(delivery_facets, technology_hypotheses, probes)

    contract = {
        "contract_version": "2.0",
        "contract_id": f"delivery-contract-{uuid.uuid4().hex[:8]}",
        "task_id": task_id,
        "task_types": normalized_types,
        "delivery_mode": delivery_mode,
        "delivery_facets": delivery_facets,
        "technology_hypotheses": technology_hypotheses,
        "project_dir": project_dir,
        "capabilities": capabilities,
        "deliverables": deliverables,
        "deliverable_groups": deliverable_groups,
        "constraints": constraints,
        "acceptance_probes": probes,
        "gate_plan": gate_plan,
        "extraction_diagnostics": capability_diagnostics,
        "assumptions": [],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    contract["probe_adapter_plan"] = ProbeAdapterRegistry.default().build_gate_plan(
        project_dir or "",
        contract,
    )
    return contract
