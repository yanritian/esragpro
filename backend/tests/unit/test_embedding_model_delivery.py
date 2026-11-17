from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "provision_embedding_model.py"
REPO_ROOT = SCRIPT.parents[2]
LOCK = REPO_ROOT / "backend" / "model-locks" / "BAAI--bge-m3.json"
LICENSE = REPO_ROOT / "backend" / "model-locks" / "BAAI--bge-m3.LICENSE"


def _load_module():
    spec = importlib.util.spec_from_file_location("provision_embedding_model", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provisioner = _load_module()


def _make_lock(path: Path, files: dict[str, bytes]) -> Path:
    entries = []
    for relative, content in sorted(files.items()):
        entries.append(
            {
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "url": f"https://example.invalid/{REVISION}/{relative}",
            }
        )
    lock = {
        "schema_version": 1,
        "model": "BAAI/bge-m3",
        "revision": REVISION,
        "dimension": 1024,
        "target_dir": f"BAAI--bge-m3/{REVISION}",
        "files": entries,
        "license": {
            "spdx_id": "MIT",
            "source": "https://example.invalid/FlagEmbedding/LICENSE",
            "sha256": "0" * 64,
        },
    }
    path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return path


def _source_snapshot(root: Path, files: dict[str, bytes]) -> Path:
    snapshot = root / "snapshots" / REVISION
    for relative, content in files.items():
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return snapshot


def _target(root: Path) -> Path:
    return root / "BAAI--bge-m3" / REVISION


def test_production_lock_has_verified_manifest_and_license_digest() -> None:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))

    assert lock["revision"] == REVISION
    assert lock["dimension"] == 1024
    assert lock["file_count"] == 11
    assert lock["total_bytes"] == 2_293_331_623
    assert len(lock["files"]) == 11
    assert all(REVISION in entry["url"] for entry in lock["files"])
    assert hashlib.sha256(LICENSE.read_bytes()).hexdigest() == lock["license"]["sha256"]


def test_import_materialized_snapshot_publishes_stable_marker_and_summary(tmp_path: Path) -> None:
    files = {"config.json": b"{}", "nested/tokenizer.json": b"tokenizer"}
    lock = _make_lock(tmp_path / "lock.json", files)
    snapshot = _source_snapshot(tmp_path / "source", files)

    summary = provisioner.run_import(lock, tmp_path / "volume", snapshot)

    assert summary["status"] == "ready"
    assert summary["file_count"] == 2
    assert summary["total_bytes"] == 11
    marker = json.loads((_target(tmp_path / "volume") / "_MODEL_READY.json").read_text())
    assert marker["revision"] == REVISION
    assert marker["file_count"] == 2
    assert marker["files"] == [
        {"path": "config.json", "size": 2, "sha256": hashlib.sha256(b"{}").hexdigest()},
        {
            "path": "nested/tokenizer.json",
            "size": 9,
            "sha256": hashlib.sha256(b"tokenizer").hexdigest(),
        },
    ]


def test_import_accepts_huggingface_repo_cache_root(tmp_path: Path) -> None:
    files = {"config.json": b"cache"}
    lock = _make_lock(tmp_path / "lock.json", files)
    cache = tmp_path / "models--BAAI--bge-m3"
    _source_snapshot(cache, files)

    provisioner.run_import(lock, tmp_path / "volume", cache)

    assert (_target(tmp_path / "volume") / "config.json").read_bytes() == b"cache"


def test_import_dereferences_symlink_inside_hf_cache(tmp_path: Path) -> None:
    files = {"config.json": b"blob"}
    lock = _make_lock(tmp_path / "lock.json", files)
    cache = tmp_path / "models--BAAI--bge-m3"
    snapshot = _source_snapshot(cache, {})
    blob = cache / "blobs" / "abc"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"blob")
    try:
        os.symlink(blob, snapshot / "config.json")
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    provisioner.run_import(lock, tmp_path / "volume", cache)

    destination = _target(tmp_path / "volume") / "config.json"
    assert destination.read_bytes() == b"blob"
    assert not destination.is_symlink()


def test_import_rejects_symlink_path_escape_and_leaves_no_ready_marker(tmp_path: Path) -> None:
    files = {"config.json": b"safe"}
    lock = _make_lock(tmp_path / "lock.json", files)
    cache = tmp_path / "models--BAAI--bge-m3"
    snapshot = _source_snapshot(cache, {})
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"escape")
    try:
        os.symlink(outside, snapshot / "config.json")
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(provisioner.ProvisionError, match="outside"):
        provisioner.run_import(lock, tmp_path / "volume", cache)

    assert not list((tmp_path / "volume").glob("**/_MODEL_READY.json"))


def test_verify_rejects_hash_mismatch_and_missing_file(tmp_path: Path) -> None:
    files = {"config.json": b"correct", "nested/data.bin": b"data"}
    lock = _make_lock(tmp_path / "lock.json", files)
    snapshot = _source_snapshot(tmp_path / "source", files)
    volume = tmp_path / "volume"
    provisioner.run_import(lock, volume, snapshot)
    destination = _target(volume) / "config.json"
    destination.write_bytes(b"wrong")

    with pytest.raises(provisioner.ProvisionError, match="(hash|size)"):
        provisioner.run_verify(lock, volume)

    destination.write_bytes(b"correct")
    (destination.parent / "nested" / "data.bin").unlink()
    with pytest.raises(provisioner.ProvisionError, match="missing"):
        provisioner.run_verify(lock, volume)


