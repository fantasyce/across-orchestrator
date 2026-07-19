from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping
import json
import os
import platform
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import pwd
import psutil

from .coordinator import CoordinatorError, safe_cleanup_plan
from .worker_protocol import ArtifactDescriptor, CapabilityManifest, JobLease, JobManifest, ProtocolError, canonical_json, new_protocol_id, normalize_relative_path, payload_hash, sanitize_public


WORKER_VERSION = "0.10.0"
DEFAULT_WORKER_HOME = Path("~/.across/worker")
_DENIED_ENV_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "OPENAI_", "ANTHROPIC_", "GITHUB_", "SSH_")
_DENIED_ENV_NAMES = {"HOME", "USERPROFILE", "XDG_CONFIG_HOME", "DOCKER_CONFIG", "KUBECONFIG", "GNUPGHOME"}


def worker_home(env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    raw = source.get("ACROSS_WORKER_HOME") or str(DEFAULT_WORKER_HOME)
    target = Path(os.path.expanduser(raw)).resolve()
    home = Path(source.get("HOME") or Path.home()).expanduser().resolve()
    if target in {Path("/"), home} or target.parent == Path("/"):
        raise ProtocolError("worker home cannot be a user or system root")
    return target


def probe_capabilities(*, node_id: str, home: str | Path | None = None, executors: Iterable[str] | None = None) -> CapabilityManifest:
    root = Path(home).expanduser().resolve() if home else worker_home()
    root.mkdir(parents=True, exist_ok=True)
    system = platform.system().lower()
    os_name = "macos" if system == "darwin" else "linux" if system == "linux" else system
    architecture = platform.machine().lower()
    if architecture in {"aarch64", "arm64"}:
        architecture = "arm64"
    elif architecture in {"amd64", "x86_64"}:
        architecture = "x86_64"
    else:
        architecture = "unsupported"
    memory = _physical_memory_bytes()
    disk = shutil.disk_usage(root).free
    available_executors = tuple(executors or ("bounded-process",))
    isolation = "isolated" if "oci-container" in available_executors else "bounded"
    workflow_runtimes: list[str] = []
    for pointer in sorted((root / "packs").glob("*/current.json")):
        value = _read_json_if_present(pointer)
        pack_id = str(value.get("pack_id") or "")
        version = str(value.get("version") or "")
        if pack_id and version:
            workflow_runtimes.append(f"{pack_id}/{version.split('.', 1)[0]}.0")
    return CapabilityManifest(
        node_id=node_id,
        worker_version=WORKER_VERSION,
        os=os_name,
        os_version=platform.release(),
        architecture=architecture,
        cpu_count=os.cpu_count() or 1,
        memory_bytes=memory,
        disk_available_bytes=disk,
        executors=available_executors,
        isolation_level=isolation,
        workflow_runtimes=tuple(workflow_runtimes),
        max_concurrency=max(1, min((os.cpu_count() or 1) // 2, 8)),
    )


@dataclass
class ExecutionResult:
    state: str
    exit_code: int | None
    reason_category: str | None
    started_at: float
    ended_at: float
    stdout_sha256: str
    stderr_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    artifacts: list[ArtifactDescriptor]
    cleanup_status: str
    sandbox: Path
    resource_usage: dict[str, Any]

    def public_dict(self) -> dict[str, Any]:
        return sanitize_public(
            {
                "state": self.state,
                "exit_code": self.exit_code,
                "reason_category": self.reason_category,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "stdout_sha256": self.stdout_sha256,
                "stderr_sha256": self.stderr_sha256,
                "stdout_bytes": self.stdout_bytes,
                "stderr_bytes": self.stderr_bytes,
                "artifacts": [item.to_dict() for item in self.artifacts],
                "cleanup_status": self.cleanup_status,
                "resource_usage": dict(self.resource_usage),
            }
        )


class BoundedProcessExecutor:
    def __init__(self, home: str | Path | None = None):
        self.home = Path(home).expanduser().resolve() if home else worker_home()
        self.sandboxes = self.home / "sandboxes"
        self.logs = self.home / "logs"
        self.sandboxes.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)

    def execute(
        self,
        manifest: JobManifest,
        lease: JobLease,
        *,
        cancel_event: threading.Event | None = None,
        started_event: threading.Event | None = None,
        extra_env: Mapping[str, str] | None = None,
        input_payloads: Mapping[str, bytes] | None = None,
    ) -> ExecutionResult:
        if manifest.executor not in {"bounded-process", "oci-container"}:
            raise ProtocolError("worker cannot run the requested executor type")
        if lease.manifest_hash != manifest.manifest_hash or lease.job_id != manifest.job_id or lease.run_id != manifest.run_id:
            raise ProtocolError("lease does not bind the supplied manifest")
        sandbox = self._prepare_sandbox(manifest, lease)
        input_dir = sandbox / "input"
        output_dir = sandbox / "output"
        scratch_dir = sandbox / "scratch"
        for item in (input_dir, output_dir, scratch_dir):
            item.mkdir(parents=True, exist_ok=True)
        required_disk = max(
            0,
            int(manifest.required_capabilities.get("disk_bytes") or 0),
            int(manifest.budgets.get("disk_bytes") or 0),
        )
        if required_disk and shutil.disk_usage(sandbox).free < required_disk:
            return self._preflight_failure(sandbox, "disk_budget_unavailable")
        self._materialize_inputs(manifest, input_dir, input_payloads or {})
        environment = self._environment(manifest, lease, sandbox, input_dir, output_dir, scratch_dir, extra_env)
        command_argv = _sandboxed_command(manifest, sandbox, environment, self.home)
        budgets = manifest.budgets
        timeout_seconds = max(0.1, min(float(budgets.get("timeout_seconds") or 300), 24 * 60 * 60))
        max_output_bytes = max(1024, min(int(budgets.get("max_output_bytes") or 10 * 1024 * 1024), 100 * 1024 * 1024))
        max_memory_bytes = max(64 * 1024 * 1024, min(int(budgets.get("memory_bytes") or 512 * 1024 * 1024), 64 * 1024 * 1024 * 1024))
        max_process_count = max(1, min(int(budgets.get("process_count") or 64), 1024))
        stdout_file = sandbox / "stdout.log"
        stderr_file = sandbox / "stderr.log"
        started = time.time()
        state: str
        exit_code: int | None = None
        reason: str | None = None
        process: subprocess.Popen[bytes] | None = None
        peak_memory_bytes = 0
        peak_process_count = 0
        try:
            if cancel_event and cancel_event.is_set():
                stdout_file.touch()
                stderr_file.touch()
                state = "cancelled"
                reason = "user_cancelled"
            else:
                with stdout_file.open("wb") as stdout_handle, stderr_file.open("wb") as stderr_handle:
                    process = subprocess.Popen(
                        command_argv,
                        cwd=scratch_dir,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        start_new_session=True,
                        close_fds=True,
                        preexec_fn=None if sys.platform == "darwin" else lambda: _apply_resource_limits(budgets),
                    )
                    if started_event:
                        started_event.set()
                    next_resource_check = time.monotonic()
                    while True:
                        exit_code = process.poll()
                        if exit_code is not None:
                            state = "completed" if exit_code == 0 else "failed"
                            reason = None if exit_code == 0 else "worker_process_failed"
                            break
                        if cancel_event and cancel_event.is_set():
                            _terminate_process_tree(process)
                            exit_code = process.wait(timeout=5)
                            state = "cancelled"
                            reason = "user_cancelled"
                            break
                        if time.time() - started > timeout_seconds:
                            _terminate_process_tree(process)
                            exit_code = process.wait(timeout=5)
                            state = "failed"
                            reason = "time_budget_exceeded"
                            break
                        if _size(stdout_file) + _size(stderr_file) > max_output_bytes:
                            _terminate_process_tree(process)
                            exit_code = process.wait(timeout=5)
                            state = "failed"
                            reason = "output_budget_exceeded"
                            break
                        if sys.platform == "darwin" and time.monotonic() >= next_resource_check:
                            memory_bytes, process_count = _process_group_usage(process.pid)
                            peak_memory_bytes = max(peak_memory_bytes, memory_bytes)
                            peak_process_count = max(peak_process_count, process_count)
                            if memory_bytes > max_memory_bytes:
                                _terminate_process_tree(process)
                                exit_code = process.wait(timeout=5)
                                state = "failed"
                                reason = "memory_budget_exceeded"
                                break
                            if process_count > max_process_count:
                                _terminate_process_tree(process)
                                exit_code = process.wait(timeout=5)
                                state = "failed"
                                reason = "process_budget_exceeded"
                                break
                            next_resource_check = time.monotonic() + 0.2
                        time.sleep(0.02)
        except FileNotFoundError:
            state = "failed"
            reason = "entrypoint_not_found"
        except PermissionError:
            state = "failed"
            reason = "entrypoint_permission_denied"
        except Exception:
            if process and process.poll() is None:
                _terminate_process_tree(process)
            state = "failed"
            reason = "worker_internal_error"
        ended = time.time()
        artifacts: list[ArtifactDescriptor] = []
        if state == "completed":
            try:
                artifacts = self._collect_artifacts(manifest, lease, output_dir)
            except ProtocolError:
                state = "failed"
                reason = "artifact_validation_failed"
        return ExecutionResult(
            state=state,
            exit_code=exit_code,
            reason_category=reason,
            started_at=started,
            ended_at=ended,
            stdout_sha256=_file_hash(stdout_file),
            stderr_sha256=_file_hash(stderr_file),
            stdout_bytes=_size(stdout_file),
            stderr_bytes=_size(stderr_file),
            artifacts=artifacts,
            cleanup_status="retained" if int(manifest.cleanup_policy.get("retention_seconds") or 0) > 0 else "pending",
            sandbox=sandbox,
            resource_usage={
                "wall_seconds": max(0.0, ended - started),
                "peak_memory_bytes": peak_memory_bytes or None,
                "peak_process_count": peak_process_count or None,
                "stdout_bytes": _size(stdout_file),
                "stderr_bytes": _size(stderr_file),
            },
        )

    @staticmethod
    def _preflight_failure(sandbox: Path, reason: str) -> ExecutionResult:
        now = time.time()
        stdout_file = sandbox / "stdout.log"
        stderr_file = sandbox / "stderr.log"
        stdout_file.touch()
        stderr_file.touch()
        return ExecutionResult(
            state="failed",
            exit_code=None,
            reason_category=reason,
            started_at=now,
            ended_at=now,
            stdout_sha256=_file_hash(stdout_file),
            stderr_sha256=_file_hash(stderr_file),
            stdout_bytes=0,
            stderr_bytes=0,
            artifacts=[],
            cleanup_status="pending",
            sandbox=sandbox,
            resource_usage={"wall_seconds": 0.0, "peak_memory_bytes": None, "peak_process_count": None, "stdout_bytes": 0, "stderr_bytes": 0},
        )

    def _materialize_inputs(self, manifest: JobManifest, input_dir: Path, inputs: Mapping[str, bytes]) -> None:
        declared = {str(item.get("logical_name") or ""): str(item.get("sha256") or "") for item in manifest.input_artifacts}
        if set(inputs) != set(declared):
            if declared or inputs:
                raise ProtocolError("job input payloads do not match the manifest")
            return
        for logical_name, payload in inputs.items():
            normalized = normalize_relative_path(logical_name, "input logical_name")
            target = (input_dir / normalized).resolve()
            if input_dir.resolve() not in target.parents or target.is_symlink():
                raise ProtocolError("job input path escaped the sandbox")
            if sha256(payload).hexdigest() != declared[logical_name]:
                raise ProtocolError("job input payload hash mismatch")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        manifest_path = input_dir / "job-manifest.json"
        manifest_path.write_text(canonical_json(manifest.to_dict()) + "\n", encoding="utf-8")

    def cleanup(self, *, run_id: str, job_id: str, attempt: int, dry_run: bool = False) -> list[str]:
        targets = safe_cleanup_plan(self.home, run_id, job_id, attempt)
        rendered = [str(target.relative_to(self.home)) for target in targets]
        if dry_run:
            return rendered
        for target in targets:
            if target.exists():
                shutil.rmtree(target)
            for parent in (target.parent, target.parent.parent):
                if parent == self.sandboxes:
                    break
                try:
                    parent.rmdir()
                except (FileNotFoundError, OSError):
                    break
        return rendered

    def _prepare_sandbox(self, manifest: JobManifest, lease: JobLease) -> Path:
        target = (self.sandboxes / manifest.run_id / manifest.job_id / str(lease.attempt)).resolve()
        if self.sandboxes.resolve() not in target.parents:
            raise ProtocolError("job sandbox escaped the worker home")
        if target.exists():
            marker = target / ".across-job.json"
            if not marker.exists():
                raise ProtocolError("refusing to reuse an unmanaged sandbox")
            prior = json.loads(marker.read_text(encoding="utf-8"))
            if prior.get("manifest_hash") != manifest.manifest_hash:
                raise ProtocolError("sandbox manifest does not match current lease")
        target.mkdir(parents=True, exist_ok=True)
        marker = {"job_id": manifest.job_id, "run_id": manifest.run_id, "attempt": lease.attempt, "manifest_hash": manifest.manifest_hash}
        _atomic_write(target / ".across-job.json", marker)
        return target

    def _environment(
        self,
        manifest: JobManifest,
        lease: JobLease,
        sandbox: Path,
        input_dir: Path,
        output_dir: Path,
        scratch_dir: Path,
        extra_env: Mapping[str, str] | None,
    ) -> dict[str, str]:
        allow = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SYSTEMROOT", "WINDIR"}
        environment = {key: value for key, value in os.environ.items() if key in allow}
        pack_pointer = _read_json_if_present(self.home / "packs" / manifest.workflow_id / "current.json")
        if pack_pointer:
            pack_bin = self.home / "packs" / manifest.workflow_id / str(pack_pointer.get("version") or "") / "bin"
            if pack_bin.is_dir():
                environment["PATH"] = f"{pack_bin}{os.pathsep}{environment.get('PATH', '')}"
        environment.update(
            {
                "HOME": str(sandbox / "home"),
                "TMPDIR": str(sandbox / "tmp"),
                # Workflow packs must use the same verified Python runtime as
                # the Worker instead of launchd/system PATH, which may resolve
                # to an older TLS stack that cannot satisfy the TLS 1.3 policy.
                "ACROSS_WORKER_PYTHON": sys.executable,
                "ACROSS_JOB_ID": manifest.job_id,
                "ACROSS_RUN_ID": manifest.run_id,
                "ACROSS_MANIFEST_HASH": manifest.manifest_hash,
                "ACROSS_NODE_ID": lease.node_id,
                "ACROSS_INPUT_DIR": str(input_dir),
                "ACROSS_OUTPUT_DIR": str(output_dir),
                "ACROSS_SCRATCH_DIR": str(scratch_dir),
                # Never let a sandboxed localhost grant request inherit the
                # host's desktop HTTP/VPN proxy route. These values affect
                # only the child process and do not mutate system networking.
                "NO_PROXY": "localhost,127.0.0.1,::1",
                "no_proxy": "localhost,127.0.0.1,::1",
            }
        )
        for directory in (Path(environment["HOME"]), Path(environment["TMPDIR"])):
            directory.mkdir(parents=True, exist_ok=True)
        for key, value in dict(extra_env or {}).items():
            if key in _DENIED_ENV_NAMES or any(key.startswith(prefix) for prefix in _DENIED_ENV_PREFIXES):
                continue
            if key.startswith("ACROSS_") and key not in {
                "ACROSS_SCENARIO_SEED",
                "ACROSS_MODEL_GATEWAY_URL",
                "ACROSS_MODEL_GRANT_ID",
                "ACROSS_MODEL_TIMEOUT_SECONDS",
                "ACROSS_MODEL_MAX_TOKENS",
                "ACROSS_TRANSPORT",
            }:
                continue
            environment[str(key)] = str(value)
        network = manifest.permissions.get("network") or {}
        environment["ACROSS_NETWORK_POLICY"] = str(network.get("mode") or "none")
        environment["ACROSS_NETWORK_ALLOWLIST"] = ",".join(map(str, network.get("allowlist") or ()))
        return environment

    def _collect_artifacts(self, manifest: JobManifest, lease: JobLease, output_dir: Path) -> list[ArtifactDescriptor]:
        artifacts: list[ArtifactDescriptor] = []
        for logical_name in manifest.expected_outputs:
            target = (output_dir / logical_name).resolve()
            if output_dir.resolve() not in target.parents:
                raise ProtocolError("artifact path escaped output directory")
            if target.is_symlink() or not target.is_file():
                raise ProtocolError("required artifact is missing or unsafe")
            size = target.stat().st_size
            max_artifact_bytes = int(manifest.budgets.get("max_artifact_bytes") or 1024 * 1024 * 1024)
            if size > max_artifact_bytes:
                raise ProtocolError("artifact exceeds size budget")
            artifacts.append(
                ArtifactDescriptor(
                    artifact_id=new_protocol_id("artifact"),
                    run_id=manifest.run_id,
                    job_id=manifest.job_id,
                    node_id=lease.node_id,
                    logical_name=logical_name,
                    media_type=_media_type(logical_name),
                    size=size,
                    sha256=_file_hash(target),
                    upload_status="ready",
                    verification_status="pending",
                )
            )
        return artifacts


class ChunkedArtifactReceiver:
    """Content-addressed, resumable receiver; only confirmed chunks are retained."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def begin(self, descriptor: ArtifactDescriptor, *, chunk_size: int = 4 * 1024 * 1024) -> dict[str, Any]:
        size = max(64 * 1024, min(int(chunk_size), 16 * 1024 * 1024))
        directory = self._artifact_dir(descriptor.artifact_id)
        directory.mkdir(parents=True, exist_ok=True)
        metadata = descriptor.to_dict()
        metadata["chunk_size"] = size
        metadata["confirmed_chunks"] = self.confirmed_chunks(descriptor.artifact_id)
        _atomic_write(directory / "manifest.json", metadata)
        return sanitize_public(metadata)

    def write_chunk(self, artifact_id: str, index: int, payload: bytes, expected_sha256: str) -> dict[str, Any]:
        if index < 0 or len(payload) > 16 * 1024 * 1024:
            raise ProtocolError("artifact chunk is outside allowed limits")
        actual = sha256(payload).hexdigest()
        if actual != expected_sha256:
            raise ProtocolError("artifact chunk hash mismatch")
        directory = self._artifact_dir(artifact_id)
        manifest = _read_json(directory / "manifest.json")
        target = directory / f"chunk-{index:08d}"
        if target.exists() and _file_hash(target) == actual:
            return {"artifact_id": artifact_id, "index": index, "sha256": actual, "duplicate": True}
        with tempfile.NamedTemporaryFile("wb", dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        manifest["confirmed_chunks"] = self.confirmed_chunks(artifact_id)
        _atomic_write(directory / "manifest.json", manifest)
        return {"artifact_id": artifact_id, "index": index, "sha256": actual, "duplicate": False}

    def finalize(self, artifact_id: str) -> Path:
        directory = self._artifact_dir(artifact_id)
        manifest = _read_json(directory / "manifest.json")
        chunks = sorted(directory.glob("chunk-*"))
        final = directory / "artifact.bin"
        with tempfile.NamedTemporaryFile("wb", dir=directory, delete=False) as writer:
            temporary = Path(writer.name)
            for chunk in chunks:
                with chunk.open("rb") as reader:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        if temporary.stat().st_size != int(manifest["size"]) or _file_hash(temporary) != manifest["sha256"]:
            temporary.unlink(missing_ok=True)
            raise ProtocolError("final artifact size or hash mismatch")
        os.replace(temporary, final)
        manifest["upload_status"] = "complete"
        manifest["verification_status"] = "verified"
        _atomic_write(directory / "manifest.json", manifest)
        return final

    def confirmed_chunks(self, artifact_id: str) -> list[int]:
        directory = self._artifact_dir(artifact_id)
        return sorted(int(target.name.split("-")[1]) for target in directory.glob("chunk-[0-9]*") if target.is_file())

    def _artifact_dir(self, artifact_id: str) -> Path:
        if not artifact_id or "/" in artifact_id or "\\" in artifact_id or artifact_id in {".", ".."}:
            raise ProtocolError("unsafe artifact id")
        target = (self.root / artifact_id).resolve()
        if self.root not in target.parents:
            raise ProtocolError("artifact path escaped receiver root")
        return target


def _apply_resource_limits(budgets: Mapping[str, Any]) -> None:
    cpu_seconds = max(1, min(int(budgets.get("cpu_seconds") or budgets.get("timeout_seconds") or 300), 24 * 60 * 60))
    memory_bytes = max(64 * 1024 * 1024, min(int(budgets.get("memory_bytes") or 512 * 1024 * 1024), 64 * 1024 * 1024 * 1024))
    file_bytes = max(1024 * 1024, min(int(budgets.get("max_artifact_bytes") or 1024 * 1024 * 1024), 16 * 1024 * 1024 * 1024))
    process_count = max(1, min(int(budgets.get("process_count") or 64), 1024))
    open_files = max(16, min(int(budgets.get("open_files") or 256), 4096))
    limits_to_apply = [
        (resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1)),
        (resource.RLIMIT_FSIZE, (file_bytes, file_bytes)),
        (resource.RLIMIT_NOFILE, (open_files, open_files)),
    ]
    if sys.platform != "darwin":
        limits_to_apply.append((resource.RLIMIT_NPROC, (process_count, process_count)))
    for resource_id, limits in limits_to_apply:
        try:
            resource.setrlimit(resource_id, limits)
        except (ValueError, OSError):
            pass
    if sys.platform != "darwin":
        try:
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        except (ValueError, OSError):
            pass


def _sandboxed_command(manifest: JobManifest, sandbox: Path, environment: Mapping[str, str], worker_root: Path) -> list[str]:
    if manifest.executor == "oci-container":
        return _oci_container_command(manifest, sandbox, environment)
    command = list(manifest.command_argv)
    executable = Path(command[0]).expanduser()
    if executable.is_file() or executable.is_symlink():
        command[0] = str(executable.resolve())
    network_mode = str((manifest.permissions.get("network") or {}).get("mode") or "none")
    if sys.platform == "darwin":
        tool = Path("/usr/bin/sandbox-exec")
        if not tool.is_file():
            raise ProtocolError("macOS job sandbox is unavailable")
        quoted_sandbox = json.dumps(str(sandbox))
        login_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        denied_roots = {
            login_home / ".ssh",
            login_home / ".aws",
            login_home / ".gnupg",
            login_home / ".config",
            login_home / ".codex",
            login_home / ".claude",
            login_home / "Desktop",
            login_home / "Documents",
            login_home / "Downloads",
            login_home / "Library" / "Keychains",
            login_home / "Library" / "Safari",
            login_home / "Library" / "Application Support" / "Google" / "Chrome",
            login_home / "Library" / "Application Support" / "Firefox",
            worker_root / "identity",
            worker_root / "state",
        }
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow file-map-executable)",
            "(allow file-read*)",
            "(allow signal (target same-sandbox))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow ipc-posix*)",
        ]
        lines.extend(
            f"(deny file-read* (subpath {json.dumps(str(root))}))"
            for root in sorted(denied_roots, key=lambda item: str(item))
        )
        lines.append(f"(allow file-write* (subpath {quoted_sandbox}))")
        if network_mode == "allowlist":
            gateway = str(environment.get("ACROSS_MODEL_GATEWAY_URL") or "")
            parsed = urllib.parse.urlparse(gateway)
            if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or not parsed.port:
                raise ProtocolError("bounded macOS network allowlist requires a Worker-local grant proxy")
            lines.append(f'(allow network-outbound (remote tcp "localhost:{parsed.port}"))')
        profile = sandbox / ".across-sandbox.sb"
        profile.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return [str(tool), "-f", str(profile), "--", *command]
    if sys.platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if network_mode == "allowlist":
            raise ProtocolError("bounded Linux network allowlists require the OCI executor")
        if not bwrap:
            raise ProtocolError("Linux bounded sandbox requires bubblewrap")
        wrapper = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--bind",
            str(sandbox),
            str(sandbox),
            "--dev",
            "/dev",
            "--proc",
            "/proc",
        ]
        if Path("/home").exists():
            wrapper.extend(["--tmpfs", "/home"])
        if Path("/root").exists():
            wrapper.extend(["--tmpfs", "/root"])
        return [*wrapper, "--", *command]
    raise ProtocolError("unsupported Worker sandbox platform")


def _oci_container_command(manifest: JobManifest, sandbox: Path, environment: Mapping[str, str]) -> list[str]:
    runtime = shutil.which("docker") or shutil.which("podman")
    if not runtime:
        raise ProtocolError("OCI executor requires Docker or Podman")
    image = str(manifest.required_capabilities.get("oci_image") or "").strip()
    if not image:
        raise ProtocolError("OCI executor requires an explicit immutable image reference")
    if "@sha256:" not in image and not image.startswith("sha256:") and not bool(manifest.required_capabilities.get("allow_mutable_oci_image")):
        raise ProtocolError("OCI executor image must be digest pinned")
    network_mode = str((manifest.permissions.get("network") or {}).get("mode") or "none")
    if network_mode not in {"none", "unrestricted"}:
        raise ProtocolError("OCI network allowlists require a pre-created policy network")
    memory = max(16 * 1024 * 1024, int(manifest.budgets.get("memory_bytes") or 256 * 1024 * 1024))
    cpus = max(0.1, min(float(manifest.budgets.get("cpu_count") or 1), 64.0))
    command = [
        runtime,
        "run",
        "--rm",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--network={'none' if network_mode == 'none' else 'bridge'}",
        f"--memory={memory}",
        f"--cpus={cpus}",
        "--pids-limit=256",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m",
        "--tmpfs=/home/across:rw,nosuid,nodev,size=64m",
        f"--mount=type=bind,src={sandbox / 'input'},dst=/across/input,readonly",
        f"--mount=type=bind,src={sandbox / 'output'},dst=/across/output",
        f"--mount=type=bind,src={sandbox / 'scratch'},dst=/across/scratch",
        "--workdir=/across/scratch",
    ]
    remap = {
        str(sandbox / "input"): "/across/input",
        str(sandbox / "output"): "/across/output",
        str(sandbox / "scratch"): "/across/scratch",
        str(sandbox / "home"): "/home/across",
        str(sandbox / "tmp"): "/tmp",
    }
    for key, value in sorted(environment.items()):
        command.extend(("--env", f"{key}={remap.get(value, value)}"))
    command.append(image)
    command.extend(manifest.command_argv)
    return command


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.25)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _process_group_usage(process_group_id: int) -> tuple[int, int]:
    """Return aggregate resident memory and process count for a macOS job session."""
    try:
        root = psutil.Process(process_group_id)
        processes = [root, *root.children(recursive=True)]
    except (psutil.Error, OSError):
        return 0, 0
    memory_bytes = 0
    process_count = 0
    for process in processes:
        try:
            memory_bytes += max(0, int(process.memory_info().rss))
            process_count += 1
        except (psutil.Error, OSError):
            continue
    return memory_bytes, process_count


def _physical_memory_bytes() -> int:
    if sys.platform == "darwin":
        try:
            return int(subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True).strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        return 0


def _media_type(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return {".json": "application/json", ".md": "text/markdown", ".txt": "text/plain", ".sqlite": "application/vnd.sqlite3", ".graphml": "application/graphml+xml"}.get(suffix, "application/octet-stream")


def _file_hash(path: Path) -> str:
    digest = sha256()
    if not path.exists():
        return digest.hexdigest()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError("artifact upload manifest is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise ProtocolError("artifact upload manifest must be an object")
    return value


def _read_json_if_present(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
