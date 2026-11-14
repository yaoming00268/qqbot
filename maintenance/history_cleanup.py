"""Compact disposable historical checkouts without losing their contents.

Reports and source backups stay in place. Each snapshot file is stored once by
SHA-256 in a verified ZIP; only then may the original generated trees be removed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import time
import zipfile


ROOT = Path(__file__).resolve().parents[1]
GENERATED = re.compile(r"(?:validation(?:_[A-Za-z0-9_]+)?|live_[0-9a-f]+|engine_fixture_[0-9a-f]+|chrome-smoke)\Z")


def checked_target(root, path):
    root, path = Path(root).resolve(), Path(path).absolute()
    relative = path.relative_to(root)
    if (len(relative.parts) != 3 or relative.parts[0] not in
            {"review_artifacts", "implementation_artifacts"}
            or not GENERATED.fullmatch(relative.name)):
        raise ValueError(f"Not a generated historical checkout: {path}")
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError(f"Refusing a reparse point: {parent}")
    if path.resolve() != path or not path.resolve().is_relative_to(root):
        raise ValueError(f"Target escaped the workspace: {path}")
    return path


def discover(root):
    found = []
    for name in ("review_artifacts", "implementation_artifacts"):
        for case in sorted((root / name).iterdir()):
            if case.is_dir() and not case.is_symlink():
                for target in sorted(case.iterdir()):
                    if target.is_dir() and GENERATED.fullmatch(target.name):
                        found.append(checked_target(root, target))
    return found


def files_under(root, targets):
    for target in targets:
        checked_target(root, target)
        for base, directories, names in os.walk(target, followlinks=False):
            for name in directories:
                directory = Path(base) / name
                if directory.is_symlink() or (hasattr(directory, "is_junction") and directory.is_junction()):
                    raise ValueError(f"Refusing a nested reparse point: {directory}")
            for name in names:
                path = Path(base) / name
                if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError(f"Not a regular snapshot file: {path}")
                yield path


def digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        hashes = {row["sha256"] for row in manifest["files"]}
        for digest in hashes:
            with archive.open("objects/" + digest) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise ValueError(f"Corrupted archived object: {digest}")
        if {info.filename for info in archive.infolist()} != {"manifest.json", *("objects/" + value for value in hashes)}:
            raise ValueError("Archive manifest and objects do not match")
    return manifest


def compact(root, destination, *, apply=False):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    targets = discover(root)
    if any(destination.is_relative_to(target) for target in targets):
        raise ValueError("Archive must be outside the deletion targets")
    if not apply:
        sizes = [{"path": str(path.relative_to(root)), "bytes": sum(item.stat().st_size
                    for item in files_under(root, [path]))} for path in targets]
        return {"mode": "preview", "targets": sizes, "bytes": sum(item["bytes"] for item in sizes)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError("Choose a new archive path; existing evidence is never overwritten")
    temporary = destination.with_suffix(".partial")
    if temporary.exists():
        raise FileExistsError(f"Inspect incomplete archive first: {temporary}")
    manifest = {"version": 1, "created": time.time(), "root": str(root),
                "targets": [str(path.relative_to(root)) for path in targets], "files": []}
    objects = set()
    with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as archive:
        for number, path in enumerate(files_under(root, targets), 1):
            before = path.stat()
            digest = digest_file(path)
            row = {"path": path.relative_to(root).as_posix(), "bytes": before.st_size,
                   "mtime_ns": before.st_mtime_ns, "sha256": digest}
            if digest not in objects:
                archive.write(path, "objects/" + digest)
                objects.add(digest)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError(f"Snapshot changed while archiving: {path}")
            manifest["files"].append(row)
            if number % 5000 == 0:
                print(json.dumps({"archived_files": number, "unique_objects": len(objects)}), flush=True)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
    verified = verify_archive(temporary)
    # Recheck every source before deleting any tree. No mutation outside the
    # explicitly recognized historical directories is ever permitted.
    for row in verified["files"]:
        path = root / row["path"]
        current = path.stat()
        if (current.st_size, current.st_mtime_ns) != (row["bytes"], row["mtime_ns"]):
            raise ValueError(f"Snapshot changed before cleanup: {path}")
    expected = {row["path"] for row in verified["files"]}
    if {path.relative_to(root).as_posix() for path in files_under(root, targets)} != expected:
        raise ValueError("Snapshot file set changed before cleanup")
    temporary.replace(destination)
    removed = []
    for target in targets:
        checked_target(root, target)
        shutil.rmtree(target)
        removed.append(str(target.relative_to(root)))
    original = sum(row["bytes"] for row in verified["files"])
    return {"mode": "applied", "archive": str(destination), "verified": True,
            "removed_targets": removed, "files": len(verified["files"]), "unique_objects": len(objects),
            "original_bytes": original, "archive_bytes": destination.stat().st_size,
            "saved_bytes": original - destination.stat().st_size}


def restore_archive(archive_path, destination, targets=()):
    """Restore selected snapshot files into a new directory, never overwrite."""
    manifest = verify_archive(archive_path)
    requested = {str(Path(value)).replace("\\", "/") for value in targets}
    known = {str(Path(value)).replace("\\", "/") for value in manifest["targets"]}
    if requested - known:
        raise ValueError(f"Unknown snapshot targets: {sorted(requested - known)}")
    rows = [row for row in manifest["files"] if not requested or any(
        row["path"].startswith(value + "/") for value in requested)]
    destination = Path(destination).resolve()
    for row in rows:
        relative = Path(row["path"])
        if (relative.is_absolute() or PureWindowsPath(row["path"]).drive or ".." in relative.parts
                or not (destination / relative).resolve().is_relative_to(destination)):
            raise ValueError(f"Unsafe snapshot path: {row['path']}")
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        for row in rows:
            path = destination / row["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open("objects/" + row["sha256"]) as source, path.open("xb") as output:
                shutil.copyfileobj(source, output)
            os.utime(path, ns=(row["mtime_ns"], row["mtime_ns"]))
    return {"mode": "restored", "directory": str(destination), "files": len(rows),
            "bytes": sum(row["bytes"] for row in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--archive", type=Path, default=ROOT / "maintenance_reports/cleanup_20261108/history-snapshots.zip")
    parser.add_argument("--restore-to", type=Path)
    parser.add_argument("--target", action="append", default=[], help="Restore just this manifest target (repeatable)")
    args = parser.parse_args()
    if args.restore_to:
        print(json.dumps(restore_archive(args.archive, args.restore_to, args.target), ensure_ascii=False, indent=2))
        return
    if args.target:
        parser.error("--target requires --restore-to")
    result = compact(ROOT, args.archive, apply=args.apply)
    output = args.archive.with_suffix(".result.json" if args.apply else ".preview.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
