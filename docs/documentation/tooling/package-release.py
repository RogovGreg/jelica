#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


VARIANT_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a deterministic JELICA documentation release bundle."
    )
    parser.add_argument("--docs-root", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--locale", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--size", required=True)
    return parser.parse_args()


def load_json(file_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Required release input is missing: {file_path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Invalid JSON in {file_path} at line {error.lineno}, column {error.colno}: "
            f"{error.msg}"
        ) from error
    if not isinstance(value, dict):
        raise ValueError(f"Release input must contain a JSON object: {file_path}")
    return value


def source_date_epoch() -> int:
    raw_value = os.environ.get("SOURCE_DATE_EPOCH")
    if raw_value is None:
        raise ValueError("SOURCE_DATE_EPOCH must be set by the documentation build entry point.")
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError("SOURCE_DATE_EPOCH must be an integer.") from error
    if value < 0 or value > 0xFFFFFFFF:
        raise ValueError("SOURCE_DATE_EPOCH must be between 0 and 4294967295.")
    return value


def sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_tree_hash(source_roots: list[Path], docs_root: Path) -> str:
    for source_root in source_roots:
        if source_root.is_symlink() or not source_root.is_dir():
            raise ValueError(f"Documentation source input is missing or unsafe: {source_root}")
    files: list[Path] = []
    for source_root in source_roots:
        for item in source_root.rglob("*"):
            if item.is_symlink():
                raise ValueError(f"Documentation source symlinks are not supported: {item}")
            if item.is_file():
                files.append(item)
    files.sort(key=lambda item: item.relative_to(docs_root).as_posix())
    if not files:
        raise ValueError("Documentation source inputs are empty.")
    digest = hashlib.sha256()
    for file_path in files:
        relative_bytes = file_path.relative_to(docs_root).as_posix().encode("utf-8")
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(bytes.fromhex(sha256_file(file_path)))
    return digest.hexdigest()


def normalize_file(file_path: Path, epoch: int) -> None:
    file_path.chmod(0o644)
    os.utime(file_path, (epoch, epoch), follow_symlinks=False)


def copy_file(source: Path, destination: Path, epoch: int) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Release input must be a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    normalize_file(destination, epoch)


def write_json(file_path: Path, value: dict[str, Any], epoch: int) -> None:
    file_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    normalize_file(file_path, epoch)


def checksum_entries(bundle_root: Path) -> list[dict[str, Any]]:
    entries = []
    for file_path in sorted(item for item in bundle_root.rglob("*") if item.is_file()):
        relative_path = file_path.relative_to(bundle_root).as_posix()
        if relative_path == "checksums.json":
            continue
        entries.append(
            {
                "path": relative_path,
                "sha256": sha256_file(file_path),
                "size": file_path.stat().st_size,
            }
        )
    return entries


def verify_checksums(bundle_root: Path, entries: list[dict[str, Any]]) -> None:
    expected_paths = {
        item.relative_to(bundle_root).as_posix()
        for item in bundle_root.rglob("*")
        if item.is_file()
        and item.relative_to(bundle_root).as_posix() != "checksums.json"
    }
    listed_paths = {entry["path"] for entry in entries}
    if expected_paths != listed_paths:
        raise ValueError("Checksum manifest does not cover every release artifact.")
    for entry in entries:
        relative_path = entry["path"]
        parts = relative_path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"Unsafe checksum path: {relative_path}")
        file_path = bundle_root.joinpath(*parts)
        if file_path.stat().st_size != entry["size"]:
            raise ValueError(f"Checksum size verification failed: {relative_path}")
        if sha256_file(file_path) != entry["sha256"]:
            raise ValueError(f"Checksum verification failed: {relative_path}")


def normalize_directories(bundle_root: Path, epoch: int) -> None:
    directories = [bundle_root]
    directories.extend(item for item in bundle_root.rglob("*") if item.is_dir())
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        directory.chmod(0o755)
        os.utime(directory, (epoch, epoch), follow_symlinks=False)


