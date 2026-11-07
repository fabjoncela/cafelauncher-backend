#!/usr/bin/env python3
"""Validate and import current CafeLauncher profile data into the local API DB."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import server
from validation import PROFILE_CFG_FILES, normalize_graphics, normalize_profile, validate_cfg


def load_bundle(repo_root: Path) -> list[tuple[str, dict, dict, str]]:
    profiles_dir = repo_root / "profiles"
    graphics_dir = repo_root / "graphics"
    if not profiles_dir.is_dir() or not graphics_dir.is_dir():
        raise ValueError("Repository root must contain profiles/ and graphics/ directories.")

    bundles = []
    for profile_name, cfg_name in PROFILE_CFG_FILES.items():
        profile_path = profiles_dir / f"{profile_name}.json"
        graphics_path = graphics_dir / f"{profile_name}.json"
        cfg_path = repo_root / cfg_name
        for path in (profile_path, graphics_path, cfg_path):
            if not path.is_file():
                raise ValueError(f"Required source file is missing: {path}")
        try:
            raw_profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
            raw_graphics = json.loads(graphics_path.read_text(encoding="utf-8-sig"))
            cfg = cfg_path.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Could not read source data for {profile_name}: {error}") from error

        profile = normalize_profile(profile_name, raw_profile, allow_legacy_cfg_url=True)
        graphics = normalize_graphics(profile_name, raw_graphics, profile)
        cfg = validate_cfg(cfg)
        bundles.append((profile_name, profile, graphics, cfg))
    return bundles


def import_bundles(bundles: list[tuple[str, dict, dict, str]]) -> int:
    server._initialize()
    created_utc = server._utc_now()
    with server.DB_LOCK, server._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT COUNT(*) FROM profile_revisions").fetchone()[0]
        if existing:
            connection.rollback()
            raise ValueError("The backend already contains profile revisions; refusing to overwrite or mix imported data.")
        for profile_name, profile, graphics, cfg in bundles:
            connection.execute(
                "INSERT INTO profile_revisions "
                "(profile_name, revision, created_utc, profile_json, graphics_json, cfg_text) "
                "VALUES (?, 1, ?, ?, ?, ?)",
                (
                    profile_name,
                    created_utc,
                    server._json_bytes(profile).decode("utf-8"),
                    server._json_bytes(graphics).decode("utf-8"),
                    cfg,
                ),
            )
        connection.commit()
    return len(bundles)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import checked-in CafeLauncher profiles, graphics presets, and CFGs into the local backend database.")
    parser.add_argument("--repo-root", required=True, type=Path, help="Read-only CafeLauncher repository path")
    parser.add_argument("--dry-run", action="store_true", help="Validate and summarize without writing backend data")
    args = parser.parse_args()

    repo_root = args.repo_root.expanduser().resolve()
    bundles = load_bundle(repo_root)
    if args.dry_run:
        print(f"Validated {len(bundles)} profile bundles from {repo_root}; backend database was not changed.")
        for profile_name, _, _, _ in bundles:
            print(f"  {profile_name}: profile + graphics + CFG")
        return

    count = import_bundles(bundles)
    print(f"Imported {count} profile bundles into {server._db_path()} as revision 1.")
    print("Legacy GitHub CFG URLs were normalized to /v1/cfg/{file} routes.")


if __name__ == "__main__":
    main()
