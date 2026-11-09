#!/usr/bin/env python3
"""Inspect incomplete launcher release uploads and explicitly remove an orphan."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import server


ALLOWED_STAGED_FILES = {"launcher.bin", "graphics-module.ps1"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_root() -> Path:
    data_dir = server._data_dir().resolve()
    return data_dir / "releases"


def inspect_staged_releases(version: str | None = None) -> list[dict[str, object]]:
    server._initialize()
    root = _release_root()
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Release asset path is not a regular directory: {root}")

    with server.DB_LOCK, server._connect() as connection:
        published = {
            row["version"]
            for row in connection.execute("SELECT version FROM releases").fetchall()
        }

    reports: list[dict[str, object]] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if version is not None and directory.name != version:
            continue
        if directory.is_symlink() or not directory.is_dir():
            continue
        if not server._is_launcher_release_version(directory.name) or directory.name in published:
            continue
        files: list[dict[str, object]] = []
        timestamps: list[float] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            item: dict[str, object] = {"name": path.name}
            if path.is_symlink():
                item["type"] = "symlink"
            elif path.is_file():
                stat = path.stat()
                timestamps.append(stat.st_mtime)
                item.update({"type": "file", "size": stat.st_size, "sha256": _sha256(path)})
            elif path.is_dir():
                item["type"] = "directory"
            else:
                item["type"] = "other"
            files.append(item)
        created = min(timestamps) if timestamps else directory.stat().st_mtime
        reports.append(
            {
                "version": directory.name,
                "directory": str(directory),
                "oldestFileUtc": datetime.fromtimestamp(created, timezone.utc).isoformat(),
                "files": files,
            }
        )
    return reports


def remove_staged_release(version: str, expected_files: list[dict[str, object]]) -> int:
    if not server._is_launcher_release_version(version):
        raise ValueError("Release version is invalid.")
    server._initialize()
    root = _release_root()
    target = root / version
    if target.parent != root or target.is_symlink() or not target.is_dir():
        raise ValueError(f"Staged release directory does not exist or is unsafe: {target}")

    removed = 0
    # BEGIN IMMEDIATE serializes this check/removal against API uploads, whose
    # file replacement and release-row writes also happen inside a write txn.
    with server.DB_LOCK, server._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        published = connection.execute(
            "SELECT 1 FROM releases WHERE version = ?", (version,)
        ).fetchone()
        if published is not None:
            connection.rollback()
            raise ValueError("Refusing cleanup: this release version is published.")

        entries = list(target.iterdir())
        current_files: list[dict[str, object]] = []
        for path in entries:
            if path.is_symlink() or not path.is_file():
                connection.rollback()
                raise ValueError(f"Refusing cleanup: unexpected file or directory in staged release: {path}")
            if path.name not in ALLOWED_STAGED_FILES and not path.name.startswith(".upload-"):
                connection.rollback()
                raise ValueError(f"Refusing cleanup: unexpected staged filename: {path.name}")
            stat = path.stat()
            current_files.append(
                {"name": path.name, "type": "file", "size": stat.st_size, "sha256": _sha256(path)}
            )
        expected_inventory = sorted(
            (item.get("name"), item.get("type"), item.get("size"), item.get("sha256"))
            for item in expected_files
        )
        current_inventory = sorted(
            (item.get("name"), item.get("type"), item.get("size"), item.get("sha256"))
            for item in current_files
        )
        if current_inventory != expected_inventory:
            connection.rollback()
            raise ValueError("Refusing cleanup: staged files changed since they were displayed for confirmation; inspect again.")

        for path in entries:
            path.unlink()
            removed += 1
        target.rmdir()
        connection.commit()
    return removed


def _print_reports(reports: list[dict[str, object]]) -> None:
    if not reports:
        print("No unpublished staged launcher releases were found.")
        return
    for report in reports:
        print(f"Unpublished staged release {report['version']}")
        print(f"  directory: {report['directory']}")
        print(f"  oldest file UTC: {report['oldestFileUtc']}")
        files = report["files"]
        if not files:
            print("  (empty directory)")
        for item in files:
            if item["type"] == "file":
                print(f"  {item['name']}  {item['size']} bytes  sha256={item['sha256']}")
            else:
                print(f"  {item['name']}  unexpected {item['type']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only inspect incomplete launcher releases, or explicitly clean one unpublished staging directory."
    )
    parser.add_argument("--version", help="Inspect only this version")
    parser.add_argument("--remove-version", help="Prompt to remove one unpublished staged version")
    args = parser.parse_args()
    if args.version and args.remove_version:
        parser.error("Use --version or --remove-version, not both.")

    try:
        if args.remove_version:
            reports = inspect_staged_releases(args.remove_version)
            if not reports:
                print("No unpublished staged release with that version was found.")
                return 1
            _print_reports(reports)
            expected = f"REMOVE {args.remove_version}"
            answer = input(f"Type {expected!r} to remove only these unpublished staging files: ")
            if answer != expected:
                print("Cleanup canceled; no files were removed.")
                return 1
            removed = remove_staged_release(args.remove_version, reports[0]["files"])
            print(f"Removed {removed} staged file(s) from unpublished release {args.remove_version}.")
            return 0
        _print_reports(inspect_staged_releases(args.version))
        return 0
    except (OSError, sqlite3.Error, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