def validate_release(docs_root: Path, bundle_root: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(docs_root / "tooling" / "validate-artifacts.py"),
            "--docs-root",
            str(docs_root),
            "--artifact-root",
            str(bundle_root),
        ],
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("Copied release artifacts failed compatibility validation.")


def validated_inventory(docs_root: Path, artifact_root: Path) -> list[str]:
    inventory_descriptor, inventory_name = tempfile.mkstemp(
        prefix="jelica-doc-inventory-", suffix=".json"
    )
    os.close(inventory_descriptor)
    inventory_file = Path(inventory_name)
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(docs_root / "tooling" / "validate-artifacts.py"),
                "--docs-root",
                str(docs_root),
                "--artifact-root",
                str(artifact_root),
                "--inventory-output",
                str(inventory_file),
            ],
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("Build artifacts failed compatibility validation.")
        inventory = load_json(inventory_file)
    finally:
        inventory_file.unlink(missing_ok=True)

    paths = inventory.get("files")
    if not isinstance(paths, list) or not paths:
        raise ValueError("Artifact validator returned an empty release inventory.")
    if any(not isinstance(path, str) for path in paths):
        raise ValueError("Artifact validator returned a malformed release inventory.")
    if len(paths) != len(set(paths)):
        raise ValueError("Artifact validator returned duplicate release paths.")
    for relative_path in paths:
        parts = relative_path.split("/")
        if (
            not relative_path
            or relative_path.startswith("/")
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError(f"Artifact validator returned an unsafe path: {relative_path}")
    return sorted(paths)


def verify_archive(
    bundle_root: Path,
    archive_path: Path,
    archive_root: str,
    epoch: int,
) -> None:
    expected_files = {
        item.relative_to(bundle_root).as_posix(): (item.stat().st_size, sha256_file(item))
        for item in bundle_root.rglob("*")
        if item.is_file()
    }
    expected_directories = {""}
    expected_directories.update(
        item.relative_to(bundle_root).as_posix()
        for item in bundle_root.rglob("*")
        if item.is_dir()
    )
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    seen_names: set[str] = set()

    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            if member.name in seen_names:
                raise ValueError(f"Release archive contains a duplicate member: {member.name}")
            seen_names.add(member.name)
            if member.name == archive_root:
                relative_path = ""
            elif member.name.startswith(f"{archive_root}/"):
                relative_path = member.name[len(archive_root) + 1 :]
            else:
                raise ValueError(f"Release archive member escapes its root: {member.name}")
            if relative_path:
                parts = relative_path.split("/")
                if any(part in {"", ".", ".."} for part in parts):
                    raise ValueError(f"Release archive contains an unsafe path: {member.name}")
            if (
                member.uid != 0
                or member.gid != 0
                or member.uname
                or member.gname
                or member.mtime != epoch
            ):
                raise ValueError(f"Release archive metadata is not normalized: {member.name}")
            if member.isdir():
                if member.mode != 0o755:
                    raise ValueError(f"Release archive directory mode is invalid: {member.name}")
                actual_directories.add(relative_path)
                continue
            if not member.isfile() or member.mode != 0o644:
                raise ValueError(f"Release archive contains an unsupported member: {member.name}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(f"Release archive file cannot be read: {member.name}")
            digest = hashlib.sha256()
            for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                digest.update(chunk)
            expected = expected_files.get(relative_path)
            if expected != (member.size, digest.hexdigest()):
                raise ValueError(f"Release archive content differs: {member.name}")
            actual_files.add(relative_path)

    if actual_files != set(expected_files):
        raise ValueError("Release archive does not contain exactly the release files.")
    if actual_directories != expected_directories:
        raise ValueError("Release archive does not contain exactly the release directories.")


def create_archive(bundle_root: Path, archive_path: Path, archive_root: str, epoch: int) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{archive_path.name}.",
        suffix=".tmp",
        dir=archive_path.parent,
        delete=False,
    )
    temporary_archive = Path(temporary_output.name)
    members = [bundle_root]
    members.extend(sorted(bundle_root.rglob("*")))
    try:
        with temporary_output as raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw_output,
                mtime=epoch,
            ) as compressed_output:
                with tarfile.open(
                    fileobj=compressed_output,
                    mode="w",
                    format=tarfile.USTAR_FORMAT,
                ) as archive:
                    for item in members:
                        relative = item.relative_to(bundle_root)
                        archived_name = (
                            archive_root
                            if relative == Path(".")
                            else f"{archive_root}/{relative.as_posix()}"
                        )
                        info = archive.gettarinfo(str(item), arcname=archived_name)
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = epoch
                        info.mode = 0o755 if item.is_dir() else 0o644
                        if item.is_file():
                            with item.open("rb") as source:
                                archive.addfile(info, source)
                        else:
                            archive.addfile(info)
        verify_archive(bundle_root, temporary_archive, archive_root, epoch)
        normalize_file(temporary_archive, epoch)
        os.replace(temporary_archive, archive_path)
    finally:
        temporary_archive.unlink(missing_ok=True)