def test_verify_rejects_extra_ready_file_and_does_not_use_network(
    tmp_path: Path, monkeypatch
) -> None:
    files = {"config.json": b"data"}
    lock = _make_lock(tmp_path / "lock.json", files)
    snapshot = _source_snapshot(tmp_path / "source", files)
    volume = tmp_path / "volume"
    provisioner.run_import(lock, volume, snapshot)
    (_target(volume) / "unexpected.bin").write_bytes(b"extra")

    def fail_network(*_args, **_kwargs):
        raise AssertionError("verify must be network-free")

    monkeypatch.setattr(provisioner.urllib.request, "urlopen", fail_network)
    with pytest.raises(provisioner.ProvisionError, match="extra"):
        provisioner.run_verify(lock, volume)


def test_cli_emits_one_stable_machine_readable_summary(tmp_path: Path, capsys) -> None:
    files = {"config.json": b"data"}
    lock = _make_lock(tmp_path / "lock.json", files)
    snapshot = _source_snapshot(tmp_path / "source", files)
    volume = tmp_path / "volume"
    provisioner.run_import(lock, volume, snapshot)

    assert provisioner.main(["verify", "--lock", str(lock), "--model-root", str(volume)]) == 0
    output = capsys.readouterr().out.strip().splitlines()
    assert len(output) == 1
    summary = json.loads(output[0])
    assert summary == {
        "command": "verify",
        "file_count": 1,
        "idempotent": True,
        "lock_sha256": summary["lock_sha256"],
        "model": "BAAI/bge-m3",
        "revision": REVISION,
        "status": "ready",
        "target": summary["target"],
        "total_bytes": 4,
    }


def test_import_repairs_partial_publication_and_is_idempotent(tmp_path: Path) -> None:
    files = {"config.json": b"data"}
    lock = _make_lock(tmp_path / "lock.json", files)
    snapshot = _source_snapshot(tmp_path / "source", files)
    volume = tmp_path / "volume"
    partial = _target(volume)
    partial.mkdir(parents=True)
    (partial / "config.json").write_bytes(b"partial")
    (volume / "BAAI--bge-m3" / ".5617a9f61b028005a4858fdac845db406aefb181.staging-orphan").mkdir(
        parents=True
    )

    first = provisioner.run_import(lock, volume, snapshot)
    marker = (_target(volume) / "_MODEL_READY.json").read_bytes()
    second = provisioner.run_import(lock, volume, snapshot)

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert (_target(volume) / "config.json").read_bytes() == b"data"
    assert (_target(volume) / "_MODEL_READY.json").read_bytes() == marker
    assert not list(
        (volume / "BAAI--bge-m3").glob(".5617a9f61b028005a4858fdac845db406aefb181.staging-*")
    )


def test_failed_publication_cleans_owned_staging_and_ready_marker(
    tmp_path: Path, monkeypatch
) -> None:
    files = {"config.json": b"data"}
    lock = _make_lock(tmp_path / "lock.json", files)
    snapshot = _source_snapshot(tmp_path / "source", files)
    volume = tmp_path / "volume"

    def interrupt(*_args, **_kwargs):
        raise provisioner.ProvisionError("interrupted publication")

    monkeypatch.setattr(provisioner, "_write_marker", interrupt)
    with pytest.raises(provisioner.ProvisionError, match="interrupted"):
        provisioner.run_import(lock, volume, snapshot)

    assert not list(volume.glob("**/_MODEL_READY.json"))
    assert not list(
        (volume / "BAAI--bge-m3").glob(".5617a9f61b028005a4858fdac845db406aefb181.staging-*")
    )


def test_acquire_uses_locked_urls_and_publishes_summary(tmp_path: Path, monkeypatch) -> None:
    files = {"config.json": b"downloaded"}
    lock = _make_lock(tmp_path / "lock.json", files)

    def fake_download(url: str, destination: Path) -> None:
        assert url.startswith("https://example.invalid/")
        destination.write_bytes(files[destination.name])

    monkeypatch.setattr(provisioner, "download_file", fake_download)
    summary = provisioner.run_acquire(lock, tmp_path / "volume")

    assert summary["status"] == "ready"
    assert (_target(tmp_path / "volume") / "config.json").read_bytes() == b"downloaded"


def test_concurrent_lock_is_fail_closed(tmp_path: Path) -> None:
    files = {"config.json": b"data"}
    lock = _make_lock(tmp_path / "lock.json", files)
    volume = tmp_path / "volume"
    volume.mkdir()
    lock_file = volume / ".BAAI--bge-m3.provision.lock"
    lock_file.write_text("owned", encoding="ascii")

    with pytest.raises(provisioner.ProvisionError, match="lock"):
        provisioner.run_verify(lock, volume)


@pytest.mark.parametrize(
    "wrapper",
    [
        REPO_ROOT / "scripts/provision-embedding-model.ps1",
        REPO_ROOT / "scripts/provision-embedding-model.sh",
    ],
)
def test_wrappers_use_no_deps_init_and_optional_readonly_source(wrapper: Path) -> None:
    source = wrapper.read_text(encoding="utf-8")
    normalized = source.replace("\\", "/").lower()
    assert "embedding-model-init" in normalized
    assert "run" in normalized
    assert "--rm" in normalized
    assert "--no-deps" in normalized
    assert (
        "model-locks/baai--bge-m3.json" in normalized
        or "model-locks\\baai--bge-m3.json" in normalized
    )
    assert ":ro" in normalized
    assert "-p" in normalized or "--project-name" in normalized
