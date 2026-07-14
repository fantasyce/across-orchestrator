from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Protocol, runtime_checkable
import hashlib
import json
import math
import os
import platform
import signal
import stat
import subprocess
import threading
import time

from .redaction import redact_sensitive_value

SANDBOX_POLICY_SCHEMA = "across-sandbox-policy/1.0"
SANDBOX_EVIDENCE_SCHEMA = "across-sandbox-evidence/1.0"
SANDBOX_EXECUTION_SCHEMA = "across-sandbox-execution/1.0"
SANDBOX_PROVIDER_CONTRACT = "across-sandbox-provider/1.0"

VALID_NETWORK_MODES = {"none", "adapter_scoped", "allowlist", "unrestricted_requires_approval"}
VALID_FILESYSTEM_MODES = {"read_only", "run_scoped", "candidate_workspace_only", "allowlist"}
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024
MAX_TIMEOUT_SECONDS = 3600.0
DEFAULT_MAX_WALL_TIMEOUT_SECONDS = MAX_TIMEOUT_SECONDS
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_STATE_ROOTS = 32
MAX_RUNTIME_STATE_FILES = 32


@dataclass(frozen=True)
class SandboxExecutionRequest:
    argv: tuple[str, ...]
    cwd: Path
    workspace_root: Path
    policy: dict[str, Any]
    timeout_seconds: float
    max_output_bytes: int
    refresh_timeout_on_output: bool = False
    max_wall_timeout_seconds: float = DEFAULT_MAX_WALL_TIMEOUT_SECONDS
    environment: dict[str, str] | None = None
    cancellation: Any | None = None


@runtime_checkable
class SandboxProvider(Protocol):
    """Dependency-free adapter contract for local or remote sandbox runtimes."""

    provider_id: str

    def capabilities(self) -> dict[str, Any]: ...

    def execute(self, request: SandboxExecutionRequest) -> dict[str, Any]: ...


class SandboxProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, SandboxProvider] = {}

    def register(self, provider: SandboxProvider, *, replace: bool = False) -> None:
        provider_id = str(getattr(provider, "provider_id", "")).strip()
        if not provider_id:
            raise ValueError("sandbox provider_id must be a non-empty string")
        if not isinstance(provider, SandboxProvider):
            raise TypeError("sandbox provider must implement capabilities() and execute()")
        if provider_id in self._providers and not replace:
            raise ValueError(f"sandbox provider is already registered: {provider_id}")
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> SandboxProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise ValueError(f"unknown sandbox provider: {provider_id}") from exc

    def list(self) -> list[dict[str, Any]]:
        return [
            {"provider_id": provider_id, **provider.capabilities()}
            for provider_id, provider in sorted(self._providers.items())
        ]


