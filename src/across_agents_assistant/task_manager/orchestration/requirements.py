"""Requirement manifest extraction from user task descriptions.

Extracts required deliverables (files, configurations, documentation) that are
explicitly listed or strongly implied by the user request.  The manifest is used
to check decomposition coverage and to produce project-level quality reports.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from typing import Dict, Iterable, List, Optional, Tuple

from across_agents_assistant.task_manager.models import (
    AcceptanceCheck,
    RequirementDeliverable,
    RequirementManifest,
)


# -- Well-known filenames without (or with uncommon) extensions ----------------

SPECIAL_FILENAMES = {
    "Dockerfile",
    "Containerfile",
    "Makefile",
    "README",
    "LICENSE",
    "CHANGELOG",
}

SPECIAL_DOTFILES = {
    ".env.example",
    ".gitignore",
    ".dockerignore",
}

IGNORED_FILELIKE_WORDS = {
    "python3",
    "python",
    "json",
    "api",
    "app",
    "fastapi",
    "pydantic",
    "uvicorn",
    "pytest",
    "httpx",
    "node",
    "node.js",
}

KNOWN_FILE_EXTENSIONS = {
    ".py", ".md", ".txt", ".rst", ".json", ".toml", ".yaml", ".yml",
    ".ini", ".cfg", ".env", ".example", ".sh", ".html", ".css", ".js",
    ".ts", ".tsx", ".jsx", ".vue", ".swift", ".kt", ".java", ".go",
    ".rs", ".rb", ".php",
}

MODULE_LIKE_DOTTED_NAMES = {
    "http.server",
    "unittest.mock",
}

CONFIG_FILENAMES = {
    "requirements.txt",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "mypy.ini",
    "pytest.ini",
}

RUNTIME_DATA_EXTENSIONS = {".json", ".db", ".sqlite", ".sqlite3"}

AUXILIARY_DELIVERABLE_BASENAMES = {
    "__init__.py",
}


# -- Path de-duplication -------------------------------------------------------


def canonical_requirement_key(path_hint: str) -> str:
    """Return a stable equivalence key for requirement de-duplication.

    Bare Python basenames (e.g. ``server.py``) are collapsed into a
    ``py:`` namespace so that a nested path like ``src/server.py`` can
    replace them.  Nested Python paths keep their full identity to avoid
    collapsing ``app/utils.py`` and ``lib/utils.py``.
    """
    normalized = normalize_path_hint(path_hint) or path_hint
    normalized = normalized.replace("\\", "/")
    basename = os.path.basename(normalized)
    lower_path = normalized.lower()
    lower_base = basename.lower()

    if lower_base in {"readme", "readme.md", "readme.rst", "readme.txt"}:
        return "doc:readme"
    if lower_base in {"license", "license.md", "license.txt"}:
        return "doc:license"
    if lower_base in {"changelog", "changelog.md", "changelog.txt"}:
        return "doc:changelog"
    if lower_base.startswith("test_") and lower_base.endswith(".py"):
        return f"test:{lower_base}"
    if lower_base.endswith("_test.py"):
        return f"test:{lower_base}"
    if lower_base.endswith(".py") and "/" not in normalized:
        return f"py:{lower_base}"
    return lower_path


def requirement_preference_score(path_hint: str) -> tuple:
    """Higher score wins when two path hints refer to the same requirement."""
    normalized = normalize_path_hint(path_hint) or path_hint
    basename = os.path.basename(normalized).lower()
    score = 0
    if "/" in normalized:
        score += 20
    if "." in basename:
        score += 10
    if basename == "readme.md":
        score += 30
    if normalized.startswith("tests/"):
        score += 30
    return (score, len(normalized))


def dedupe_requirement_path_hints(path_hints: List[str]) -> List[str]:
    """Deduplicate overlapping extraction results while preserving useful paths."""
    best: Dict[str, str] = {}
    order: List[str] = []
    for path_hint in path_hints:
        key = canonical_requirement_key(path_hint)
        if key not in best:
            best[key] = path_hint
            order.append(key)
            continue
        current = best[key]
        if requirement_preference_score(path_hint) > requirement_preference_score(current):
            best[key] = path_hint
    result = [best[key] for key in order]

    # Post-process: remove bare basenames that have a nested equivalent.
    # "bare" = no directory separator.  Nested paths are preferred for the
    # same file when both exist (e.g. src/server.py wins over server.py).
    nested_basenames: Dict[str, str] = {}
    for path in result:
        if "/" in path:
            base = os.path.basename(path).lower()
            if base not in nested_basenames:
                nested_basenames[base] = path

    if nested_basenames:
        result = [
            p for p in result
            if not ("/" not in p and os.path.basename(p).lower() in nested_basenames)
        ]
    return result


# -- Public API ----------------------------------------------------------------


def extract_requirement_manifest(
    task_id: str,
    description: str,
    project_dir: Optional[str] = None,
) -> RequirementManifest:
    """Build a ``RequirementManifest`` by scanning *description* for required files."""
    manifest = RequirementManifest.new(task_id=task_id, project_dir=project_dir)
    seen: set[Tuple[str, str]] = set()

    for path_hint in extract_required_path_hints(description):
        if is_runtime_data_path_hint(description, path_hint):
            continue
        artifact_type = infer_artifact_type(path_hint, description)
        key = (artifact_type, path_hint)
        if key in seen:
            continue
        seen.add(key)
        manifest.deliverables.append(
            RequirementDeliverable(
                requirement_id=f"req-{uuid.uuid4().hex[:8]}",
                artifact_type=artifact_type,
                required=True,
                path_hint=path_hint,
                description=f"Required deliverable from user request: {path_hint}",
                source="user_request",
            )
        )

    for check_type, check_desc in infer_quality_checks(description, manifest.deliverables):
        manifest.quality_checks.append(
            AcceptanceCheck(check_type=check_type, description=check_desc, required=True)
        )

    manifest.updated_at = time.time()
    return manifest


# -- Path-hint extraction -----------------------------------------------------


NEGATIVE_PATH_CONTEXT_RE = re.compile(
    r"(?:\b("
    r"do\s+not|don't|dont|without|avoid|forbid(?:den)?|prohibit(?:ed)?|"
    r"disallow(?:ed)?|must\s+not|should\s+not|no"
    r")\b|不要|不得|禁止|禁用|不能|不可|无需|不需要|不允许|避免)",
    re.IGNORECASE,
)

NEGATIVE_PATH_LIST_PREFIX_RE = re.compile(
    r"(?:\b(?:do\s+not|don't|dont|without|avoid|forbid(?:den)?|prohibit(?:ed)?|"
    r"disallow(?:ed)?|must\s+not|should\s+not|no)\b|不要|不得|禁止|禁用|不能|不可|无需|不需要|不允许|避免)"
    r".{0,60}"
    r"(?:\b(?:files?|scripts?|docs?|documents?|dockerfile|docker-compose|docker|"
    r"setup\.py|__init__\.py|container\s+tooling|package\s+files?)\b|文件|脚本|文档)",
    re.IGNORECASE,
)

POSITIVE_PATH_LIST_PREFIX_RE = re.compile(
    r"(?:\b(?:deliver|create|include|write|produce|required|requires?|must\s+deliver)\b"
    r".{0,120}\b(?:files?|deliverables?|artifacts?)\b)"
    r"|(?:\b(?:files?|deliverables?|artifacts?)\b.{0,120}"
    r"\b(?:deliver|create|include|write|produce|required|exactly|only|single)\b)"
    r"|(?:\bkeep\b.{0,80}\b(?:deliverable|deliverables|artifact|artifacts|file|files)\b.{0,40}\bsmall\b)"
    r"|(?:\b(?:deliverable|deliverables|artifact|artifacts|file|files)\b.{0,40}\bsmall\b)"
    r"|(?:交付|创建|生成|输出|需要|必须).{0,80}(?:文件|产物)",
    re.IGNORECASE,
)


def _is_positive_colon_list_item(clause: str, path_hint: str) -> bool:
    """True when a path appears in a positive file list after a colon.

    A sentence can contain both a positive deliverable list and an unrelated
    negative constraint, for example:
    ``Deliver exactly these files and no package manager output: index.html``.
    In that shape the files after the colon are required deliverables, not
    forbidden paths.
    """
    if not clause or not path_hint:
        return False
    lower_clause = clause.lower()
    lower_hint = path_hint.lower()
    hint_index = lower_clause.find(lower_hint)
    if hint_index < 0:
        return False
    colon_index = lower_clause.rfind(":", 0, hint_index)
    if colon_index < 0:
        return False
    prefix = lower_clause[:colon_index]
    list_prefix = lower_clause[max(0, colon_index - 140):colon_index]
    after_colon_before_hint = lower_clause[colon_index + 1:hint_index]
    if NEGATIVE_PATH_CONTEXT_RE.search(after_colon_before_hint):
        return False
    if not POSITIVE_PATH_LIST_PREFIX_RE.search(prefix):
        return False
    return not NEGATIVE_PATH_LIST_PREFIX_RE.search(list_prefix)


def has_negative_container_constraint(description: str) -> bool:
    """Return True when Docker/container tooling is explicitly forbidden."""
    text = (description or "").lower()
    negative = (
        r"(?:\b(?:do\s+not|don't|dont|without|no|avoid|forbid(?:den)?|prohibit(?:ed)?|"
        r"disallow(?:ed)?|must\s+not|should\s+not)\b|不要|不得|禁止|禁用|不能|不可|无需|不需要|不允许|避免)"
    )
    container = (
        r"(?:\b(?:docker|dockerfile|docker-compose|container|containers|containerfile|container\s+tooling)\b|容器)"
    )
    return bool(
        re.search(negative + r".{0,80}" + container, text)
        or re.search(container + r".{0,80}" + negative, text)
    )


def has_container_delivery_intent(description: str) -> bool:
    """Return True when the task asks for deployable container tooling.

    The bare word "container" is common in frontend work ("canvas container",
    "layout container") and must not imply Docker deliverables by itself.
    """
    text = (description or "").lower()
    if has_negative_container_constraint(description):
        return False
    if re.search(r"\b(docker|dockerfile|docker-compose|containerfile|kubernetes|k8s)\b", text):
        return True
    if re.search(r"\b(containerized|containerised|container\s+image|container\s+build|container\s+tooling|container\s+deployment)\b", text):
        return True
    ui_container = (
        r"\b(?:canvas|animation|layout|ui|dom|html|css|page|component|card|panel|full[-\s]*screen)\b"
        r".{0,80}\bcontainer\b"
        r"|\bcontainer\b.{0,80}"
        r"\b(?:element|div|layout|canvas|animation|ui|dom|html|css|page|component|card|panel)\b"
    )
    if re.search(ui_container, text):
        return False
    if re.search(r"\b(build|package|deploy|run|ship)\b.{0,80}\bcontainer\b", text):
        return True
    if re.search(r"\bcontainer\b.{0,80}\b(image|build|runtime|registry|deployment|orchestration)\b", text):
        return True
    return bool(re.search(r"(容器化|容器镜像|容器部署|容器运行时)", text))


def _collect_path_hints_from_text(text: str) -> List[str]:
    candidates: List[str] = []
    seen: set[str] = set()

    patterns = [
        r"`([^`\n]+)`",
        r"\b((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)\b",
        r"\b([A-Za-z0-9_-]+\.[A-Za-z0-9][A-Za-z0-9_.-]*)\b",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text):
            for item in split_candidate_list(match.group(1)):
                normalized = normalize_path_hint(item)
                if not normalized:
                    continue
                for expanded in expand_path_hint_alternatives(normalized):
                    if not is_probable_deliverable_path(expanded):
                        continue
                    if expanded not in seen:
                        seen.add(expanded)
                        candidates.append(expanded)

    for special in sorted(SPECIAL_FILENAMES | SPECIAL_DOTFILES, key=len, reverse=True):
        pattern = rf"(?:^|[\s,，、]){re.escape(special)}(?=[\s,，、./。；;]|$)"
        if re.search(pattern, text):
            if special not in seen:
                seen.add(special)
                candidates.append(special)

    return candidates


def extract_forbidden_path_hints(description: str) -> List[str]:
    """Return file-path candidates that appear in explicit negative contexts."""
    forbidden: List[str] = []
    seen: set[str] = set()
    clauses = re.split(r"(?<=[;!?。；！？，])\s*|(?<=[.])\s+|[\n\r]+", description)
    for clause in clauses:
        if not NEGATIVE_PATH_CONTEXT_RE.search(clause):
            continue
        for path_hint in _collect_path_hints_from_text(clause):
            if _is_positive_colon_list_item(clause, path_hint):
                continue
            if _is_positive_runtime_entrypoint_before_negative_context(clause, path_hint):
                continue
            if _is_positive_deliverable_before_negative_context(clause, path_hint):
                continue
            if _is_validation_manifest_reference_before_negative_context(clause, path_hint):
                continue
            if _is_final_deliverable_status_negation(clause, path_hint):
                continue
            if _is_content_restriction_subject(clause, path_hint):
                continue
            if _is_excepted_from_negative_context(clause, path_hint):
                continue
            if path_hint not in seen:
                seen.add(path_hint)
                forbidden.append(path_hint)
    return dedupe_requirement_path_hints(forbidden)


def _is_positive_deliverable_before_negative_context(clause: str, path_hint: str) -> bool:
    """True when a requested output path appears before unrelated constraints.

    Example: ``Create web/app.js ... without fetch`` requires ``web/app.js``;
    the negative constraint targets runtime behavior, not the file path.
    """
    if not clause or not path_hint:
        return False
    lowered = clause.lower()
    normalized = normalize_path_hint(path_hint or "").lower()
    if not normalized:
        return False
    terms = sorted({normalized, os.path.basename(normalized)}, key=len, reverse=True)
    negative_matches = list(NEGATIVE_PATH_CONTEXT_RE.finditer(lowered))
    if not negative_matches:
        return False

    for term in terms:
        index = lowered.find(term)
        if index < 0:
            continue
        if any(match.start() < index for match in negative_matches):
            continue
        if not any(match.start() > index for match in negative_matches):
            continue
        before = lowered[max(0, index - 120):index]
        if re.search(r"\b(create|write|produce|deliver|output|generate|implement|build|add|update)\b", before):
            return True
    return False


def _is_validation_manifest_reference_before_negative_context(clause: str, path_hint: str) -> bool:
    """True when a manifest item is referenced before unrelated negatives.

    Example: ``Check the exact manifest (README.md, web/app.js) ... no external
    packages`` references manifest members; the negative targets dependencies.
    """
    if not clause or not path_hint:
        return False
    lowered = clause.lower()
    normalized = normalize_path_hint(path_hint or "").lower()
    if not normalized:
        return False
    terms = sorted({normalized, os.path.basename(normalized)}, key=len, reverse=True)
    negative_matches = list(NEGATIVE_PATH_CONTEXT_RE.finditer(lowered))
    if not negative_matches:
        return False

    for term in terms:
        index = lowered.find(term)
        if index < 0:
            continue
        if any(match.start() < index for match in negative_matches):
            continue
        if not any(match.start() > index for match in negative_matches):
            continue
        before = lowered[max(0, index - 220):index]
        if re.search(
            r"\b(check|checks|validate|validates|verify|verifies)\b.{0,180}"
            r"\b(exact|manifest|required\s+files?|file\s+manifest|seven[-\s]*file|7[-\s]*file)\b",
            before,
        ):
            return True
    return False


def _is_positive_runtime_entrypoint_before_negative_context(clause: str, path_hint: str) -> bool:
    """True when a required runtime entrypoint appears before unrelated negatives.

    Example: ``open index.html directly, with no package managers`` means
    ``index.html`` is the positive runtime entrypoint. The negative clause
    constrains tooling, not the file itself.
    """
    if not clause or not path_hint:
        return False
    lowered = clause.lower()
    normalized = normalize_path_hint(path_hint or "").lower()
    if not normalized:
        return False
    terms = sorted({normalized, os.path.basename(normalized)}, key=len, reverse=True)
    negative_matches = list(NEGATIVE_PATH_CONTEXT_RE.finditer(lowered))
    if not negative_matches:
        return False

    for term in terms:
        index = lowered.find(term)
        if index < 0:
            continue
        negatives_before = [match for match in negative_matches if match.start() < index]
        negatives_after = [match for match in negative_matches if match.start() > index]
        if negatives_before or not negatives_after:
            continue
        before = lowered[max(0, index - 100):index]
        first_negative = negatives_after[0]
        between = lowered[index + len(term):first_negative.start()]
        negative_tail = lowered[first_negative.start():first_negative.start() + 140]
        has_positive_entrypoint_verb = bool(
            re.search(
                r"\b(?:open|opening|run|running|load|loading|serve|serving|view|viewing)\b.{0,80}$",
                before,
            )
        )
        has_entrypoint_context = bool(
            re.search(r"\b(?:directly|browser|locally|entry\s*point|entrypoint|static)\b", between)
        )
        negative_targets_tooling = bool(
            re.search(
                r"\b(?:package\s+managers?|external\s+cdn|generated\s+dependencies|dependencies|"
                r"node_modules|frameworks?|dev\s+servers?|server|build\s+(?:commands?|steps?|process(?:es)?)|"
                r"build\s*step|build)\b",
                negative_tail,
            )
        )
        if has_positive_entrypoint_verb and (has_entrypoint_context or negative_targets_tooling):
            return True
    return False


def _is_excepted_from_negative_context(clause: str, path_hint: str) -> bool:
    """True when a path appears in the exception portion of a negative clause.

    Example: ``除 README.md 和 TESTING.md 外不要增加额外文档`` forbids extra
    docs, not README.md or TESTING.md themselves.
    """
    if not clause or not path_hint:
        return False
    lower_clause = clause.lower()
    lower_hint = path_hint.lower()
    hint_index = lower_clause.find(lower_hint)
    if hint_index < 0:
        return False
    chinese_boundary = lower_clause.find("外")
    if "除" in lower_clause and chinese_boundary >= 0 and hint_index < chinese_boundary:
        return True
    except_boundary = lower_clause.find("except")
    negative_boundary = min(
        [idx for idx in (lower_clause.find("do not"), lower_clause.find("don't"), lower_clause.find("no ")) if idx >= 0],
        default=-1,
    )
    return except_boundary >= 0 and negative_boundary >= 0 and except_boundary < hint_index < negative_boundary


def _is_final_deliverable_status_negation(clause: str, path_hint: str) -> bool:
    """True when the user says a path is not a final deliverable, not that it is forbidden.

    Example: ``Do not treat todo.json as a final deliverable; it is runtime
    data`` should prevent ``todo.json`` from becoming a requirement, but should
    not fail the task if the program legitimately creates runtime data.
    """
    lowered = (clause or "").lower()
    normalized = normalize_path_hint(path_hint or "").lower()
    basename = os.path.basename(normalized)
    if not normalized:
        return False
    escaped_hint = re.escape(normalized)
    escaped_base = re.escape(basename)
    path_pattern = f"(?:{escaped_hint}|{escaped_base})"
    deliverable_phrase = r"(?:final\s+)?(?:deliverable|artifact|handoff|requirement|required\s+file)"
    return bool(
        re.search(r"\b(?:treat|consider|count|require)\b.{0,80}" + path_pattern + r".{0,80}\bas\b.{0,30}" + deliverable_phrase, lowered)
        or re.search(path_pattern + r".{0,80}\b(?:as|be)\b.{0,30}" + deliverable_phrase, lowered)
    )


def _is_content_restriction_subject(clause: str, path_hint: str) -> bool:
    """True when a path is the file being constrained, not the forbidden file.

    Example: ``README.md must not mention npm`` restricts README content; it
    should not make README.md forbidden.
    """
    lowered = (clause or "").lower()
    normalized = normalize_path_hint(path_hint or "").lower()
    if not lowered or not normalized:
        return False
    terms = {normalized, os.path.basename(normalized)}
    for term in sorted(terms, key=len, reverse=True):
        index = lowered.find(term)
        if index < 0:
            continue
        before = lowered[max(0, index - 60):index]
        if NEGATIVE_PATH_CONTEXT_RE.search(before):
            continue
        after = lowered[index + len(term):index + len(term) + 120]
        after = re.sub(r"^\.(?:md|rst|txt)\b", "", after)
        if re.match(
            r"\s*(?:must|should|shall|can|may)?\s*"
            r"(?:not|never|avoid|without)?\s*"
            r"(?:mention|include|contain|reference|refer\s+to|list|document|describe)\b",
            after,
        ):
            return True
    return False


def _is_positive_path_list_member(description: str, path_hint: str) -> bool:
    normalized = normalize_path_hint(path_hint or "")
    if not normalized:
        return False
    lowered = (description or "").lower()
    lower_hint = normalized.lower()
    search_terms = {lower_hint, os.path.basename(lower_hint)}
    for term in search_terms:
        start = 0
        while True:
            index = lowered.find(term, start)
            if index < 0:
                break
            colon_index = lowered.rfind(":", max(0, index - 260), index)
            if colon_index >= 0:
                prefix = lowered[max(0, colon_index - 180):colon_index]
                between = lowered[colon_index + 1:index]
                if POSITIVE_PATH_LIST_PREFIX_RE.search(prefix) and not NEGATIVE_PATH_CONTEXT_RE.search(between):
                    return True
            start = index + len(term)
    return False


def _is_reference_only_path_hint(description: str, path_hint: str) -> bool:
    """True when a path is only referenced or linked, not requested as output."""
    normalized = normalize_path_hint(path_hint or "")
    if not normalized:
        return False
    if _is_positive_path_list_member(description, normalized):
        return False
    escaped_hint = re.escape(normalized.lower())
    escaped_base = re.escape(os.path.basename(normalized).lower())
    path_pattern = rf"(?:{escaped_hint}|{escaped_base})"
    clauses = re.split(r"(?<=[;!?。；！？，])\s*|(?<=[.])\s+|[\n\r]+", description or "")
    for clause in clauses:
        lowered = clause.lower()
        match = re.search(path_pattern, lowered)
        if not match:
            continue
        if _is_positive_colon_list_item(clause, normalized):
            return False
        before = lowered[max(0, match.start() - 100):match.start()]
        positive_output = re.search(
            r"\b(create|write|produce|deliver|required|requires?|output|generate|implement|build|add)\b",
            before,
        )
        if positive_output:
            return False
        reference_only = re.search(
            r"\b(link|load|reference|referenced|referencing|import|href|src|stylesheet|script)\b",
            before,
        )
        if reference_only:
            return True
    return False


def extract_required_path_hints(description: str) -> List[str]:
    """Return a list of file-path candidates found in *description*."""
    forbidden_keys = {
        canonical_requirement_key(path_hint)
        for path_hint in extract_forbidden_path_hints(description)
    }
    candidates = [
        path_hint for path_hint in _collect_path_hints_from_text(description)
        if canonical_requirement_key(path_hint) not in forbidden_keys
        and not _is_reference_only_path_hint(description, path_hint)
    ]
    return dedupe_requirement_path_hints(candidates)


def split_candidate_list(raw: str) -> Iterable[str]:
    """Split on comma, Chinese comma, or whitespace."""
    for item in re.split(r"[,，、\s]+", raw):
        item = item.strip()
        if item:
            yield item


def normalize_path_hint(candidate: str) -> Optional[str]:
    """Normalize a path-hint candidate — strip quotes, backticks, leading ``./``."""
    candidate = candidate.strip().strip("`'\"").rstrip(".,:;!?。；，)]}")
    if not candidate:
        return None
    candidate = candidate.replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    return candidate or None


def expand_path_hint_alternatives(candidate: str) -> List[str]:
    """Expand same-level slash alternatives such as ``setup.py/pyproject.toml``.

    Real paths like ``app/main.py`` keep their directory component because the
    first segment is not itself a deliverable-like filename.  The expansion is
    deliberately narrow to avoid flattening normal nested project paths.
    """
    normalized = normalize_path_hint(candidate or "")
    if not normalized or normalized.startswith("/"):
        return [normalized] if normalized else []
    parts = [part for part in normalized.split("/") if part]
    if len(parts) < 2:
        return [normalized]
    if all("/" not in part and is_probable_deliverable_path(part) for part in parts):
        return parts
    return [normalized]


def is_runtime_data_path_hint(description: str, path_hint: str) -> bool:
    """Return True when *path_hint* is runtime storage, not a deliverable.

    Natural-language task contracts often mention files like ``todos.json`` as
    an implementation detail for persistence.  Those files are valid runtime
    data, but should not become required handoff deliverables unless the user
    explicitly asks to create or produce that data/config file.
    """
    normalized = normalize_path_hint(path_hint or "")
    if not normalized:
        return False

    ext = os.path.splitext(normalized)[1].lower()
    if ext not in RUNTIME_DATA_EXTENSIONS:
        return False

    lower_description = (description or "").lower()
    lower_hint = normalized.lower()
    hint_index = lower_description.find(lower_hint)
    if hint_index == -1:
        hint_index = lower_description.find(os.path.basename(lower_hint))
    if hint_index == -1:
        return False

    window = lower_description[max(0, hint_index - 180): hint_index + len(lower_hint) + 140]
    escaped_hint = re.escape(lower_hint)
    escaped_base = re.escape(os.path.basename(lower_hint))
    path_pattern = f"(?:{escaped_hint}|{escaped_base})"
    explicit_deliverable = (
        re.search(
            r"\b(create|write|generate|produce|output)\b.{0,50}" + path_pattern,
            window,
        )
        or re.search(
            r"\b(required\s+files?|required\s+deliverables?|deliverables?|file\s+named|named)\b.{0,60}" + path_pattern,
            window,
        )
    )
    if explicit_deliverable:
        return False
    return bool(
        re.search(
            r"\b(store|stored|storage|persist|persistence|runtime|local|data|database|cache|defaulting|defaults?)\b",
            window,
        )
    )


def is_auxiliary_deliverable_path_hint(description: str, path_hint: str) -> bool:
    """Return True for helper/scaffolding files that should not become final deliverables.

    Owner decomposition may create package markers or placeholders as a means to
    implement a feature.  Those files can exist in the project, but they should
    not become acceptance requirements unless the user explicitly requested them
    as handoff artifacts.
    """
    normalized = normalize_path_hint(path_hint or "")
    if not normalized:
        return False
    basename = os.path.basename(normalized).lower()
    if basename not in AUXILIARY_DELIVERABLE_BASENAMES:
        return False

    lowered = (description or "").lower()
    explicit_user_artifact = bool(
        re.search(r"\b(create|write|produce|deliver|required|file\s+named|named)\b.{0,60}" + re.escape(basename), lowered)
        and not re.search(r"\b(placeholder|scaffold|empty|package marker|test package)\b.{0,80}" + re.escape(basename), lowered)
    )
    return not explicit_user_artifact


def is_probable_deliverable_path(candidate: str) -> bool:
    """Heuristic: could *candidate* plausibly be a file path?"""
    lowered = candidate.lower()
    if lowered in IGNORED_FILELIKE_WORDS:
        return False
    if lowered.startswith(("http://", "https://")) or "://" in candidate:
        return False
    # Reject bare domains that look like ``example.com``
    if "/" not in candidate and "." in candidate:
        suffix = candidate.rsplit(".", 1)[-1].lower()
        if suffix in {"com", "cn", "net", "org", "io", "ai", "dev", "app"}:
            return False
        # Reject module-like dotted names such as http.server
        if lowered in MODULE_LIKE_DOTTED_NAMES:
            return False
        suffix_ext = os.path.splitext(candidate)[1].lower()
        if suffix_ext not in KNOWN_FILE_EXTENSIONS and candidate not in SPECIAL_DOTFILES:
            return False
    if candidate in SPECIAL_FILENAMES or candidate in SPECIAL_DOTFILES:
        return True
    if "/" in candidate:
        parts = [part for part in candidate.split("/") if part]
        if not parts:
            return False
        if _looks_like_absolute_system_path(parts):
            return False
        basename = parts[-1]
        if basename in SPECIAL_FILENAMES or basename in SPECIAL_DOTFILES:
            return True
        if "." in basename:
            return bool(re.search(r"\.[A-Za-z0-9][A-Za-z0-9_.-]*$", basename))
        return False
    return bool(re.search(r"\.[A-Za-z0-9][A-Za-z0-9_.-]*$", candidate))


def _looks_like_absolute_system_path(parts: List[str]) -> bool:
    """Reject absolute local paths after regex extraction drops the leading slash."""
    if len(parts) < 2:
        return False
    first = parts[0].lower()
    second = parts[1].lower() if len(parts) > 1 else ""
    if first == "tmp":
        return True
    if first == "var" and second == "folders":
        return True
    if first == "private" and second in {"tmp", "var"}:
        return True
    if first in {"users", "volumes", "applications", "system", "library", "usr", "opt", "etc"}:
        return True
    return False


# -- Artifact-type inference --------------------------------------------------


def infer_artifact_type(path_hint: str, description: str = "") -> str:
    """Map a path hint to a structured ``artifact_type``."""
    basename = os.path.basename(path_hint).lower()
    if basename in {"dockerfile", "containerfile"}:
        return "dockerfile"
    if basename in {"docker-compose.yml", "docker-compose.yaml"}:
        return "compose_config"
    if basename.startswith("readme"):
        return "documentation"
    if basename in CONFIG_FILENAMES:
        return "config_file"
    if basename.endswith(".py"):
        if "test" in basename or "/tests/" in f"/{path_hint}":
            return "test_source"
        return "api_service_source"
    if basename.endswith((".yml", ".yaml", ".toml", ".json", ".ini", ".env", ".example")):
        return "config_file"
    if basename.endswith((".md", ".txt", ".rst")):
        return "documentation"
    return "file"


def infer_quality_checks(
    description: str,
    deliverables: List[RequirementDeliverable],
) -> List[Tuple[str, str]]:
    """Infer project-level quality checks from the description and manifest."""
    checks: List[Tuple[str, str]] = [
        ("required_files_exist", "Verify every required manifest file exists.")
    ]
    lower = description.lower()
    if any(d.path_hint and d.path_hint.endswith(".py") for d in deliverables):
        checks.append(
            ("python_syntax_valid", "Verify generated Python files parse successfully.")
        )
    if "pytest" in lower or any(d.artifact_type == "test_source" for d in deliverables):
        checks.append(
            (
                "pytest_passes",
                "Run generated pytest tests when dependencies are installable.",
            )
        )
    if has_container_delivery_intent(description) or any(
        d.artifact_type in {"dockerfile", "compose_config"} for d in deliverables
    ):
        checks.append(
            (
                "container_config_exists",
                "Verify Dockerfile and compose config requested by the task exist.",
            )
        )
    return checks