def validate_variant_metadata(
    manifest: dict[str, Any],
    version: dict[str, Any],
    locale: str,
    profile: str,
    text_size: str,
) -> tuple[str, int]:
    documentation_version = version.get("documentationVersion")
    format_version = version.get("artifactFormatVersion")
    if not isinstance(documentation_version, str) or not documentation_version:
        raise ValueError("Generated documentationVersion must be a non-empty string.")
    if not isinstance(format_version, int) or isinstance(format_version, bool):
        raise ValueError("Generated artifactFormatVersion must be an integer.")
    expected = {
        "locale": locale,
        "profile": profile,
        "textSize": text_size,
        "version": documentation_version,
        "schemaVersion": format_version,
    }
    for name, value in expected.items():
        if manifest.get(name) != value:
            raise ValueError(f"Manifest {name} does not match the selected release variant.")
    return documentation_version, format_version


def release_version_component(documentation_version: str) -> str:
    encoded = quote(documentation_version, safe="-._~")
    windows_stem = encoded.split(".", maxsplit=1)[0].upper()
    if encoded in {".", ".."} or encoded.endswith(".") or windows_stem in WINDOWS_RESERVED_NAMES:
        encoded = "".join(f"%{byte:02X}" for byte in documentation_version.encode("utf-8"))
    return encoded


def ensure_release_directory(releases_root: Path, directory: Path) -> None:
    try:
        relative_parts = directory.relative_to(releases_root).parts
    except ValueError as error:
        raise ValueError(f"Release path escapes the release root: {directory}") from error
    current = releases_root
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Release output symlinks are not supported: {current}")
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise ValueError(f"Release output path is not a directory: {current}")


def publish_release(
    staging_root: Path,
    staged_archive: Path,
    final_root: Path,
    archive_path: Path,
) -> None:
    backup_root: Path | None = None
    installed_new_root = False
    if final_root.is_symlink():
        raise ValueError(f"Release output symlinks are not supported: {final_root}")
    if final_root.exists() and not final_root.is_dir():
        raise ValueError(f"Release output path is not a directory: {final_root}")
    if final_root.exists():
        backup_root = Path(
            tempfile.mkdtemp(prefix=f".{final_root.name}.previous-", dir=final_root.parent)
        )
        backup_root.rmdir()
        os.replace(final_root, backup_root)
    try:
        os.replace(staging_root, final_root)
        installed_new_root = True
        os.replace(staged_archive, archive_path)
    except OSError:
        if installed_new_root and final_root.exists():
            shutil.rmtree(final_root)
        if backup_root is not None and backup_root.exists():
            os.replace(backup_root, final_root)
        raise
    if backup_root is not None:
        shutil.rmtree(backup_root)