class LocalWorkspaceSandboxProvider:
    provider_id = "local-workspace"

    def capabilities(self) -> dict[str, Any]:
        native_backend = self._native_backend()
        network_modes = ["none"]
        if native_backend == "macos-sandbox-exec":
            network_modes.append("adapter_scoped")
        return {
            "contract_version": SANDBOX_PROVIDER_CONTRACT,
            "runtime": "local",
            "transport": "in_process",
            "network_modes": network_modes,
            "filesystem_modes": sorted(VALID_FILESYSTEM_MODES),
            "native_policy_backend": native_backend,
        }

    def execute(self, request: SandboxExecutionRequest) -> dict[str, Any]:
        started = time.monotonic()
        command, enforcement = self._sandboxed_command(request)
        env = _execution_environment(request)
        process = subprocess.Popen(
            command,
            cwd=request.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
        stdout = _BoundedOutput(request.max_output_bytes)
        stderr = _BoundedOutput(request.max_output_bytes)
        output_activity = _OutputActivity(started)
        stdout_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stdout, stdout, output_activity),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stderr, stderr, output_activity),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        timeout_kind: str | None = None
        fixed_deadline = started + request.timeout_seconds
        max_wall_deadline = started + request.max_wall_timeout_seconds
        while True:
            if request.cancellation is not None and request.cancellation.is_cancelled():
                _terminate_process_group(process)
                category = (
                    request.cancellation.category()
                    if hasattr(request.cancellation, "category")
                    else None
                )
                from .cancellation import ActionCancelledError

                raise ActionCancelledError(request.cancellation.reason(), category=category)
            now = time.monotonic()
            if request.refresh_timeout_on_output:
                idle_deadline = output_activity.last_activity() + request.timeout_seconds
                deadline = min(idle_deadline, max_wall_deadline)
            else:
                deadline = min(fixed_deadline, max_wall_deadline)
            remaining = deadline - now
            if remaining <= 0:
                timed_out = True
                timeout_kind = (
                    "max_wall"
                    if now >= max_wall_deadline or not request.refresh_timeout_on_output
                    else "idle"
                )
                _terminate_process_group(process)
                exit_code = process.wait()
                break
            try:
                exit_code = process.wait(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        duration_ms = int((time.monotonic() - started) * 1000)
        return _execution_receipt(
            request,
            provider_id=self.provider_id,
            exit_code=exit_code,
            timed_out=timed_out,
            timeout_kind=timeout_kind,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            enforcement=enforcement,
        )

    def _native_backend(self) -> str:
        if platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file():
            return "macos-sandbox-exec"
        return "policy-boundary"

    def _sandboxed_command(self, request: SandboxExecutionRequest) -> tuple[list[str], dict[str, Any]]:
        backend = self._native_backend()
        if backend != "macos-sandbox-exec":
            return list(request.argv), {
                "backend": backend,
                "argv_without_shell": True,
                "workspace_boundary": "validated",
                "network_policy": "declared_not_kernel_isolated",
                "filesystem_policy": "declared_not_kernel_isolated",
            }
        profile = _macos_sandbox_profile(request)
        return ["/usr/bin/sandbox-exec", "-p", profile, "--", *request.argv], {
            "backend": backend,
            "argv_without_shell": True,
            "workspace_boundary": "kernel_enforced",
            "network_policy": "kernel_enforced",
            "filesystem_policy": "kernel_enforced",
        }


class _BoundedOutput:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.total_bytes = 0

    @property
    def truncated(self) -> bool:
        return self.total_bytes > len(self.data)

    def append(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        remaining = self.limit - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


class _OutputActivity:
    def __init__(self, started: float) -> None:
        self._last_activity = started
        self._lock = threading.Lock()

    def record(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()

    def last_activity(self) -> float:
        with self._lock:
            return self._last_activity


def get_sandbox_provider_registry() -> SandboxProviderRegistry:
    return _SANDBOX_PROVIDERS


def execute_sandbox_command(
    policy: dict[str, Any] | None,
    *,
    command: list[str],
    cwd: str,
    provider_id: str = "local-workspace",
    timeout_seconds: float | None = None,
    refresh_timeout_on_output: bool | None = None,
    max_wall_timeout_seconds: float | None = None,
    max_output_bytes: int | None = None,
    environment: dict[str, str] | None = None,
    cancellation: Any | None = None,
    registry: SandboxProviderRegistry | None = None,
) -> dict[str, Any]:
    """Validate policy boundaries and execute argv through a registered provider."""

    evaluation = evaluate_sandbox_policy(policy or {}, command=command, cwd=cwd)
    normalized = evaluation["policy"]
    if evaluation["status"] != "passed":
        return _blocked_execution_receipt(evaluation, provider_id=provider_id)
    workspace_root = Path(normalized["workspace_root"]).expanduser().resolve(strict=True)
    cwd_path = Path(cwd).expanduser().resolve(strict=True)
    if not workspace_root.is_dir() or not cwd_path.is_dir():
        raise ValueError("workspace_root and cwd must be existing directories")
    limits = dict(normalized.get("execution") or {})
    timeout = _bounded_float(
        timeout_seconds if timeout_seconds is not None else limits.get("timeout_seconds"),
        default=DEFAULT_TIMEOUT_SECONDS,
        minimum=0.01,
        maximum=MAX_TIMEOUT_SECONDS,
        name="timeout_seconds",
    )
    refresh_timeout = _bounded_bool(
        refresh_timeout_on_output
        if refresh_timeout_on_output is not None
        else limits.get("refresh_timeout_on_output"),
        default=False,
        name="refresh_timeout_on_output",
    )
    max_wall_timeout = _bounded_float(
        max_wall_timeout_seconds
        if max_wall_timeout_seconds is not None
        else limits.get("max_wall_timeout_seconds"),
        default=DEFAULT_MAX_WALL_TIMEOUT_SECONDS,
        minimum=0.01,
        maximum=MAX_TIMEOUT_SECONDS,
        name="max_wall_timeout_seconds",
    )
    output_limit = _bounded_int(
        max_output_bytes if max_output_bytes is not None else limits.get("max_output_bytes"),
        default=DEFAULT_MAX_OUTPUT_BYTES,
        minimum=1,
        maximum=MAX_OUTPUT_BYTES,
        name="max_output_bytes",
    )
    request = SandboxExecutionRequest(
        argv=tuple(str(item) for item in command),
        cwd=cwd_path,
        workspace_root=workspace_root,
        policy=normalized,
        timeout_seconds=timeout,
        refresh_timeout_on_output=refresh_timeout,
        max_wall_timeout_seconds=max_wall_timeout,
        max_output_bytes=output_limit,
        environment={str(key): str(value) for key, value in (environment or {}).items()},
        cancellation=cancellation,
    )
    if normalized["filesystem_policy"]["mode"] == "allowlist":
        try:
            for root in normalized["filesystem_policy"]["allowlist_roots"]:
                _validated_write_root(root, workspace_root)
        except (FileNotFoundError, ValueError) as exc:
            return _provider_policy_blocked_receipt(request, provider_id, str(exc))
    provider = (registry or _SANDBOX_PROVIDERS).get(provider_id)
    capabilities = provider.capabilities()
    network_mode = normalized["network_policy"]["mode"]
    filesystem_mode = normalized["filesystem_policy"]["mode"]
    if network_mode not in capabilities.get("network_modes", []):
        return _provider_policy_blocked_receipt(
            request,
            provider_id,
            f"provider does not enforce network policy: {network_mode}",
        )
    if filesystem_mode not in capabilities.get("filesystem_modes", []):
        return _provider_policy_blocked_receipt(
            request,
            provider_id,
            f"provider does not enforce filesystem policy: {filesystem_mode}",
        )
    try:
        return redact_sensitive_value(provider.execute(request))
    except FileNotFoundError:
        return _error_execution_receipt(request, provider_id, "command_not_found", "sandbox command was not found")
    except PermissionError:
        return _error_execution_receipt(request, provider_id, "permission_denied", "sandbox command permission was denied")


def evaluate_sandbox_policy(
    policy: dict[str, Any] | None,
    *,
    command: list[str] | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Evaluate a sandbox policy without executing the command."""

    normalized = normalize_sandbox_policy(policy or {})
    checks: list[dict[str, Any]] = []
    blocked_reasons: list[str] = []

    network_mode = normalized["network_policy"]["mode"]
    filesystem_mode = normalized["filesystem_policy"]["mode"]
    if network_mode not in VALID_NETWORK_MODES:
        blocked_reasons.append(f"unsupported network policy: {network_mode}")
    if filesystem_mode not in VALID_FILESYSTEM_MODES:
        blocked_reasons.append(f"unsupported filesystem policy: {filesystem_mode}")

    checks.append(_check("network_policy", network_mode in VALID_NETWORK_MODES, {"mode": network_mode}))
    checks.append(_check("filesystem_policy", filesystem_mode in VALID_FILESYSTEM_MODES, {"mode": filesystem_mode}))
    try:
        runtime_state_roots = _validated_runtime_state_roots(
            normalized["filesystem_policy"]["runtime_state_roots"],
            normalized.get("workspace_root"),
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        runtime_state_roots = []
        blocked_reasons.append(str(exc))
        checks.append(_check("runtime_state_roots", False, reason=str(exc)))
    else:
        normalized["filesystem_policy"]["runtime_state_roots"] = [str(root) for root in runtime_state_roots]
        checks.append(
            _check(
                "runtime_state_roots",
                True,
                _runtime_state_roots_summary(runtime_state_roots),
            )
        )
    try:
        runtime_state_files = _validated_runtime_state_files(
            normalized["filesystem_policy"]["runtime_state_files"],
            runtime_state_roots,
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        runtime_state_files = []
        blocked_reasons.append(str(exc))
        checks.append(_check("runtime_state_files", False, reason=str(exc)))
    else:
        normalized["filesystem_policy"]["runtime_state_files"] = [str(path) for path in runtime_state_files]
        checks.append(
            _check(
                "runtime_state_files",
                True,
                _runtime_state_files_summary(runtime_state_files),
            )
        )
    checks.append(_check("promotion_block", normalized["promotion"]["merge_release_signing_blocked"] is True))
    checks.append(_check("human_approval", normalized["promotion"]["human_approval_required"] is True))

    model_budget = int(normalized["budget"]["max_model_calls"])
    checks.append(_check("model_budget", model_budget >= 0 and model_budget <= 100, {"max_model_calls": model_budget}))

    if command is not None:
        command_result = _evaluate_command(normalized, command, cwd)
        checks.extend(command_result["checks"])
        blocked_reasons.extend(command_result["blocked_reasons"])

    for check in checks:
        if check["status"] == "blocked" and check["reason"] not in blocked_reasons:
            blocked_reasons.append(check["reason"])

    return {
        "schema_version": SANDBOX_EVIDENCE_SCHEMA,
        "policy_schema_version": normalized["schema_version"],
        "status": "blocked" if blocked_reasons else "passed",
        "policy": normalized,
        "command": command or None,
        "cwd": cwd or None,
        "checks": checks,
        "blocked_reasons": blocked_reasons,
        "execution": {
            "performed": False,
            "reason": "sandbox-probe validates policy and command boundaries only",
        },
    }


def normalize_sandbox_policy(policy: dict[str, Any]) -> dict[str, Any]:
    runtime_policy = dict(policy.get("runtime_policy") or {})
    source = {**runtime_policy, **policy}
    network = _policy_object(source.get("network_policy"), source.get("network") or "none")
    filesystem = _policy_object(source.get("filesystem_policy"), source.get("filesystem") or "read_only")
    budget = dict(source.get("budget") or {})
    promotion = dict(source.get("promotion") or {})
    execution = dict(source.get("execution") or {})
    return {
        "schema_version": SANDBOX_POLICY_SCHEMA,
        "risk_profile": str(source.get("risk_profile") or "low"),
        "network_policy": {
            "mode": str(network.get("mode") or "none"),
            "allowlist": [str(item) for item in network.get("allowlist") or []],
        },
        "filesystem_policy": {
            "mode": str(filesystem.get("mode") or "read_only"),
            "allowlist_roots": [str(item) for item in filesystem.get("allowlist_roots") or filesystem.get("allowlist") or []],
            "runtime_state_roots": _runtime_state_root_values(filesystem.get("runtime_state_roots")),
            "runtime_state_files": _runtime_state_file_values(filesystem.get("runtime_state_files")),
        },
        "budget": {
            "max_model_calls": int(budget.get("max_model_calls") or 0),
            "max_candidate_repairs": int(budget.get("max_candidate_repairs") or 0),
            "max_usd": float(budget.get("max_usd") or 0),
        },
        "execution": {
            "timeout_seconds": float(
                execution["timeout_seconds"]
                if execution.get("timeout_seconds") is not None
                else DEFAULT_TIMEOUT_SECONDS
            ),
            "refresh_timeout_on_output": _bounded_bool(
                execution.get("refresh_timeout_on_output"),
                default=False,
                name="refresh_timeout_on_output",
            ),
            "max_wall_timeout_seconds": _bounded_float(
                execution.get("max_wall_timeout_seconds"),
                default=DEFAULT_MAX_WALL_TIMEOUT_SECONDS,
                minimum=0.01,
                maximum=MAX_TIMEOUT_SECONDS,
                name="max_wall_timeout_seconds",
            ),
            "max_output_bytes": int(
                execution["max_output_bytes"]
                if execution.get("max_output_bytes") is not None
                else DEFAULT_MAX_OUTPUT_BYTES
            ),
        },
        "promotion": {
            "human_approval_required": promotion.get("human_approval_required") is not False,
            "merge_release_signing_blocked": promotion.get("merge_release_signing_blocked") is not False,
        },
        "workspace_root": str(source.get("workspace_root") or source.get("workspace") or ""),
        "command_allowlist": [_command_key(item) for item in source.get("command_allowlist") or []],
    }


def _evaluate_command(policy: dict[str, Any], command: list[str], cwd: str | None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    blocked_reasons: list[str] = []
    clean_command = [str(item) for item in command if str(item).strip()]
    if not clean_command:
        reason = "command must be a non-empty argv array"
        return {"checks": [_check("command_shape", False, reason=reason)], "blocked_reasons": [reason]}

    allowlist = set(policy.get("command_allowlist") or [])
    command_key = _command_key(clean_command)
    command_allowed = bool(allowlist) and (command_key in allowlist or clean_command[0] in allowlist)
    if not command_allowed:
        reason = f"command is not allowlisted: {command_key}"
        blocked_reasons.append(reason)
    checks.append(_check("command_allowlist", command_allowed, {"command": command_key}, reason=None if command_allowed else reason))

    workspace_root = str(policy.get("workspace_root") or "")
    if cwd and workspace_root:
        try:
            cwd_path = Path(cwd).expanduser().resolve(strict=False)
            root_path = Path(workspace_root).expanduser().resolve(strict=False)
        except (RuntimeError, OSError):
            cwd_path = _normalize_boundary_path(cwd)
            root_path = _normalize_boundary_path(workspace_root)
        inside = cwd_path == root_path or root_path in cwd_path.parents
        if not inside:
            reason = "cwd must stay inside workspace_root"
            blocked_reasons.append(reason)
        checks.append(_check("cwd_boundary", inside, {"cwd": str(cwd_path), "workspace_root": str(root_path)}, reason=None if inside else reason))
    elif cwd and not workspace_root:
        reason = "workspace_root is required when cwd is supplied"
        blocked_reasons.append(reason)
        checks.append(_check("cwd_boundary", False, reason=reason))

    return {"checks": checks, "blocked_reasons": blocked_reasons}


def _macos_sandbox_profile(request: SandboxExecutionRequest) -> str:
    network_mode = request.policy["network_policy"]["mode"]
    filesystem = request.policy["filesystem_policy"]
    lines = [
        "(version 1)",
        "(deny default)",
        "(import \"system.sb\")",
        "(allow process-exec process-fork)",
        "(allow file-read*)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
    ]
    if network_mode == "none":
        lines.append("(deny network*)")
    elif network_mode == "adapter_scoped":
        lines.append("(allow network-outbound)")
    filesystem_mode = filesystem["mode"]
    if filesystem_mode != "read_only":
        roots = [request.workspace_root]
        if filesystem_mode == "allowlist":
            roots = [_validated_write_root(item, request.workspace_root) for item in filesystem["allowlist_roots"]]
        for root in roots:
            lines.append(f'(allow file-write* (subpath "{_sandbox_quote(str(root))}"))')
    runtime_state_roots = _validated_runtime_state_roots(
        filesystem["runtime_state_roots"],
        request.workspace_root,
    )
    for root in runtime_state_roots:
        lines.append(f'(allow file-write* (subpath "{_sandbox_quote(str(root))}"))')
    runtime_state_files = _validated_runtime_state_files(
        filesystem["runtime_state_files"],
        runtime_state_roots,
    )
    for path in runtime_state_files:
        lines.append(f'(allow file-write* (literal "{_sandbox_quote(str(path))}"))')
    return "\n".join(lines)


def _validated_write_root(value: str, workspace_root: Path) -> Path:
    root = Path(value).expanduser().resolve(strict=True)
    if root != workspace_root and workspace_root not in root.parents:
        raise ValueError("filesystem allowlist roots must stay inside workspace_root")
    return root


def _runtime_state_root_values(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("runtime_state_roots must be an array of absolute directory paths")
    if len(value) > MAX_RUNTIME_STATE_ROOTS:
        raise ValueError(f"runtime_state_roots must contain at most {MAX_RUNTIME_STATE_ROOTS} paths")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("runtime_state_roots entries must be non-empty strings")
    return list(value)


def _runtime_state_file_values(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("runtime_state_files must be an array of absolute file paths")
    if len(value) > MAX_RUNTIME_STATE_FILES:
        raise ValueError(f"runtime_state_files must contain at most {MAX_RUNTIME_STATE_FILES} paths")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("runtime_state_files entries must be non-empty strings")
    return list(value)


def _validated_runtime_state_roots(
    values: list[str],
    workspace_root: str | Path | None,
) -> list[Path]:
    if not values:
        return []
    try:
        workspace = (
            Path(workspace_root).expanduser().resolve(strict=True)
            if workspace_root
            else None
        )
    except (FileNotFoundError, OSError, RuntimeError):
        raise ValueError("workspace_root must be an existing directory") from None
    roots: list[Path] = []
    for value in values:
        expanded = Path(value).expanduser()
        if not expanded.is_absolute() or "\x00" in value:
            raise ValueError("runtime_state_roots entries must be absolute directory paths")
        try:
            root = expanded.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            raise ValueError("runtime_state_roots entries must be existing directories") from None
        if not root.is_dir():
            raise ValueError("runtime_state_roots entries must be existing directories")
        if root == Path(root.anchor):
            raise ValueError("runtime_state_roots cannot include a filesystem root")
        if workspace is not None and root in workspace.parents:
            raise ValueError("runtime_state_roots cannot contain workspace_root")
        if any(root == existing or root in existing.parents or existing in root.parents for existing in roots):
            raise ValueError("runtime_state_roots cannot overlap")
        roots.append(root)
    return roots


def _runtime_state_roots_summary(roots: list[Path]) -> dict[str, Any]:
    canonical_roots = sorted(str(root) for root in roots)
    return {
        "count": len(canonical_roots),
        "sha256": _json_hash(canonical_roots),
    }


def _validated_runtime_state_files(
    values: list[str],
    runtime_state_roots: list[Path],
) -> list[Path]:
    files: list[Path] = []
    file_identities: set[tuple[int, int]] = set()
    for value in values:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute() or "\x00" in value:
            raise ValueError("runtime_state_files entries must be absolute file paths")
        if candidate == Path(candidate.anchor):
            raise ValueError("runtime_state_files cannot include a filesystem root")
        try:
            file_stat = candidate.lstat()
            path = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            raise ValueError("runtime_state_files entries must be existing regular files") from None
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("runtime_state_files entries must be existing regular files")
        identity = (file_stat.st_dev, file_stat.st_ino)
        if path in files or identity in file_identities:
            raise ValueError("runtime_state_files cannot overlap")
        if any(path == root or root in path.parents for root in runtime_state_roots):
            raise ValueError("runtime_state_files cannot overlap runtime_state_roots")
        files.append(path)
        file_identities.add(identity)
    return files


def _runtime_state_files_summary(files: list[Path]) -> dict[str, Any]:
    canonical_files = sorted(str(path) for path in files)
    return {
        "count": len(canonical_files),
        "sha256": _json_hash(canonical_files),
    }


def _sandbox_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _execution_environment(request: SandboxExecutionRequest) -> dict[str, str]:
    # Preserve the legacy command-adapter environment without recording it in evidence.
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ACROSS_SANDBOX_NETWORK_POLICY"] = request.policy["network_policy"]["mode"]
    env["ACROSS_SANDBOX_FILESYSTEM_POLICY"] = request.policy["filesystem_policy"]["mode"]
    env.update(request.environment or {})
    return env


def _drain_stream(stream: Any, output: _BoundedOutput, activity: _OutputActivity) -> None:
    if stream is None:
        return
    try:
        while True:
            read = getattr(stream, "read1", stream.read)
            chunk = read(64 * 1024)
            if not chunk:
                return
            output.append(chunk)
            activity.record()
    finally:
        stream.close()


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if not hasattr(os, "killpg"):
        process.terminate()
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=0.25)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _execution_receipt(
    request: SandboxExecutionRequest,
    *,
    provider_id: str,
    exit_code: int,
    timed_out: bool,
    timeout_kind: str | None,
    duration_ms: int,
    stdout: _BoundedOutput,
    stderr: _BoundedOutput,
    enforcement: dict[str, Any],
) -> dict[str, Any]:
    status = "timed_out" if timed_out else ("completed" if exit_code == 0 else "failed")
    runtime_state_roots = _validated_runtime_state_roots(
        request.policy["filesystem_policy"]["runtime_state_roots"],
        request.workspace_root,
    )
    runtime_state_files = _validated_runtime_state_files(
        request.policy["filesystem_policy"]["runtime_state_files"],
        runtime_state_roots,
    )
    receipt = {
        "schema_version": SANDBOX_EXECUTION_SCHEMA,
        "status": status,
        "provider": {"provider_id": provider_id, "contract_version": SANDBOX_PROVIDER_CONTRACT},
        "command": _command_receipt(request.argv),
        "workspace": {
            "workspace_sha256": _text_hash(str(request.workspace_root)),
            "cwd_sha256": _text_hash(str(request.cwd)),
            "cwd_within_workspace": True,
        },
        "policy": {
            "schema_version": request.policy["schema_version"],
            "network_mode": request.policy["network_policy"]["mode"],
            "filesystem_mode": request.policy["filesystem_policy"]["mode"],
            "runtime_state_roots": _runtime_state_roots_summary(runtime_state_roots),
            "runtime_state_files": _runtime_state_files_summary(runtime_state_files),
            "policy_sha256": _json_hash(request.policy),
        },
        "execution": {
            "performed": True,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "timeout_kind": timeout_kind,
            "timeout_seconds": request.timeout_seconds,
            "refresh_timeout_on_output": request.refresh_timeout_on_output,
            "max_wall_timeout_seconds": request.max_wall_timeout_seconds,
            "duration_ms": duration_ms,
        },
        "output": {
            "encoding": "utf-8",
            "max_bytes_per_stream": request.max_output_bytes,
            "stdout": stdout.text(),
            "stderr": stderr.text(),
            "stdout_bytes": stdout.total_bytes,
            "stderr_bytes": stderr.total_bytes,
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
        },
        "enforcement": enforcement,
    }
    return _finalize_execution_receipt(receipt)


def _blocked_execution_receipt(evaluation: dict[str, Any], *, provider_id: str) -> dict[str, Any]:
    policy = dict(evaluation.get("policy") or {})
    blocked_reasons = [_public_block_reason(reason) for reason in evaluation.get("blocked_reasons") or []]
    return _finalize_execution_receipt({
        "schema_version": SANDBOX_EXECUTION_SCHEMA,
        "status": "blocked",
        "provider": {"provider_id": provider_id, "contract_version": SANDBOX_PROVIDER_CONTRACT},
        "command": _command_receipt(tuple(evaluation.get("command") or [])),
        "execution": {"performed": False, "timed_out": False, "timeout_kind": None},
        "blocked_reasons": blocked_reasons,
        "policy_evidence": {
            "schema_version": evaluation.get("schema_version"),
            "policy_schema_version": evaluation.get("policy_schema_version"),
            "status": evaluation.get("status"),
            "network_mode": (policy.get("network_policy") or {}).get("mode"),
            "filesystem_mode": (policy.get("filesystem_policy") or {}).get("mode"),
            "checks": [
                {
                    "id": check.get("id"),
                    "status": check.get("status"),
                    "reason": _public_block_reason(check.get("reason")),
                }
                for check in evaluation.get("checks") or []
            ],
        },
    })


def _public_block_reason(reason: Any) -> str:
    text = str(reason or "blocked")
    if text.startswith("command is not allowlisted:"):
        return "command is not allowlisted"
    if text.startswith("workspace boundary cannot be resolved:"):
        return "workspace boundary cannot be resolved"
    return text


def _error_execution_receipt(
    request: SandboxExecutionRequest,
    provider_id: str,
    category: str,
    message: str,
) -> dict[str, Any]:
    return _finalize_execution_receipt({
        "schema_version": SANDBOX_EXECUTION_SCHEMA,
        "status": "error",
        "provider": {"provider_id": provider_id, "contract_version": SANDBOX_PROVIDER_CONTRACT},
        "command": _command_receipt(request.argv),
        "execution": {"performed": False, "timed_out": False, "timeout_kind": None},
        "error": {"category": category, "message": message},
    })


def _provider_policy_blocked_receipt(
    request: SandboxExecutionRequest,
    provider_id: str,
    reason: str,
) -> dict[str, Any]:
    return _finalize_execution_receipt({
        "schema_version": SANDBOX_EXECUTION_SCHEMA,
        "status": "blocked",
        "provider": {"provider_id": provider_id, "contract_version": SANDBOX_PROVIDER_CONTRACT},
        "command": _command_receipt(request.argv),
        "execution": {"performed": False, "timed_out": False, "timeout_kind": None},
        "blocked_reasons": [reason],
    })


def _finalize_execution_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    safe_receipt = redact_sensitive_value(receipt)
    safe_receipt["receipt_sha256"] = _json_hash(safe_receipt)
    return safe_receipt


def _command_receipt(argv: tuple[str, ...]) -> dict[str, Any]:
    return {
        "executable": Path(argv[0]).name if argv else None,
        "argument_count": max(0, len(argv) - 1),
        "argv_sha256": _json_hash(list(argv)),
    }


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float, name: str) -> float:
    result = default if value is None else float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _bounded_bool(value: Any, *, default: bool, name: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int, name: str) -> int:
    result = default if value is None else int(value)
    if result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _json_hash(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_boundary_path(value: str) -> PurePath:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        raise ValueError("path must be a non-empty string without null bytes")
    return PurePath(os.path.abspath(os.path.expanduser(text)))


def _policy_object(value: Any, fallback_mode: str) -> dict[str, Any]:
    if isinstance(value, str):
        return {"mode": value}
    if isinstance(value, dict):
        return {**value, "mode": value.get("mode") or fallback_mode}
    return {"mode": fallback_mode}


def _command_key(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value if str(item).strip())
    return str(value)


def _check(check_id: str, passed: bool, details: dict[str, Any] | None = None, *, reason: str | None = None) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": "passed" if passed else "blocked",
        "reason": reason or ("passed" if passed else check_id),
        "details": details or {},
    }


_SANDBOX_PROVIDERS = SandboxProviderRegistry()
_SANDBOX_PROVIDERS.register(LocalWorkspaceSandboxProvider())
