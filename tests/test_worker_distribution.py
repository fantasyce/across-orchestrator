from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import hashlib
import json
import tarfile


def _load(name: str, path: Path):
    spec = spec_from_file_location(name, path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_distribution_is_deterministic_and_contains_standalone_bootstrap(tmp_path):
    root = Path(__file__).resolve().parents[1]
    builder = _load("worker_distribution_builder", root / "packaging" / "build_worker_distribution.py")
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    builder.build(root, first, "0.10.0", source_date_epoch=1_700_000_000)
    builder.build(root, second, "0.10.0", source_date_epoch=1_700_000_000)
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(second.read_bytes()).hexdigest()
    with tarfile.open(first, "r:gz") as archive:
        names = archive.getnames()
        assert "install-worker.py" in names
        assert "src/across_orchestrator/worker_cli.py" in names
        manifest = json.load(archive.extractfile("worker-distribution.json"))
    assert manifest == {
        "schema_version": "across-worker-distribution/1.0",
        "version": "0.10.0",
        "entrypoint": "src/across_orchestrator/worker_cli.py",
        "python_requires": ">=3.11",
        "dependencies": ["cryptography>=42.0", "psutil>=5.9"],
    }


def test_bootstrap_rejects_archive_escape_and_version_mismatch(tmp_path):
    root = Path(__file__).resolve().parents[1]
    builder = _load("worker_distribution_builder_second", root / "packaging" / "build_worker_distribution.py")
    installer = _load("worker_distribution_installer", root / "packaging" / "install_worker.py")
    artifact = tmp_path / "worker.tar.gz"
    builder.build(root, artifact, "0.10.0", source_date_epoch=1_700_000_000)
    destination = tmp_path / "unpacked"
    destination.mkdir()
    manifest = installer._extract(artifact, destination, expected_version="0.10.0")
    assert manifest["version"] == "0.10.0"
    second = tmp_path / "wrong-version"
    second.mkdir()
    import pytest

    with pytest.raises(ValueError, match="does not match"):
        installer._extract(artifact, second, expected_version="0.10.1")
