from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any


GOAL_CONTRACT_SCHEMA = "across-goal-contract/1.0"
GOAL_CHANGE_PROPOSAL_SCHEMA = "across-goal-change-proposal/1.0"

_EXECUTION_PROFILES = {"direct", "orchestrated", "workflow-pack"}
_REVIEW_POLICIES = {"automatic", "human", "independent_agent", "quality_gate", "security_policy"}
_PROPOSAL_OPERATIONS = {"add", "replace", "remove"}
_PROPOSAL_DECISIONS = {"pending", "accepted", "partially_accepted", "rejected", "superseded"}
_HOST_OWNED_PATHS = {"/confirmed_by", "/confirmed_at", "/revision", "/goal_id", "/task_id"}


def _is_host_owned_path(path: str) -> bool:
    return any(path == owned or path.startswith(f"{owned}/") for owned in _HOST_OWNED_PATHS)


def _normalized_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip()


def _required_text(value: Any, name: str) -> str:
    normalized = _normalized_text(value)
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _positive_revision(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{name} must be an array of non-empty strings")
    return value


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("goal protocol values must be canonical JSON") from exc


def criterion_id(description: str, validator_kind: str) -> str:
    description_value = _required_text(description, "description")
    validator_value = _required_text(validator_kind, "validator_kind").lower()
    digest = hashlib.sha256(f"{validator_value}\n{description_value}".encode("utf-8")).hexdigest()
    return f"criterion-{digest[:16]}"


def stable_goal_hash(value: Mapping[str, Any]) -> str:
    payload = _canonical_json(_mapping(value, "value"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_goal_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    contract = copy.deepcopy(dict(_mapping(value, "goal contract")))
    if contract.get("schema_version") != GOAL_CONTRACT_SCHEMA:
        raise ValueError(f"schema_version must be {GOAL_CONTRACT_SCHEMA}")
    _required_text(contract.get("goal_id"), "goal_id")
    _positive_revision(contract.get("revision"), "revision")
    _required_text(contract.get("task_id"), "task_id")
    _required_text(contract.get("statement"), "statement")
    _required_text(contract.get("success_outcome"), "success_outcome")
    scope = _mapping(contract.get("scope"), "scope")
    _string_list(scope.get("includes"), "scope.includes")
    _string_list(scope.get("excludes"), "scope.excludes")
    criteria = contract.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("acceptance_criteria must be a non-empty array")
    seen: set[str] = set()
    for index, raw_criterion in enumerate(criteria):
        criterion = _mapping(raw_criterion, f"acceptance_criteria[{index}]")
        identifier = _required_text(criterion.get("criterion_id"), "criterion_id")
        if identifier in seen:
            raise ValueError(f"duplicate criterion_id: {identifier}")
        seen.add(identifier)
        _required_text(criterion.get("description"), "criterion description")
        if not isinstance(criterion.get("required"), bool):
            raise ValueError("criterion required must be a boolean")
        _required_text(criterion.get("validator_kind"), "validator_kind")
        if criterion.get("review_policy") not in _REVIEW_POLICIES:
            raise ValueError("criterion review_policy is invalid")
        _required_text(criterion.get("source"), "criterion source")
    if not isinstance(contract.get("dependencies"), list):
        raise ValueError("dependencies must be an array")
    if contract.get("execution_profile") not in _EXECUTION_PROFILES:
        raise ValueError("execution_profile is invalid")
    _required_text(contract.get("source"), "source")
    if bool(contract.get("confirmed_by")) != bool(contract.get("confirmed_at")):
        raise ValueError("confirmed_by and confirmed_at must be supplied together")
    _required_text(contract.get("created_at"), "created_at")
    _canonical_json(contract)
    return contract


def normalize_goal_change_proposal(value: Mapping[str, Any]) -> dict[str, Any]:
    proposal = copy.deepcopy(dict(_mapping(value, "goal change proposal")))
    if proposal.get("schema_version") != GOAL_CHANGE_PROPOSAL_SCHEMA:
        raise ValueError(f"schema_version must be {GOAL_CHANGE_PROPOSAL_SCHEMA}")
    _required_text(proposal.get("proposal_id"), "proposal_id")
    _required_text(proposal.get("goal_id"), "goal_id")
    _positive_revision(proposal.get("base_goal_revision"), "base_goal_revision")
    _required_text(proposal.get("proposed_by"), "proposed_by")
    _required_text(proposal.get("reason"), "reason")
    operations = proposal.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("operations must be a non-empty array")
    for index, raw_operation in enumerate(operations):
        operation = _mapping(raw_operation, f"operations[{index}]")
        if operation.get("op") not in _PROPOSAL_OPERATIONS:
            raise ValueError("proposal operation is invalid")
        path = _required_text(operation.get("path"), "operation path")
        if not path.startswith("/") or _is_host_owned_path(path):
            raise ValueError("proposal operation targets host-owned fields")
        if operation.get("op") != "remove" and "value" not in operation:
            raise ValueError("proposal operation value is required")
    impact = _mapping(proposal.get("impact_summary"), "impact_summary")
    _string_list(impact.get("goal_ids"), "impact_summary.goal_ids")
    _string_list(impact.get("criterion_ids"), "impact_summary.criterion_ids")
    _string_list(impact.get("evidence_ids"), "impact_summary.evidence_ids")
    if not isinstance(impact.get("requires_revalidation"), bool):
        raise ValueError("impact_summary.requires_revalidation must be a boolean")
    _mapping(proposal.get("risk_summary"), "risk_summary")
    _mapping(proposal.get("estimated_cost"), "estimated_cost")
    _string_list(proposal.get("alternatives"), "alternatives")
    if proposal.get("decision_state") not in _PROPOSAL_DECISIONS:
        raise ValueError("decision_state is invalid")
    _required_text(proposal.get("created_at"), "created_at")
    _canonical_json(proposal)
    return proposal
