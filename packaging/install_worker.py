#!/usr/bin/env python3
"""Install a verified Across Worker release without a development checkout."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request


MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
MAX_UNPACKED_BYTES = 300 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("release assets must use credential-free HTTPS URLs")
    request = urllib.request.Request(url, headers={"User-Agent": "Across-Worker-Installer/1.0"})
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as writer:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise ValueError("Worker release asset exceeds the download limit")
            writer.write(chunk)


def _extract(artifact: Path, destination: Path, *, expected_version: str) -> dict[str, Any]:
    with tarfile.open(artifact, mode="r:gz") as archive:
        members = archive.getmembers()
        if not members or len(members) > 20_000:
            raise ValueError("Worker release archive is empty or too large")
        total = 0
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk() or member.isdev():
                raise ValueError("Worker release archive contains an unsafe entry")
            total += max(0, int(member.size))
        if total > MAX_UNPACKED_BYTES:
            raise ValueError("Worker release archive exceeds the unpacked size limit")
        manifest_member = next((item for item in members if item.name == "worker-distribution.json" and item.isfile()), None)
        if not manifest_member:
            raise ValueError("Worker release manifest is missing")
        handle = archive.extractfile(manifest_member)
        manifest = json.loads(handle.read().decode("utf-8") if handle else "{}")
        if manifest.get("schema_version") != "across-worker-distribution/1.0" or manifest.get("version") != expected_version:
            raise ValueError("Worker release manifest does not match the requested version")
        for member in members:
            if member.isdir():
                (destination / member.name).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target = (destination / member.name).resolve()
            if destination.resolve() not in target.parents:
                raise ValueError("Worker release extraction escaped its staging root")
            target.parent.mkdir(parents=True, exist_ok=True)
            reader = archive.extractfile(member)
            if not reader:
                raise ValueError("Worker release member is unavailable")
            with target.open("wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            target.chmod(member.mode & 0o755 if member.mode else 0o644)
    return manifest


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(argv, check=True, env=env)


def install(args: argparse.Namespace) -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("Across Worker requires Python 3.11 or newer")
    root = Path(args.home).expanduser().resolve()
    if root in {Path("/"), Path.home().resolve()}:
        raise ValueError("unsafe Worker home")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="across-worker-install-") as temporary_name:
        temporary = Path(temporary_name)
        artifact = temporary / "worker.tar.gz"
        _download(args.distribution_url, artifact)
        if _sha256(artifact) != args.distribution_sha256:
            raise ValueError("Worker release checksum mismatch")
        source = temporary / "source"
        source.mkdir()
        manifest = _extract(artifact, source, expected_version=args.version)
        dependencies = manifest.get("dependencies") or []
        if not isinstance(dependencies, list) or not all(isinstance(item, str) and item for item in dependencies):
            raise ValueError("Worker release dependency manifest is invalid")
        environment = root / "bootstrap" / "venv"
        if not (environment / "bin" / "python").is_file():
            _run([sys.executable, "-m", "venv", str(environment)])
        python = environment / "bin" / "python"
        if dependencies:
            _run([str(python), "-m", "pip", "install", "--disable-pip-version-check", *dependencies])
        runtime_env = {**os.environ, "PYTHONPATH": str(source / "src")}
        _run([str(python), "-m", "across_orchestrator.worker_cli", "--home", str(root), "install"], env=runtime_env)
        if args.pack_url:
            pack = temporary / "workflow-pack.tar.gz"
            _download(args.pack_url, pack)
            if _sha256(pack) != args.pack_sha256:
                raise ValueError("Worker workflow pack checksum mismatch")
            _run([str(root / "bin" / "across-worker"), "pack", "install", "--artifact", str(pack), "--sha256", args.pack_sha256])
        ca_path = root / "identity" / "enrollment-ca.pem"
        ca_path.parent.mkdir(parents=True, exist_ok=True)
        ca_path.write_bytes(__import__("base64").urlsafe_b64decode(args.enrollment_ca_base64.encode("ascii")))
        ca_path.chmod(0o600)
        worker = root / "bin" / "across-worker"
        join = [
            str(worker),
            "join",
            "--endpoint", args.worker_endpoint,
            "--enrollment-endpoint", args.enrollment_endpoint,
            "--transport", args.transport,
            "--enrollment-id", args.enrollment_id,
            "--pairing-code", args.pairing_code,
            "--ca-file", str(ca_path),
        ]
        if args.display_name:
            join.extend(["--display-name", args.display_name])
        _run(join)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install and pair a verified Across Worker release")
    parser.add_argument("--distribution-url", required=True)
    parser.add_argument("--distribution-sha256", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--home", default="~/.across/worker")
    parser.add_argument("--worker-endpoint", required=True)
    parser.add_argument("--enrollment-endpoint", required=True)
    parser.add_argument("--enrollment-ca-base64", required=True)
    parser.add_argument("--transport", choices=("direct", "overlay"), required=True)
    parser.add_argument("--enrollment-id", required=True)
    parser.add_argument("--pairing-code", required=True)
    parser.add_argument("--display-name")
    parser.add_argument("--pack-url")
    parser.add_argument("--pack-sha256")
    args = parser.parse_args(argv)
    if not __import__("re").fullmatch(r"[0-9a-f]{64}", args.distribution_sha256):
        parser.error("distribution SHA-256 must contain 64 lowercase hexadecimal characters")
    if bool(args.pack_url) != bool(args.pack_sha256) or (args.pack_sha256 and not __import__("re").fullmatch(r"[0-9a-f]{64}", args.pack_sha256)):
        parser.error("workflow pack URL and SHA-256 must be supplied together")
    try:
        install(args)
    except Exception as exc:
        print(f"Across Worker installation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
