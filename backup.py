#!/usr/bin/env python3
"""Create an online SQLite backup plus a copy of backend asset directories."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import server


def create_backup(destination: Path) -> Path:
    destination = destination.expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Backup destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    source_db = server._db_path()
    if not source_db.is_file():
        raise ValueError(f"Backend database does not exist: {source_db}")
    target_db = destination / source_db.name
    with closing(sqlite3.connect(source_db)) as source, closing(sqlite3.connect(target_db)) as target:
        source.backup(target)

    data_dir = server._data_dir()
    for asset_dir in ("releases", "tools"):
        source_assets = data_dir / asset_dir
        if source_assets.is_dir():
            shutil.copytree(source_assets, destination / asset_dir, dirs_exist_ok=True)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the backend SQLite database and published assets.")
    parser.add_argument("destination", type=Path, help="New or empty private backup directory")
    args = parser.parse_args()
    try:
        result = create_backup(args.destination)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.error(str(error))
    print(f"Backend backup created at {result}")
    print("For a consistent database and asset snapshot, pause publishing while this command runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
