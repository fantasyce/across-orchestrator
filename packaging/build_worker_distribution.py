from __future__ import annotations

from pathlib import Path
import argparse
import gzip
import io
import json
import tarfile


def build(source_root: Path, output: Path, version: str, *, source_date_epoch: int = 0) -> Path:
    package = source_root / "src" / "across_orchestrator"
    if not (package / "worker_cli.py").is_file():
        raise ValueError("Across Orchestrator source root is invalid")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = json.dumps(
        {
            "schema_version": "across-worker-distribution/1.0",
            "version": version,
            "entrypoint": "src/across_orchestrator/worker_cli.py",
            "python_requires": ">=3.11",
            "dependencies": ["cryptography>=42.0", "psutil>=5.9"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    with (
        output.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=source_date_epoch) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        info = tarfile.TarInfo("worker-distribution.json")
        info.size = len(manifest)
        info.mode = 0o644
        info.mtime = source_date_epoch
        archive.addfile(info, io.BytesIO(manifest))
        for path in sorted(package.rglob("*.py")):
            relative = Path("src") / "across_orchestrator" / path.relative_to(package)
            info = archive.gettarinfo(str(path), arcname=str(relative))
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = source_date_epoch
            with path.open("rb") as handle:
                archive.addfile(info, handle)
        installer = source_root / "packaging" / "install_worker.py"
        if installer.is_file():
            info = archive.gettarinfo(str(installer), arcname="install-worker.py")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755
            info.mtime = source_date_epoch
            with installer.open("rb") as handle:
                archive.addfile(info, handle)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a deterministic Across Worker source distribution")
    parser.add_argument("--source-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-date-epoch", type=int, default=0)
    args = parser.parse_args()
    result = build(Path(args.source_root).resolve(), Path(args.output).resolve(), args.version, source_date_epoch=args.source_date_epoch)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