def create_release(args: argparse.Namespace) -> tuple[Path, Path, int]:
    docs_root = args.docs_root.resolve()
    artifact_root = args.artifact_root.resolve()
    for name, value in (
        ("locale", args.locale),
        ("profile", args.profile),
        ("size", args.size),
    ):
        if not VARIANT_VALUE_PATTERN.fullmatch(value):
            raise ValueError(f"Release {name} is not a safe path value: {value!r}")

    manifest = load_json(artifact_root / "documentation-manifest.json")
    version = load_json(artifact_root / "version.json")
    documentation_version, format_version = validate_variant_metadata(
        manifest, version, args.locale, args.profile, args.size
    )
    release_version_path = release_version_component(documentation_version)
    epoch = source_date_epoch()
    generated_at = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source_hash = source_tree_hash(
        [docs_root / "source" / args.locale, docs_root / "template"], docs_root
    )
    manifest_paths = manifest.get("paths")
    if not isinstance(manifest_paths, dict):
        raise ValueError("Generated manifest paths must be an object.")
    if not isinstance(manifest_paths.get("pdf"), str):
        raise ValueError("Release requires a generated PDF artifact.")
    if not isinstance(manifest_paths.get("html"), str):
        raise ValueError("Release requires generated HTML artifacts.")
    inventory = validated_inventory(docs_root, artifact_root)

    releases_path = docs_root / "releases"
    if releases_path.is_symlink():
        raise ValueError(f"Release output symlinks are not supported: {releases_path}")
    releases_path.mkdir(parents=True, exist_ok=True)
    releases_root = releases_path.resolve()
    if releases_root.parent != docs_root:
        raise ValueError("Release output root must remain inside the documentation directory.")
    format_directory = f"format-v{format_version}"
    format_root = releases_root / release_version_path / format_directory
    locale_root = format_root / args.locale
    ensure_release_directory(releases_root, locale_root)
    final_root = (
        locale_root / f"{args.profile}-{args.size}"
    )
    archive_stem = (
        f"jelica-documentation-{release_version_path}-{format_directory}-"
        f"{args.locale}-{args.profile}-{args.size}"
    )
    archive_path = format_root / f"{archive_stem}.tar.gz"
    staging_root = Path(tempfile.mkdtemp(prefix=".release-", dir=releases_root))
    archive_staging_root = Path(
        tempfile.mkdtemp(prefix=".release-archive-", dir=releases_root)
    )
    staged_archive = archive_staging_root / archive_path.name

    try:
        for relative_path in inventory:
            parts = relative_path.split("/")
            copy_file(
                artifact_root.joinpath(*parts), staging_root.joinpath(*parts), epoch
            )

        release_metadata = {
            "releaseVersion": documentation_version,
            "artifactFormatVersion": format_version,
            "locale": args.locale,
            "profile": args.profile,
            "textSize": args.size,
            "generatedAt": generated_at,
            "sourceHash": source_hash,
        }
        write_json(staging_root / "release.json", release_metadata, epoch)
        validate_release(docs_root, staging_root)

        entries = checksum_entries(staging_root)
        checksum_manifest = {"algorithm": "SHA-256", "files": entries}
        write_json(staging_root / "checksums.json", checksum_manifest, epoch)
        verify_checksums(staging_root, entries)
        normalize_directories(staging_root, epoch)
        create_archive(staging_root, staged_archive, archive_stem, epoch)
        publish_release(staging_root, staged_archive, final_root, archive_path)
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        if archive_staging_root.exists():
            shutil.rmtree(archive_staging_root)

    return final_root, archive_path, len(entries)


def main() -> int:
    args = parse_args()
    try:
        release_root, archive_path, file_count = create_release(args)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Documentation release directory: {release_root}")
    print(f"Documentation release archive: {archive_path}")
    print(f"Documentation release checksums: {file_count} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
