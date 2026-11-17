#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
#
# ─── How to run ───
# uv run backend/scripts/provision_embedding_model.py import --lock backend/model-locks/BAAI--bge-m3.json --model-root /models --source /mnt/model-source
# noqa: SIZE_OK

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal, TypedDict, assert_never
from urllib.parse import urlsplit


Command = Literal["acquire", "import", "verify"]
JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
MARKER_NAME: Final = "_MODEL_READY.json"
CHUNK_SIZE: Final = 1024 * 1024


class ProvisionError(RuntimeError):
    """Raised when a model cannot be safely acquired, imported, or verified."""


class LockedFileData(TypedDict):
    path: str
    size: int
    sha256: str
    url: str


class MarkerFileData(TypedDict):
    path: str
    size: int
    sha256: str


class Summary(TypedDict):
    command: str
    status: str
    model: str
    revision: str
    target: str
    lock_sha256: str
    file_count: int
    total_bytes: int
    idempotent: bool


class MarkerData(TypedDict):
    schema_version: int
    model: str
    revision: str
    dimension: int
    target_dir: str
    lock_sha256: str
    file_count: int
    total_bytes: int
    files: list[MarkerFileData]
    ready: bool


@dataclass(frozen=True, slots=True)
class LockedFile:
    """One content-addressed file in the immutable model lock."""

    path: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True, slots=True)
