#!/usr/bin/env python3
"""Small, dependency-free CafeLauncher profile and release API prototype."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from validation import PROFILE_CFG_FILES, normalize_graphics, normalize_profile, validate_cfg


APP_VERSION = "0.1.0"
MAX_SYNC_BYTES = 4 * 1024 * 1024
MAX_CFG_BYTES = 2 * 1024 * 1024
MIN_LAUNCHER_BYTES = 10_000
MAX_LAUNCHER_BYTES = 1_000_000
MAX_MODULE_BYTES = 8 * 1024 * 1024
MAX_TOOL_BYTES = 32 * 1024 * 1024
PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
LAUNCHER_RELEASE_VERSION = re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}\.\d+\Z")
TOOL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
DB_LOCK = threading.RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _launcher_version_key(version: str) -> tuple[int, int, int, int]:
    return tuple(int(part) for part in version.split("."))


def _is_launcher_release_version(version: str) -> bool:
    if LAUNCHER_RELEASE_VERSION.fullmatch(version) is None:
        return False
    try:
        year, month, day, _sequence = (int(part) for part in version.split("."))
        date(year, month, day)
    except ValueError:
        return False
    return True


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (UnicodeEncodeError, RecursionError, TypeError) as error:
        raise ValueError("JSON contains unsupported or invalid data.") from error


def _data_dir() -> Path:
    configured = os.environ.get("CAFELAUNCHER_DATA_DIR")
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parent / "data"


def _db_path() -> Path:
    return _data_dir() / "cafelauncher.sqlite3"


@contextmanager
def _connect():
    connection = sqlite3.connect(_db_path(), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        yield connection
    finally:
        connection.close()


def _initialize() -> None:
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "releases").mkdir(exist_ok=True)
    with _connect() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS profile_revisions (
                profile_name TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_utc TEXT NOT NULL,
                profile_json TEXT NOT NULL,
                graphics_json TEXT NOT NULL,
                cfg_text TEXT NOT NULL,
                PRIMARY KEY (profile_name, revision)
            );
            CREATE INDEX IF NOT EXISTS ix_profile_revisions_created
                ON profile_revisions(profile_name, revision DESC);
            CREATE TABLE IF NOT EXISTS releases (
                version TEXT PRIMARY KEY,
                published_utc TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                launcher_sha256 TEXT NOT NULL,
                launcher_size INTEGER NOT NULL,
                module_sha256 TEXT NOT NULL,
                module_size INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tool_assets (
                tool_name TEXT NOT NULL,
                version TEXT NOT NULL,
                published_utc TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                PRIMARY KEY (tool_name, version)
            );
            CREATE TABLE IF NOT EXISTS github_mirror_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                generation INTEGER NOT NULL,
                mirrored_generation INTEGER NOT NULL,
                last_attempt_utc TEXT,
                last_success_utc TEXT,
                last_error TEXT
            );
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO github_mirror_state "
            "(singleton, generation, mirrored_generation, last_attempt_utc, last_success_utc, last_error) "
            "VALUES (1, 1, 0, NULL, NULL, NULL)"
        )


def _mark_github_mirror_dirty(connection: sqlite3.Connection) -> int:
    connection.execute("UPDATE github_mirror_state SET generation = generation + 1 WHERE singleton = 1")
    return connection.execute("SELECT generation FROM github_mirror_state WHERE singleton = 1").fetchone()[0]


class BoundedHTTPServer(ThreadingHTTPServer):
    """Keep slow or concurrent public requests from creating unbounded threads."""

    daemon_threads = True
    request_queue_size = 64
    max_workers = 32

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._worker_slots = threading.BoundedSemaphore(self.max_workers)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:
        if not self._worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "CafeLauncherBackend/" + APP_VERSION
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, _format: str, *_args: object) -> None:
        # Avoid BaseHTTPRequestHandler's default logging of raw request paths.
        parsed = urlsplit(self.path)
        safe_path = parsed.path[:180]
        print(f"{_utc_now()} {self.client_address[0]} {self.command} {safe_path} {getattr(self, '_status', 0)}")

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._status = status
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, value: object, **kwargs: object) -> None:
        self._send_bytes(status, _json_bytes(value), "application/json; charset=utf-8", **kwargs)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": code, "message": message})

    def _body(self, maximum: int) -> bytes | None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._error(411, "length_required", "Content-Length is required.")
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self._error(400, "invalid_content_length", "Content-Length is invalid.")
            return None
        if length < 0 or length > maximum:
            self._error(413, "request_too_large", "Request body exceeds the allowed size.")
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self._error(400, "incomplete_body", "Request body was incomplete.")
            return None
        return body

    def _json_body(self, maximum: int) -> object | None:
        body = self._body(maximum)
        if body is None:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            self._error(400, "invalid_json", "Request body must be valid UTF-8 JSON.")
            return None

    def _authorized(self, env_name: str) -> bool:
        expected = os.environ.get(env_name, "")
        if len(expected) < 32:
            self._error(503, "authorization_unconfigured", "The required API token is not configured on the server.")
            return False
        header = self.headers.get("Authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, expected):
            self._error(401, "unauthorized", "A valid bearer token is required.")
            return False
        return True

    @staticmethod
    def _revision_row(profile_name: str, revision: int | None = None) -> sqlite3.Row | None:
        with _connect() as connection:
            if revision is None:
                return connection.execute(
                    "SELECT * FROM profile_revisions WHERE profile_name = ? ORDER BY revision DESC LIMIT 1",
                    (profile_name,),
                ).fetchone()
            return connection.execute(
                "SELECT * FROM profile_revisions WHERE profile_name = ? AND revision = ?",
                (profile_name, revision),
            ).fetchone()

    def _send_revision_value(self, row: sqlite3.Row, field: str, content_type: str) -> None:
        value = row[field]
        body = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        etag = f'"{row["profile_name"]}-{row["revision"]}"'
        if self.headers.get("If-None-Match") == etag:
            self._status = 304
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self._send_bytes(
            200,
            body,
            content_type,
            cache_control="no-cache",
            headers={"ETag": etag, "X-Profile-Revision": str(row["revision"])},
        )

    @staticmethod
    def _valid_cfg(value: object) -> bool:
        try:
            validate_cfg(value, max_bytes=MAX_CFG_BYTES)
            return True
        except ValueError:
            return False

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]

        if parts == ["v1", "health"]:
            try:
                with _connect() as connection:
                    connection.execute("SELECT 1").fetchone()
            except sqlite3.Error:
                self._error(503, "storage_unavailable", "Backend storage is unavailable.")
                return
            self._send_json(200, {"status": "ok", "version": APP_VERSION}, cache_control="no-store")
            return

        if len(parts) == 3 and parts[0:2] in (["v1", "profiles"], ["v1", "graphics"], ["v1", "cfg"]):
            resource_name = parts[2]
            if parts[1] == "cfg":
                profile_name = next((name for name, filename in PROFILE_CFG_FILES.items() if filename == resource_name), None)
                if profile_name is None:
                    self._error(400, "invalid_cfg_name", "CFG filename is invalid.")
                    return
            else:
                profile_name = resource_name
                if not PROFILE_NAME.fullmatch(profile_name):
                    self._error(400, "invalid_profile_name", "Profile name is invalid.")
                    return
            row = self._revision_row(profile_name)
            if row is None:
                self._error(404, "not_found", "Profile has not been published.")
                return
            if parts[1] == "profiles":
                self._send_revision_value(row, "profile_json", "application/json; charset=utf-8")
            elif parts[1] == "graphics":
                self._send_revision_value(row, "graphics_json", "application/json; charset=utf-8")
            else:
                self._send_revision_value(row, "cfg_text", "text/plain; charset=utf-8")
            return

        if len(parts) == 4 and parts[:2] == ["v1", "graphics"] and parts[3] == "history":
            profile_name = parts[2]
            if not PROFILE_NAME.fullmatch(profile_name):
                self._error(400, "invalid_profile_name", "Profile name is invalid.")
                return
            query = parse_qs(parsed.query)
            try:
                # The limit means older versions, like the launcher UI. Include
                # the current graphics snapshot so an adapter can skip it.
                limit = int(query.get("limit", ["3"])[0])
            except ValueError:
                self._error(400, "invalid_limit", "History limit must be an integer.")
                return
            limit = max(1, min(limit, 20))
            with _connect() as connection:
                rows = connection.execute(
                    "WITH graphics_changes AS ("
                    " SELECT revision, created_utc, graphics_json, "
                    " LAG(graphics_json) OVER (PARTITION BY profile_name ORDER BY revision) AS previous_graphics "
                    " FROM profile_revisions WHERE profile_name = ?"
                    "), ranked_changes AS ("
                    " SELECT revision, created_utc, graphics_json, "
                    " ROW_NUMBER() OVER (ORDER BY revision DESC) AS history_rank "
                    " FROM graphics_changes "
                    " WHERE previous_graphics IS NULL OR graphics_json != previous_graphics"
                    ") SELECT revision, created_utc, graphics_json FROM ranked_changes "
                    "WHERE history_rank <= ? ORDER BY revision DESC",
                    (profile_name, limit + 1),
                ).fetchall()
            self._send_json(
                200,
                {
                    "profile": profile_name,
                    "revisions": [
                        {
                            "revision": row["revision"],
                            "createdUtc": row["created_utc"],
                            "graphics": json.loads(row["graphics_json"]),
                        }
                        for row in rows
                    ],
                },
                cache_control="no-cache",
            )
            return

        if parts == ["v1", "releases", "latest"]:
            with _connect() as connection:
                rows = connection.execute("SELECT version, manifest_json FROM releases").fetchall()
            if not rows:
                self._error(404, "no_release", "No launcher release has been published.")
            else:
                row = max(rows, key=lambda item: _launcher_version_key(item["version"]))
                self._send_bytes(200, row["manifest_json"].encode("utf-8"), "application/json; charset=utf-8", cache_control="no-cache")
            return

        if len(parts) == 4 and parts[:2] == ["v1", "tools"] and parts[3] == "latest":
            tool_name = parts[2]
            if tool_name != "logihub":
                self._error(404, "tool_not_found", "Tool was not found.")
                return
            with _connect() as connection:
                row = connection.execute(
                    "SELECT * FROM tool_assets WHERE tool_name = ? ORDER BY published_utc DESC, rowid DESC LIMIT 1",
                    (tool_name,),
                ).fetchone()
            if row is None:
                self._error(404, "tool_not_published", "No version of this tool has been published.")
                return
            self._send_json(
                200,
                {
                    "tool": tool_name,
                    "version": row["version"],
                    "publishedUtc": row["published_utc"],
                    "asset": {
                        "url": f"/v1/tools/{tool_name}/{row['version']}",
                        "size": row["size"],
                        "sha256": row["sha256"],
                    },
                },
                cache_control="no-cache",
            )
            return

        if len(parts) == 4 and parts[:2] == ["v1", "tools"]:
            tool_name, version = parts[2], parts[3]
            if tool_name != "logihub" or not TOOL_VERSION.fullmatch(version):
                self._error(404, "tool_not_found", "Tool version was not found.")
                return
            with _connect() as connection:
                row = connection.execute(
                    "SELECT sha256, size FROM tool_assets WHERE tool_name = ? AND version = ?",
                    (tool_name, version),
                ).fetchone()
            if row is None:
                self._error(404, "tool_not_found", "Tool version was not found.")
                return
            path = _data_dir() / "tools" / tool_name / version / "asset.bin"
            try:
                body = path.read_bytes()
            except OSError:
                self._error(503, "tool_asset_unavailable", "Tool asset is unavailable.")
                return
            if len(body) != row["size"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
                self._error(503, "tool_asset_integrity", "Tool asset failed its stored size/hash check.")
                return
            self._send_bytes(200, body, "application/octet-stream", cache_control="public, max-age=31536000, immutable")
            return

        if len(parts) == 4 and parts[:2] == ["v1", "releases"] and parts[3] in ("launcher", "graphics-module"):
            version, kind = parts[2], parts[3]
            if not _is_launcher_release_version(version):
                self._error(400, "invalid_release_version", "Release version is invalid.")
                return
            with _connect() as connection:
                row = connection.execute(
                    "SELECT launcher_sha256, launcher_size, module_sha256, module_size "
                    "FROM releases WHERE version = ?",
                    (version,),
                ).fetchone()
            if row is None:
                self._error(404, "release_not_found", "Release was not found.")
                return
            path = _data_dir() / "releases" / version / ("launcher.bin" if kind == "launcher" else "graphics-module.ps1")
            try:
                body = path.read_bytes()
            except OSError:
                self._error(503, "release_asset_unavailable", "Release asset is unavailable.")
                return
            expected_sha256 = row["launcher_sha256"] if kind == "launcher" else row["module_sha256"]
            expected_size = row["launcher_size"] if kind == "launcher" else row["module_size"]
            if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_sha256:
                self._error(503, "release_asset_integrity", "Release asset failed its stored size/hash check.")
                return
            content_type = "application/octet-stream" if kind == "launcher" else "text/plain; charset=utf-8"
            self._send_bytes(200, body, content_type, cache_control="public, max-age=31536000, immutable")
            return

        self._error(404, "not_found", "Endpoint was not found.")

    def do_PUT(self) -> None:
        parsed = urlsplit(self.path)
        parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]

        if len(parts) == 4 and parts[:2] == ["v1", "profiles"] and parts[3] == "sync":
            self._put_profile_sync(parts[2])
            return

        if len(parts) == 5 and parts[:3] == ["v1", "admin", "releases"] and parts[4] in ("launcher", "graphics-module"):
            self._put_release_asset(parts[3], parts[4])
            return

        if len(parts) == 5 and parts[:3] == ["v1", "admin", "releases"] and parts[4] == "manifest":
            self._put_release_manifest(parts[3])
            return

        if len(parts) == 5 and parts[:3] == ["v1", "admin", "tools"]:
            self._put_tool_asset(parts[3], parts[4])
            return

        self._error(404, "not_found", "Endpoint was not found.")

    def _put_profile_sync(self, profile_name: str) -> None:
        if not PROFILE_NAME.fullmatch(profile_name):
            self._error(400, "invalid_profile_name", "Profile name is invalid.")
            return
        if not self._authorized("CAFELAUNCHER_SYNC_TOKEN"):
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._error(415, "unsupported_media_type", "Sync requests must use application/json.")
            return
        payload = self._json_body(MAX_SYNC_BYTES)
        if payload is None:
            return
        if not isinstance(payload, dict):
            self._error(400, "invalid_sync", "Sync body must be a JSON object.")
            return
        unknown = set(payload) - {"expectedRevision", "profile", "graphics", "cfg"}
        if unknown:
            self._error(400, "invalid_sync", "Sync body contains unsupported fields.")
            return
        profile = payload.get("profile")
        graphics = payload.get("graphics")
        cfg = payload.get("cfg")
        expected_revision = payload.get("expectedRevision")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            self._error(400, "invalid_revision", "expectedRevision must be a non-negative integer.")
            return
        if not isinstance(profile, dict) or not isinstance(graphics, dict):
            self._error(400, "invalid_sync", "profile and graphics must be JSON objects.")
            return
        try:
            profile = normalize_profile(profile_name, profile)
        except ValueError as error:
            self._error(422, "invalid_profile", str(error))
            return
        try:
            graphics = normalize_graphics(profile_name, graphics, profile)
        except ValueError as error:
            self._error(422, "invalid_graphics", str(error))
            return
        try:
            cfg = validate_cfg(cfg, max_bytes=MAX_CFG_BYTES)
        except ValueError as error:
            self._error(422, "invalid_cfg", str(error))
            return

        try:
            profile_json = _json_bytes(profile).decode("utf-8")
            graphics_json = _json_bytes(graphics).decode("utf-8")
        except ValueError as error:
            self._error(422, "invalid_sync", str(error))
            return
        created_utc = _utc_now()
        with DB_LOCK, _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT revision, created_utc, profile_json, graphics_json, cfg_text "
                "FROM profile_revisions WHERE profile_name = ? ORDER BY revision DESC LIMIT 1",
                (profile_name,),
            ).fetchone()
            current = current_row["revision"] if current_row is not None else 0
            if current != expected_revision:
                connection.rollback()
                self._send_json(
                    409,
                    {"error": "revision_conflict", "message": "Profile changed since this client read it.", "currentRevision": current},
                )
                return
            unchanged = bool(
                current_row is not None
                and current_row["profile_json"] == profile_json
                and current_row["graphics_json"] == graphics_json
                and current_row["cfg_text"] == cfg
            )
            if unchanged:
                new_revision = current
                created_utc = current_row["created_utc"]
            else:
                new_revision = current + 1
                connection.execute(
                    "INSERT INTO profile_revisions "
                    "(profile_name, revision, created_utc, profile_json, graphics_json, cfg_text) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (profile_name, new_revision, created_utc, profile_json, graphics_json, cfg),
                )
                _mark_github_mirror_dirty(connection)
            connection.commit()

        response = {
            "profile": profile_name,
            "revision": new_revision,
            "createdUtc": created_utc,
            "unchanged": unchanged,
            "hashes": {
                "profile": hashlib.sha256(profile_json.encode("utf-8")).hexdigest(),
                "graphics": hashlib.sha256(graphics_json.encode("utf-8")).hexdigest(),
                "cfg": hashlib.sha256(cfg.encode("utf-8")).hexdigest(),
            },
        }
        self._send_json(200, response, headers={"ETag": f'"{profile_name}-{new_revision}"'})

    def _put_release_asset(self, version: str, kind: str) -> None:
        if not _is_launcher_release_version(version):
            self._error(400, "invalid_release_version", "Release version is invalid.")
            return
        if not self._authorized("CAFELAUNCHER_PUBLISH_TOKEN"):
            return
        with _connect() as connection:
            published = connection.execute("SELECT 1 FROM releases WHERE version = ?", (version,)).fetchone()
        if published is not None:
            self._error(409, "release_immutable", "Published releases cannot be modified.")
            return
        max_size = MAX_LAUNCHER_BYTES if kind == "launcher" else MAX_MODULE_BYTES
        body = self._body(max_size)
        if body is None:
            return
        if not body:
            self._error(400, "empty_asset", "Release asset cannot be empty.")
            return
        if kind == "launcher" and len(body) < MIN_LAUNCHER_BYTES:
            self._error(422, "launcher_asset_too_small", "Launcher asset must be at least 10,000 bytes.")
            return
        with DB_LOCK, _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            published = connection.execute("SELECT 1 FROM releases WHERE version = ?", (version,)).fetchone()
            if published is not None:
                connection.rollback()
                self._error(409, "release_immutable", "Published releases cannot be modified.")
                return
            release_dir = _data_dir() / "releases" / version
            release_dir.mkdir(parents=True, exist_ok=True)
            filename = "launcher.bin" if kind == "launcher" else "graphics-module.ps1"
            target = release_dir / filename
            if target.exists():
                try:
                    existing_body = target.read_bytes()
                except OSError:
                    connection.rollback()
                    self._error(503, "release_asset_unavailable", "Previously staged release asset is unavailable.")
                    return
                if existing_body != body:
                    connection.rollback()
                    self._error(409, "release_asset_conflict", "A different asset is already staged for this release version.")
                    return
                connection.commit()
                self._send_json(
                    201,
                    {"version": version, "asset": kind, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()},
                )
                return
            temporary_name = None
            try:
                with tempfile.NamedTemporaryFile(dir=release_dir, prefix=".upload-", delete=False) as temporary:
                    temporary.write(body)
                    temporary_name = temporary.name
                os.replace(temporary_name, target)
            finally:
                if temporary_name and os.path.exists(temporary_name):
                    os.unlink(temporary_name)
            connection.commit()
        self._send_json(
            201,
            {"version": version, "asset": kind, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()},
        )

    def _put_release_manifest(self, version: str) -> None:
        if not _is_launcher_release_version(version):
            self._error(400, "invalid_release_version", "Release version is invalid.")
            return
        if not self._authorized("CAFELAUNCHER_PUBLISH_TOKEN"):
            return
        payload = self._json_body(64 * 1024)
        if payload is None:
            return
        if not isinstance(payload, dict):
            self._error(400, "invalid_manifest", "Manifest body must be a JSON object.")
            return
        notes = payload.get("releaseNotes", "")
        if not isinstance(notes, str) or len(notes) > 4000:
            self._error(422, "invalid_release_notes", "releaseNotes must be text no longer than 4000 characters.")
            return

        with DB_LOCK, _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM releases WHERE version = ?", (version,)).fetchone() is not None:
                connection.rollback()
                self._error(409, "release_immutable", "That release version has already been published.")
                return
            release_dir = _data_dir() / "releases" / version
            launcher_path = release_dir / "launcher.bin"
            module_path = release_dir / "graphics-module.ps1"
            try:
                launcher = launcher_path.read_bytes()
                module = module_path.read_bytes()
                launcher_size = len(launcher)
                module_size = len(module)
                if not (MIN_LAUNCHER_BYTES <= launcher_size <= MAX_LAUNCHER_BYTES and 0 < module_size <= MAX_MODULE_BYTES):
                    raise OSError("asset size is invalid")
                launcher_hash = hashlib.sha256(launcher).hexdigest()
                module_hash = hashlib.sha256(module).hexdigest()
            except OSError:
                connection.rollback()
                self._error(409, "assets_missing", "Upload both release assets before publishing the manifest.")
                return

            published_utc = _utc_now()
            manifest = {
                "version": version,
                "publishedUtc": published_utc,
                "releaseNotes": notes,
                "artifacts": {
                    "launcher": {
                        "url": f"/v1/releases/{version}/launcher",
                        "size": launcher_size,
                        "sha256": launcher_hash,
                    },
                    "graphicsModule": {
                        "url": f"/v1/releases/{version}/graphics-module",
                        "size": module_size,
                        "sha256": module_hash,
                    },
                },
            }
            try:
                manifest_json = _json_bytes(manifest).decode("utf-8")
            except ValueError as error:
                connection.rollback()
                self._error(422, "invalid_release_notes", str(error))
                return
            connection.execute(
                "INSERT INTO releases "
                "(version, published_utc, manifest_json, launcher_sha256, launcher_size, module_sha256, module_size) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (version, published_utc, manifest_json, launcher_hash, launcher_size, module_hash, module_size),
            )
            _mark_github_mirror_dirty(connection)
            connection.commit()
        self._send_json(201, manifest, cache_control="no-cache")

    def _put_tool_asset(self, tool_name: str, version: str) -> None:
        if tool_name != "logihub":
            self._error(404, "tool_not_found", "Tool was not found.")
            return
        if not TOOL_VERSION.fullmatch(version):
            self._error(400, "invalid_release_version", "Tool version is invalid.")
            return
        if not self._authorized("CAFELAUNCHER_PUBLISH_TOKEN"):
            return
        body = self._body(MAX_TOOL_BYTES)
        if body is None:
            return
        if not body:
            self._error(400, "empty_asset", "Tool asset cannot be empty.")
            return

        published_utc = _utc_now()
        digest = hashlib.sha256(body).hexdigest()
        with DB_LOCK, _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM tool_assets WHERE tool_name = ? AND version = ?",
                (tool_name, version),
            ).fetchone()
            if exists is not None:
                connection.rollback()
                self._error(409, "tool_version_immutable", "Published tool versions cannot be modified.")
                return
            tool_dir = _data_dir() / "tools" / tool_name / version
            tool_dir.mkdir(parents=True, exist_ok=True)
            target = tool_dir / "asset.bin"
            temporary_name = None
            try:
                with tempfile.NamedTemporaryFile(dir=tool_dir, prefix=".upload-", delete=False) as temporary:
                    temporary.write(body)
                    temporary_name = temporary.name
                os.replace(temporary_name, target)
            finally:
                if temporary_name and os.path.exists(temporary_name):
                    os.unlink(temporary_name)
            connection.execute(
                "INSERT INTO tool_assets (tool_name, version, published_utc, sha256, size) VALUES (?, ?, ?, ?, ?)",
                (tool_name, version, published_utc, digest, len(body)),
            )
            _mark_github_mirror_dirty(connection)
            connection.commit()

        self._send_json(
            201,
            {"tool": tool_name, "version": version, "publishedUtc": published_utc, "size": len(body), "sha256": digest},
        )


def main() -> None:
    _initialize()
    host = os.environ.get("CAFELAUNCHER_BIND", "127.0.0.1")
    if host.lower() != "localhost":
        try:
            bind_address = ipaddress.ip_address(host)
            if bind_address.version != 4 or not bind_address.is_loopback:
                raise SystemExit("CAFELAUNCHER_BIND must be an IPv4 loopback address; expose it through Funnel or another HTTPS proxy.")
        except ValueError as error:
            raise SystemExit("CAFELAUNCHER_BIND must be localhost or an IPv4 loopback address.") from error
    sync_token = os.environ.get("CAFELAUNCHER_SYNC_TOKEN", "")
    publish_token = os.environ.get("CAFELAUNCHER_PUBLISH_TOKEN", "")
    if len(sync_token) < 32 or len(publish_token) < 32:
        raise SystemExit("Configure separate CAFELAUNCHER_SYNC_TOKEN and CAFELAUNCHER_PUBLISH_TOKEN values, each at least 32 characters.")
    if hmac.compare_digest(sync_token, publish_token):
        raise SystemExit("CAFELAUNCHER_SYNC_TOKEN and CAFELAUNCHER_PUBLISH_TOKEN must be different.")
    try:
        port = int(os.environ.get("CAFELAUNCHER_PORT", "8090"))
    except ValueError as error:
        raise SystemExit("CAFELAUNCHER_PORT must be an integer.") from error
    if not 1 <= port <= 65535:
        raise SystemExit("CAFELAUNCHER_PORT must be between 1 and 65535.")
    httpd = BoundedHTTPServer((host, port), ApiHandler)
    print(f"CafeLauncher Backend {APP_VERSION} listening on http://{host}:{port}")
    print(f"SQLite data: {_db_path()}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Stopping CafeLauncher Backend.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