class ModelLock:
    """Parsed model lock and its byte-level digest."""

    model: str
    revision: str
    dimension: int
    target_dir: str
    files: tuple[LockedFile, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class _VolumeLock:
    """Owned lock file used to reject concurrent provision attempts."""

    path: Path


def _error(message: str) -> ProvisionError:
    return ProvisionError(message)


def _safe_relative(value: str, *, label: str) -> str:
    if not value or "\x00" in value or "\\" in value or ":" in value:
        raise _error(f"{label} is not a safe relative POSIX path")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise _error(f"{label} is not a safe relative POSIX path")
    normalized = candidate.as_posix()
    if normalized != value:
        raise _error(f"{label} is not normalized")
    return normalized


def _read_lock(path: Path) -> ModelLock:
    try:
        raw: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(f"unable to read lock: {path}") from exc
    if not isinstance(raw, dict):
        raise _error("lock must contain a JSON object")
    try:
        model = raw["model"]
        revision = raw["revision"]
        dimension = raw["dimension"]
        target_dir = raw["target_dir"]
        entries = raw["files"]
    except KeyError as exc:
        raise _error(f"lock is missing field: {exc.args[0]}") from exc
    if (
        not isinstance(model, str)
        or not isinstance(revision, str)
        or not isinstance(dimension, int)
    ):
        raise _error("lock model, revision, and dimension have invalid types")
    if not isinstance(target_dir, str):
        raise _error("lock target_dir has invalid type")
    _safe_relative(target_dir, label="lock target_dir")
    if not isinstance(entries, list) or not entries:
        raise _error("lock files must be a non-empty list")
    parsed: list[LockedFile] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise _error("lock file entry must be an object")
        try:
            relative = entry["path"]
            size = entry["size"]
            digest = entry["sha256"]
            url = entry["url"]
        except KeyError as exc:
            raise _error(f"lock file is missing field: {exc.args[0]}") from exc
        if (
            not isinstance(relative, str)
            or not isinstance(size, int)
            or not isinstance(digest, str)
        ):
            raise _error("lock file path, size, and sha256 have invalid types")
        if not isinstance(url, str):
            raise _error("lock file url has invalid type")
        relative = _safe_relative(relative, label="lock file path")
        if relative in seen or size < 0 or len(digest) != 64:
            raise _error("lock contains a duplicate or invalid file entry")
        parsed_url = urlsplit(url)
        if parsed_url.username or parsed_url.password or revision not in url:
            raise _error(f"lock URL is not commit-qualified: {relative}")
        seen.add(relative)
        parsed.append(LockedFile(relative, size, digest.lower(), url))
    parsed.sort(key=lambda entry: entry.path)
    return ModelLock(model, revision, dimension, target_dir, tuple(parsed), _sha256(path))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _error(f"unable to hash file: {path}") from exc
    return digest.hexdigest()


def _metadata(path: Path) -> tuple[int, str]:
    try:
        size = path.stat().st_size
        digest = _sha256(path)
    except OSError as exc:
        raise _error(f"unable to inspect file: {path}") from exc
    return size, digest


def _target(model_root: Path, lock: ModelLock) -> Path:
    root = model_root.resolve()
    target = (root / Path(lock.target_dir)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise _error("lock target escapes model root") from exc
    return target


def _expected_marker(lock: ModelLock) -> MarkerData:
    files = [MarkerFileData(path=f.path, size=f.size, sha256=f.sha256) for f in lock.files]
    return MarkerData(
        schema_version=1,
        model=lock.model,
        revision=lock.revision,
        dimension=lock.dimension,
        target_dir=lock.target_dir,
        lock_sha256=lock.digest,
        file_count=len(files),
        total_bytes=sum(item["size"] for item in files),
        files=files,
        ready=True,
    )


def _summary(command: Command, lock: ModelLock, target: Path, *, idempotent: bool) -> Summary:
    marker = _expected_marker(lock)
    return Summary(
        command=command,
        status="ready",
        model=lock.model,
        revision=lock.revision,
        target=target.as_posix(),
        lock_sha256=lock.digest,
        file_count=marker["file_count"],
        total_bytes=marker["total_bytes"],
        idempotent=idempotent,
    )


def _lock_name(lock: ModelLock) -> str:
    return PurePosixPath(lock.target_dir).parts[0]


@contextmanager
def _volume_lock(model_root: Path, lock: ModelLock) -> Generator[_VolumeLock, None, None]:
    model_root.mkdir(parents=True, exist_ok=True)
    path = model_root / f".{_lock_name(lock)}.provision.lock"
    try:
        with path.open("x", encoding="ascii") as stream:
            stream.write(f"pid={os.getpid()}\n")
    except FileExistsError as exc:
        raise _error(f"provision lock already exists: {path.name}") from exc
    try:
        yield _VolumeLock(path)
    finally:
        path.unlink(missing_ok=True)


def _read_marker(path: Path) -> dict[str, JsonValue]:
    try:
        raw: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error("ready marker is unreadable") from exc
    if not isinstance(raw, dict):
        raise _error("ready marker is not an object")
    return raw


def _verify_unlocked(lock: ModelLock, target: Path) -> None:
    if not target.is_dir() or target.is_symlink():
        raise _error("model target is missing or not a directory")
    expected = _expected_marker(lock)
    marker_path = target / MARKER_NAME
    if marker_path.is_symlink() or not marker_path.is_file():
        raise _error("ready marker is missing or not a regular file")
    marker = _read_marker(marker_path)
    if marker != expected:
        raise _error("ready marker does not match lock")
    expected_paths = {entry.path for entry in lock.files}
    expected_dirs = {PurePosixPath(path).parent.as_posix() for path in expected_paths}
    expected_dirs.discard(".")
    for candidate in target.rglob("*"):
        relative = candidate.relative_to(target).as_posix()
        if relative == MARKER_NAME:
            continue
        if candidate.is_symlink():
            raise _error(f"ready target contains symlink: {relative}")
        if candidate.is_dir():
            if relative not in expected_dirs:
                raise _error(f"ready target contains extra directory: {relative}")
        elif relative not in expected_paths:
            raise _error(f"ready target contains extra file: {relative}")
    for entry in lock.files:
        candidate = target / Path(entry.path)
        if not candidate.is_file():
            raise _error(f"ready target is missing file: {entry.path}")
        size, digest = _metadata(candidate)
        if size != entry.size:
            raise _error(f"size mismatch for {entry.path}")
        if digest != entry.sha256:
            raise _error(f"hash mismatch for {entry.path}")


def _is_ready(lock: ModelLock, target: Path) -> bool:
    if not target.exists() or not target.is_dir():
        return False
    try:
        _verify_unlocked(lock, target)
    except ProvisionError:
        return False
    return True


def run_verify(lock_path: Path, model_root: Path) -> Summary:
    """Verify a published model without making network requests."""
    lock = _read_lock(lock_path)
    target = _target(model_root, lock)
    with _volume_lock(model_root, lock):
        _verify_unlocked(lock, target)
    return _summary("verify", lock, target, idempotent=True)


def _source_snapshot(source: Path, revision: str) -> tuple[Path, Path]:
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error("model source does not exist") from exc
    snapshots = resolved / "snapshots"
    if snapshots.is_dir():
        snapshot = snapshots / revision
        if not snapshot.is_dir():
            raise _error(f"model source has no locked revision: {revision}")
        return snapshot, resolved
    if resolved.name != revision or not resolved.is_dir():
        raise _error("model source must be an HF cache root or exact locked snapshot")
    cache_root = resolved.parent.parent if resolved.parent.name == "snapshots" else resolved
    return resolved, cache_root.resolve()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _copy_locked_file(
    source_snapshot: Path, source_root: Path, destination: Path, entry: LockedFile
) -> None:
    source = source_snapshot / Path(entry.path)
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(f"source file is missing: {entry.path}") from exc
    if not _inside(resolved, source_root.resolve()) or not resolved.is_file():
        raise _error(f"source path escapes cache root: {entry.path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(resolved, destination)
    except OSError as exc:
        raise _error(f"unable to copy source file: {entry.path}") from exc
    size, digest = _metadata(destination)
    if size != entry.size or digest != entry.sha256:
        raise _error(f"source hash mismatch for {entry.path}")


def download_file(url: str, destination: Path) -> None:
    """Download one locked URL directly into a staging file."""
    try:
        with urllib.request.urlopen(url, timeout=60) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output, CHUNK_SIZE)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise _error("model download failed") from exc


def _write_marker(stage: Path, marker: MarkerData) -> None:
    marker_path = stage / MARKER_NAME
    fd, temporary_name = tempfile.mkstemp(prefix=".ready-", dir=stage)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(marker, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, marker_path)
    except (OSError, UnicodeError, TypeError) as exc:
        temporary.unlink(missing_ok=True)
        raise _error("unable to publish ready marker") from exc


def _remove_staging(target: Path) -> None:
    pattern = f".{target.name}.staging-*"
    for stale in target.parent.glob(pattern):
        if stale.is_symlink():
            stale.unlink(missing_ok=True)
        elif stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)


def _publish(stage: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise _error("model target is not a directory")
        backup = target.parent / f".{target.name}.backup-{os.getpid()}"
        if backup.is_dir():
            shutil.rmtree(backup, ignore_errors=True)
        else:
            backup.unlink(missing_ok=True)
        os.replace(target, backup)
    try:
        os.replace(stage, target)
    except OSError as exc:
        if backup is not None and not target.exists():
            os.replace(backup, target)
        raise _error("unable to atomically publish model target") from exc
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def _materialize(
    command: Command,
    lock: ModelLock,
    model_root: Path,
    populate: Callable[[Path], None],
) -> Summary:
    target = _target(model_root, lock)
    with _volume_lock(model_root, lock):
        try:
            if _is_ready(lock, target):
                return _summary(command, lock, target, idempotent=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            _remove_staging(target)
            stage: Path | None = None
            try:
                stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
                populate(stage)
                _write_marker(stage, _expected_marker(lock))
                _verify_unlocked(lock, stage)
                _publish(stage, target)
            except (OSError, ProvisionError) as exc:
                if stage is not None:
                    shutil.rmtree(stage, ignore_errors=True)
                if isinstance(exc, ProvisionError):
                    raise
                raise _error("model publication failed") from exc
        except ProvisionError:
            raise
    return _summary(command, lock, target, idempotent=False)


def run_import(lock_path: Path, model_root: Path, source: Path) -> Summary:
    """Import locked files from an HF cache or exact snapshot directory."""
    lock = _read_lock(lock_path)
    source_snapshot, source_root = _source_snapshot(source, lock.revision)

    def populate(stage: Path) -> None:
        for entry in lock.files:
            _copy_locked_file(source_snapshot, source_root, stage / Path(entry.path), entry)

    return _materialize("import", lock, model_root, populate)


def run_acquire(lock_path: Path, model_root: Path) -> Summary:
    """Acquire locked files from commit-qualified URLs into the model volume."""
    lock = _read_lock(lock_path)

    def populate(stage: Path) -> None:
        for entry in lock.files:
            destination = stage / Path(entry.path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            download_file(entry.url, destination)
            size, digest = _metadata(destination)
            if size != entry.size or digest != entry.sha256:
                raise _error(f"download hash mismatch for {entry.path}")

    return _materialize("acquire", lock, model_root, populate)


def _command(value: str) -> Command:
    commands: dict[str, Command] = {
        "acquire": "acquire",
        "import": "import",
        "verify": "verify",
    }
    try:
        return commands[value]
    except KeyError as exc:
        raise _error(f"unsupported command: {value}") from exc


def main(argv: list[str] | None = None) -> int:
    """Parse the CLI and emit one stable JSON summary on success."""
    parser = argparse.ArgumentParser(description="Provision the locked BGE-M3 model.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("acquire", "import", "verify"):
        command_parser = subparsers.add_parser(name)
        command_parser.add_argument("--lock", required=True, type=Path)
        command_parser.add_argument("--model-root", required=True, type=Path)
        if name == "import":
            command_parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args(argv)
    command = _command(args.command)
    try:
        match command:
            case "acquire":
                result = run_acquire(args.lock, args.model_root)
            case "import":
                result = run_import(args.lock, args.model_root, args.source)
            case "verify":
                result = run_verify(args.lock, args.model_root)
            case unreachable:
                assert_never(unreachable)
    except ProvisionError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
