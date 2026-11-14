#!/usr/bin/env python3
"""Small, dependency-free CafeLauncher profile and release API prototype."""

from __future__ import annotations

import hashlib
import html
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from validation import PROFILE_CFG_FILES, normalize_graphics, normalize_profile, validate_cfg, viewer_schema


APP_VERSION = "0.1.0"
MIN_WEB_ACTION_RUNNER_VERSION = "0.7.6"
MAX_SYNC_BYTES = 4 * 1024 * 1024
MAX_CFG_BYTES = 2 * 1024 * 1024
MIN_LAUNCHER_BYTES = 10_000
MAX_LAUNCHER_BYTES = 1_000_000
MAX_MODULE_BYTES = 8 * 1024 * 1024
MAX_TOOL_BYTES = 32 * 1024 * 1024
PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
LAUNCHER_RELEASE_VERSION = re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}\.\d+\Z")
TOOL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
ACTION_ID = re.compile(r"[A-Za-z0-9_-]{43}\Z")
ACTION_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{43}\Z")
ACTION_EVENT_ID = re.compile(r"[0-9a-fA-F-]{36}\Z")
ACTION_PROFILES = {
    "detect-pc": None,
    "play-exec": frozenset(PROFILE_CFG_FILES),
    "apply-profile": frozenset(PROFILE_CFG_FILES),
    "apply-graphics": frozenset(PROFILE_CFG_FILES),
}
ACTION_SESSION_COOKIE = "CafeLauncherActionSession"
ACTION_SESSION_TTL_SECONDS = 3600
ACTION_REQUEST_TTL_SECONDS = 90
# The local runner stops a launcher subprocess after 120 seconds. Keep a short
# reporting margin so a lost runner result becomes INTERRUPTED promptly.
ACTION_RESULT_TTL_SECONDS = 180
MAX_ACTION_BODY_BYTES = 16 * 1024
MAX_ACTION_DETAIL_CHARS = 1000
MAX_ACTION_EVENTS = 64
ACTION_TERMINAL_STATES = frozenset({"COMPLETED", "PARTIAL", "SKIPPED", "FAILED", "CANCELED", "INTERRUPTED"})
ACTION_PROGRESS_STATES = frozenset({"RUNNING", "AWAITING_LOCAL_INPUT"})
ACTION_STEP_STATES = frozenset({"SUCCESS", "PARTIAL", "SKIPPED", "FAILED", "PENDING", "RUNNING"})
DB_LOCK = threading.RLock()

REPOSITORY_STATUS_CACHE_TTL_SECONDS = 300
REPOSITORY_STATUS_CACHE_LOCK = threading.Lock()
REPOSITORY_STATUS_CACHE: dict[str, object] = {"expires_at": 0.0, "value": None}
REPOSITORY_STATUS_TARGETS = (
    {"name": "Cs-script", "repository": "fabjoncela/Cs-script", "role": "Launcher source"},
    {"name": "CafeLauncher Backend", "repository": "fabjoncela/cafelauncher-backend", "role": "Backend API"},
    {"name": "Backend Mirror", "repository": "fabjoncela/cafelauncher-backend-mirror", "role": "Public fallback snapshot"},
)


def _github_repository_status() -> dict[str, object]:
    """Return cached public GitHub push timestamps; isolate failures per repository."""
    now = time.monotonic()
    cached = REPOSITORY_STATUS_CACHE.get("value")
    if cached is not None and now < float(REPOSITORY_STATUS_CACHE.get("expires_at", 0)):
        return cached  # type: ignore[return-value]
    with REPOSITORY_STATUS_CACHE_LOCK:
        now = time.monotonic()
        cached = REPOSITORY_STATUS_CACHE.get("value")
        if cached is not None and now < float(REPOSITORY_STATUS_CACHE.get("expires_at", 0)):
            return cached  # type: ignore[return-value]
        repositories = []
        for target in REPOSITORY_STATUS_TARGETS:
            item = {**target, "status": "unavailable", "pushedAt": None, "url": f"https://github.com/{target['repository']}", "error": None}
            request = urllib.request.Request(
                f"https://api.github.com/repos/{target['repository']}",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "CafeLauncher-Status"},
            )
            try:
                with urllib.request.urlopen(request, timeout=4) as response:
                    payload = json.loads(response.read(256 * 1024).decode("utf-8"))
                pushed_at = payload.get("pushed_at")
                if isinstance(pushed_at, str) and pushed_at:
                    item.update(status="available", pushedAt=pushed_at)
                else:
                    item["error"] = "GitHub did not return a push timestamp."
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                item["error"] = "GitHub metadata is temporarily unavailable."
            repositories.append(item)
        value = {"repositories": repositories, "checkedAt": _utc_now(), "cacheTtlSeconds": REPOSITORY_STATUS_CACHE_TTL_SECONDS}
        REPOSITORY_STATUS_CACHE.update(value=value, expires_at=time.monotonic() + REPOSITORY_STATUS_CACHE_TTL_SECONDS)
        return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _relative_age(value: str | None) -> str:
    if not value:
        return ""
    try:
        saved_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - saved_at.astimezone(timezone.utc)).total_seconds()))
    except (AttributeError, ValueError):
        return "age unavailable"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        amount, unit = seconds // 60, "minute"
    elif seconds < 86400:
        amount, unit = seconds // 3600, "hour"
    elif seconds < 86400 * 30:
        amount, unit = seconds // 86400, "day"
    else:
        amount, unit = seconds // (86400 * 30), "month"
    return f"{amount} {unit}{'' if amount == 1 else 's'} ago"


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


def _app_page_updated_utc() -> str:
    app_page = Path(__file__).resolve().parent / "web" / "app.html"
    try:
        modified_at = app_page.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(modified_at, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
            CREATE TABLE IF NOT EXISTS action_sessions (
                session_hash TEXT PRIMARY KEY,
                csrf_hash TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS action_requests (
                request_hash TEXT PRIMARY KEY,
                session_hash TEXT NOT NULL,
                action TEXT NOT NULL,
                profile_name TEXT,
                profile_revision INTEGER,
                state TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                expires_at REAL NOT NULL,
                upload_expires_at REAL,
                claim_capability_hash TEXT,
                runner_version TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                cancel_requested_utc TEXT,
                events_json TEXT NOT NULL,
                result_json TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_action_requests_expiry
                ON action_requests(expires_at);
            CREATE TABLE IF NOT EXISTS action_events (
                request_hash TEXT NOT NULL,
                event_id TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                PRIMARY KEY(request_hash, event_id)
            );
            """
        )
        action_request_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(action_requests)").fetchall()
        }
        if "cancel_requested" not in action_request_columns:
            connection.execute("ALTER TABLE action_requests ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
        if "cancel_requested_utc" not in action_request_columns:
            connection.execute("ALTER TABLE action_requests ADD COLUMN cancel_requested_utc TEXT")
        if "profile_revision" not in action_request_columns:
            connection.execute("ALTER TABLE action_requests ADD COLUMN profile_revision INTEGER")
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
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 3 and path_parts[:2] == ["v1", "actions"] and path_parts[2] != "session":
            path_parts[2] = "[request]"
            safe_path = "/" + "/".join(path_parts[:4])
        else:
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
    def _action_origin() -> str | None:
        configured = os.environ.get("CAFELAUNCHER_ACTION_ORIGIN", "").strip()
        if not configured:
            return None
        try:
            parsed = urlsplit(configured)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
            ):
                return None
            port = parsed.port
        except ValueError:
            return None
        host = parsed.hostname.lower()
        authority = host if port in (None, 443) else f"{host}:{port}"
        return f"https://{authority}"

    def _check_action_origin(self) -> bool:
        expected = self._action_origin()
        if expected is None:
            self._error(503, "actions_unconfigured", "Web actions are not configured on this server.")
            return False
        origin = self.headers.get("Origin", "").rstrip("/")
        if not origin:
            referer = urlsplit(self.headers.get("Referer", ""))
            if referer.scheme and referer.netloc:
                origin = f"{referer.scheme}://{referer.netloc}".rstrip("/")
        fetch_site = self.headers.get("Sec-Fetch-Site", "")
        if not origin and fetch_site == "same-origin" and self.headers.get("Host", "").lower() == urlsplit(expected).netloc.lower():
            origin = expected
        if origin != expected or (fetch_site and fetch_site != "same-origin"):
            self._error(403, "cross_origin_request", "Web action requests must come from the CafeLauncher site.")
            return False
        return True

    def _action_json_body(self, maximum: int) -> object | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._error(415, "unsupported_media_type", "Web action requests must use application/json.")
            return None
        body = self._body(maximum)
        if body is None:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            self._error(400, "invalid_json", "Web action body must be valid UTF-8 JSON.")
            return None
        if payload is None:
            self._error(400, "invalid_json", "Web action body must be a JSON object.")
            return None
        return payload

    @staticmethod
    def _action_hash(value: str) -> str:
        return hashlib.sha256(value.encode("ascii")).hexdigest()

    def _action_browser_session(self, *, require_csrf: bool) -> tuple[str, sqlite3.Row] | None:
        raw_cookie = self.headers.get("Cookie", "")
        try:
            cookie = SimpleCookie()
            cookie.load(raw_cookie)
            session_value = cookie[ACTION_SESSION_COOKIE].value
        except (CookieError, KeyError, TypeError, ValueError):
            self._error(401, "action_session_required", "Open the CafeLauncher page and retry this action.")
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", session_value):
            self._error(401, "action_session_invalid", "The CafeLauncher action session is invalid.")
            return None
        session_hash = self._action_hash(session_value)
        with _connect() as connection:
            row = connection.execute(
                "SELECT session_hash, csrf_hash, expires_at FROM action_sessions WHERE session_hash = ?",
                (session_hash,),
            ).fetchone()
        if row is None or float(row["expires_at"]) <= time.time():
            self._error(401, "action_session_expired", "The CafeLauncher action session expired. Reload the page.")
            return None
        if require_csrf:
            csrf_value = self.headers.get("X-Cafe-CSRF", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", csrf_value) or not hmac.compare_digest(
                self._action_hash(csrf_value), row["csrf_hash"]
            ):
                self._error(403, "csrf_rejected", "Reload the CafeLauncher page before retrying.")
                return None
        return session_hash, row

    def _send_action_state(self, request_row: sqlite3.Row, request_id: str, *, status_code: int = 200) -> None:
        try:
            events = json.loads(request_row["events_json"])
            result = json.loads(request_row["result_json"]) if request_row["result_json"] else None
        except (json.JSONDecodeError, TypeError):
            self._error(503, "action_state_unavailable", "The action result could not be read.")
            return
        state = request_row["state"]
        runner_update_required = any(
            isinstance(event, dict)
            and isinstance(event.get("step"), dict)
            and event["step"].get("name") == "Runner update"
            for event in events
        )
        if state == "REQUESTED" and float(request_row["expires_at"]) <= time.time():
            state = "EXPIRED"
        elif state in {"CLAIMED", "RUNNING", "AWAITING_LOCAL_INPUT"} and float(request_row["upload_expires_at"] or 0) <= time.time():
            state = "INTERRUPTED"
        self._send_json(
            status_code,
            {
                "requestId": request_id,
                "action": request_row["action"],
                "profile": request_row["profile_name"],
                "profileRevision": request_row["profile_revision"],
                "runnerVersion": request_row["runner_version"],
                "minimumRunnerVersion": MIN_WEB_ACTION_RUNNER_VERSION,
                "runnerUpdateRequired": runner_update_required,
                "state": state,
                "cancelRequested": bool(request_row["cancel_requested"]),
                "cancelRequestedUtc": request_row["cancel_requested_utc"],
                "createdUtc": request_row["created_utc"],
                "expiresUtc": datetime.fromtimestamp(float(request_row["expires_at"]), timezone.utc).isoformat().replace("+00:00", "Z"),
                "events": events,
                "result": result,
            },
            cache_control="no-store",
        )

    def _get_action_session(self) -> None:
        if not self._check_action_origin():
            return
        existing_hash = None
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            existing_value = cookie[ACTION_SESSION_COOKIE].value
            if re.fullmatch(r"[A-Za-z0-9_-]{43}", existing_value):
                existing_hash = self._action_hash(existing_value)
        except (CookieError, KeyError, TypeError, ValueError):
            pass
        csrf_value = secrets.token_urlsafe(32)
        now = time.time()
        session_value = None
        with DB_LOCK, _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM action_sessions WHERE expires_at <= ?", (now,))
            existing = connection.execute(
                "SELECT session_hash FROM action_sessions WHERE session_hash = ? AND expires_at > ?",
                (existing_hash, now),
            ).fetchone() if existing_hash else None
            if existing:
                session_hash = existing["session_hash"]
                connection.execute(
                    "UPDATE action_sessions SET csrf_hash = ? WHERE session_hash = ?",
                    (self._action_hash(csrf_value), session_hash),
                )
                session_value = cookie[ACTION_SESSION_COOKIE].value
            else:
                session_value = secrets.token_urlsafe(32)
                session_hash = self._action_hash(session_value)
                connection.execute(
                    "INSERT INTO action_sessions (session_hash, csrf_hash, created_utc, expires_at) VALUES (?, ?, ?, ?)",
                    (session_hash, self._action_hash(csrf_value), _utc_now(), now + ACTION_SESSION_TTL_SECONDS),
                )
            connection.commit()
        self._send_json(
            200,
            {"csrfToken": csrf_value, "expiresInSeconds": ACTION_SESSION_TTL_SECONDS},
            headers={
                "Set-Cookie": (
                    f"{ACTION_SESSION_COOKIE}={session_value}; Path=/v1/actions; Max-Age={ACTION_SESSION_TTL_SECONDS}; "
                    "Secure; HttpOnly; SameSite=Strict"
                ),
                "Vary": "Origin",
            },
        )

    def _create_action_request(self) -> None:
        if not self._check_action_origin():
            return
        browser = self._action_browser_session(require_csrf=True)
        if browser is None:
            return
        session_hash, _session_row = browser
        payload = self._action_json_body(2048)
        if payload is None:
            return
        if not isinstance(payload, dict) or set(payload) not in (
            {"action", "profile", "clientRequestId"},
            {"action", "profile", "clientRequestId", "profileRevision"},
        ):
            self._error(400, "invalid_action_request", "Action request contains missing or unsupported fields.")
            return
        action = payload["action"]
        profile_name = payload["profile"]
        client_id = payload["clientRequestId"]
        has_revision_hint = "profileRevision" in payload
        revision_hint = payload.get("profileRevision")
        if not isinstance(action, str) or action not in ACTION_PROFILES:
            self._error(422, "unsupported_action", "That web action is not available.")
            return
        if not isinstance(client_id, str) or not ACTION_CLIENT_ID.fullmatch(client_id):
            self._error(422, "invalid_request_id", "clientRequestId must be a 256-bit base64url identifier.")
            return
        expected_profiles = ACTION_PROFILES[action]
        if expected_profiles is None:
            if profile_name is not None:
                self._error(422, "invalid_action_profile", "Detect PC does not use a profile.")
                return
        elif not isinstance(profile_name, str) or profile_name not in expected_profiles:
            self._error(422, "invalid_action_profile", "Choose a supported CafeLauncher profile.")
            return
        if expected_profiles is None:
            if has_revision_hint and revision_hint is not None:
                self._error(422, "invalid_profile_revision", "Detect PC does not use a profile revision.")
                return
        elif has_revision_hint and (isinstance(revision_hint, bool) or not isinstance(revision_hint, int) or revision_hint < 1):
            self._error(422, "invalid_profile_revision", "Choose a profile with a valid published revision before running this action.")
            return

        request_hash = self._action_hash(client_id)
        now = time.time()
        created_utc = _utc_now()
        with DB_LOCK, _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale_cutoff = now - 86400
            connection.execute(
                "DELETE FROM action_events WHERE request_hash IN "
                "(SELECT request_hash FROM action_requests WHERE expires_at < ? AND COALESCE(upload_expires_at, 0) < ?)",
                (stale_cutoff, now),
            )
            connection.execute(
                "DELETE FROM action_requests WHERE expires_at < ? AND COALESCE(upload_expires_at, 0) < ?",
                (stale_cutoff, now),
            )
            row = connection.execute(
                "SELECT * FROM action_requests WHERE request_hash = ?", (request_hash,)
            ).fetchone()
            if row is not None:
                if (row["session_hash"] != session_hash or row["action"] != action or
                    row["profile_name"] != profile_name or
                    (has_revision_hint and row["profile_revision"] != revision_hint)):
                    connection.rollback()
                    self._error(409, "request_id_conflict", "This action request identifier has already been used.")
                    return
                connection.commit()
                self._send_action_state(row, client_id)
                return
            profile_revision = None
            if expected_profiles is not None:
                current_revision = connection.execute(
                    "SELECT revision FROM profile_revisions WHERE profile_name = ? ORDER BY revision DESC LIMIT 1",
                    (profile_name,),
                ).fetchone()
                if current_revision is None:
                    connection.rollback()
                    self._error(409, "profile_unavailable", "This profile has not been published to the backend yet.")
                    return
                profile_revision = int(current_revision["revision"])
                if has_revision_hint and revision_hint != profile_revision:
                    connection.rollback()
                    self._error(409, "profile_revision_changed", "This profile changed since the page loaded. Refreshing it before the action starts.")
                    return
            outstanding = connection.execute(
                "SELECT COUNT(*) FROM action_requests WHERE session_hash = ? "
                "AND state IN ('REQUESTED', 'CLAIMED', 'RUNNING', 'AWAITING_LOCAL_INPUT') "
                "AND (expires_at > ? OR upload_expires_at > ?)",
                (session_hash, now, now),
            ).fetchone()[0]
            if outstanding >= 3:
                connection.rollback()
                self._error(429, "too_many_action_requests", "Finish or wait for an existing CafeLauncher action first.")
                return
            connection.execute(
                "INSERT INTO action_requests "
                "(request_hash, session_hash, action, profile_name, profile_revision, state, created_utc, expires_at, events_json) "
                "VALUES (?, ?, ?, ?, ?, 'REQUESTED', ?, ?, '[]')",
                (
                    request_hash,
                    session_hash,
                    action,
                    profile_name,
                    profile_revision,
                    created_utc,
                    now + ACTION_REQUEST_TTL_SECONDS,
                ),
            )
            row = connection.execute("SELECT * FROM action_requests WHERE request_hash = ?", (request_hash,)).fetchone()
            connection.commit()
        self._send_action_state(row, client_id, status_code=201)

    def _claim_action_request(self, request_id: str) -> None:
        if not ACTION_ID.fullmatch(request_id):
            self._error(404, "action_not_found", "Action request was not found.")
            return
        payload = self._action_json_body(2048)
        if payload is None:
            return
        if not isinstance(payload, dict) or set(payload) != {"runnerVersion", "protocolVersion", "claimCapability"}:
            self._error(400, "invalid_action_claim", "Action claim contains missing or unsupported fields.")
            return
        runner_version = payload["runnerVersion"]
        protocol_version = payload["protocolVersion"]
        claim_capability = payload["claimCapability"]
        if (
            not isinstance(runner_version, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", runner_version)
            or isinstance(protocol_version, bool)
            or protocol_version != 1
            or not isinstance(claim_capability, str)
            or not ACTION_ID.fullmatch(claim_capability)
        ):
            self._error(422, "invalid_action_claim", "Runner claim fields are invalid.")
            return

        request_hash = self._action_hash(request_id)
        capability_hash = self._action_hash(claim_capability)
        now = time.time()
        with DB_LOCK, _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM action_requests WHERE request_hash = ?", (request_hash,)
            ).fetchone()
            if row is None:
                connection.rollback()
                self._error(404, "action_not_found", "Action request was not found.")
                return
            if row["state"] == "REQUESTED":
                if float(row["expires_at"]) <= now:
                    connection.execute(
                        "UPDATE action_requests SET state = 'EXPIRED' WHERE request_hash = ?", (request_hash,)
                    )
                    connection.commit()
                    self._error(410, "action_expired", "This action request expired. Return to the page and retry.")
                    return
                if not self._runner_version_is_current(runner_version):
                    detail = (
                        f"Installed runner v{runner_version} is too old; v{MIN_WEB_ACTION_RUNNER_VERSION} or newer is required. "
                        "Use Download CafeLauncher and open the downloaded file once to update this PC, then retry."
                    )
                    step = {"name": "Runner update", "status": "FAILED", "detail": detail}
                    result = self._runner_update_required_result(row["action"], row["profile_name"], runner_version)
                    events = [{"eventId": str(uuid.uuid4()), "state": "FAILED", "createdUtc": _utc_now(), "step": step}]
                    connection.execute(
                        "UPDATE action_requests SET state = 'FAILED', runner_version = ?, events_json = ?, result_json = ?, "
                        "upload_expires_at = ? WHERE request_hash = ? AND state = 'REQUESTED'",
                        (runner_version, _json_bytes(events).decode("utf-8"), _json_bytes(result).decode("utf-8"), now + ACTION_RESULT_TTL_SECONDS, request_hash),
                    )
                    row = connection.execute("SELECT * FROM action_requests WHERE request_hash = ?", (request_hash,)).fetchone()
                    connection.commit()
                    self._send_action_state(row, request_id)
                    return
                upload_expires = now + ACTION_RESULT_TTL_SECONDS
                connection.execute(
                    "UPDATE action_requests SET state = 'CLAIMED', claim_capability_hash = ?, runner_version = ?, "
                    "upload_expires_at = ? WHERE request_hash = ? AND state = 'REQUESTED'",
                    (capability_hash, runner_version, upload_expires, request_hash),
                )
                row = connection.execute("SELECT * FROM action_requests WHERE request_hash = ?", (request_hash,)).fetchone()
                connection.commit()
            elif row["state"] in {"CLAIMED", "RUNNING", "AWAITING_LOCAL_INPUT"}:
                if float(row["upload_expires_at"] or 0) <= now or not hmac.compare_digest(
                    row["claim_capability_hash"] or "", capability_hash
                ):
                    connection.rollback()
                    self._error(409, "action_already_claimed", "This action request has already been claimed.")
                    return
                connection.commit()
            else:
                connection.rollback()
                self._error(409, "action_not_claimable", "This action request is no longer available to run.")
                return
        self._send_json(
            200,
            {
                "requestId": request_id,
                "action": row["action"],
                "profile": row["profile_name"],
                "profileRevision": row["profile_revision"],
                "state": row["state"],
                "requestExpiresUtc": datetime.fromtimestamp(float(row["expires_at"]), timezone.utc).isoformat().replace("+00:00", "Z"),
                "uploadExpiresUtc": datetime.fromtimestamp(float(row["upload_expires_at"]), timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )

    @staticmethod
    def _runner_version_is_current(version: str) -> bool:
        if re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
            return False
        return tuple(int(part) for part in version.split(".")) >= tuple(
            int(part) for part in MIN_WEB_ACTION_RUNNER_VERSION.split(".")
        )

    @staticmethod
    def _runner_update_required_result(action: str, profile: str | None, version: str) -> dict[str, object]:
        summary = (
            f"This PC's CafeLauncher runner (v{version}) needs an update before it can run this action. "
            f"Download and open the latest CafeLauncher runner (v{MIN_WEB_ACTION_RUNNER_VERSION}), then retry."
        )
        step = {
            "name": "Runner update",
            "status": "FAILED",
            "detail": f"v{version} is older than the required v{MIN_WEB_ACTION_RUNNER_VERSION}.",
        }
        if action == "play-exec":
            cfg_name = PROFILE_CFG_FILES[profile]
            data: dict[str, object] = {
                "steamLaunchRequested": False,
                "cs2AlreadyRunning": False,
                "executionOutcomeKnown": True,
                "cfgName": cfg_name,
                "consoleCommands": [],
            }
        elif action in {"apply-profile", "apply-graphics"}:
            data = {"executionOutcomeKnown": True}
        else:
            data = {
                "steam": {"status": "NOT_DETECTED", "executable": None, "libraries": [], "activeSessionPresent": False},
                "cs2": {"status": "NOT_UNIQUELY_DETECTED", "installPath": None, "configPath": None},
            }
        return {"summary": summary, "steps": [step], "data": data}

    def _cancel_action_request(self, request_id: str) -> None:
        if not self._check_action_origin():
            return
        browser = self._action_browser_session(require_csrf=True)
        if browser is None:
            return
        session_hash, _session_row = browser
        payload = self._action_json_body(16)
        if payload is None:
            return
        if payload != {}:
            self._error(400, "invalid_action_cancel", "Action cancel body must be an empty JSON object.")
            return
        if not ACTION_ID.fullmatch(request_id):
            self._error(404, "action_not_found", "Action request was not found.")
            return
        request_hash = self._action_hash(request_id)
        with DB_LOCK, _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM action_requests WHERE request_hash = ? AND session_hash = ?",
                (request_hash, session_hash),
            ).fetchone()
            if row is None:
                connection.rollback()
                self._error(404, "action_not_found", "Action request was not found.")
                return
            accepted = row["state"] not in ACTION_TERMINAL_STATES
            if accepted:
                now_utc = _utc_now()
                if row["state"] == "REQUESTED":
                    connection.execute(
                        "UPDATE action_requests SET state = 'CANCELED', cancel_requested = 1, cancel_requested_utc = ? "
                        "WHERE request_hash = ? AND state = 'REQUESTED'",
                        (now_utc, request_hash),
                    )
                else:
                    connection.execute(
                        "UPDATE action_requests SET cancel_requested = 1, cancel_requested_utc = ? "
                        "WHERE request_hash = ? AND state NOT IN ('COMPLETED','PARTIAL','SKIPPED','FAILED','CANCELED','EXPIRED','INTERRUPTED')",
                        (now_utc, request_hash),
                    )
            row = connection.execute("SELECT * FROM action_requests WHERE request_hash = ?", (request_hash,)).fetchone()
            connection.commit()
        self._send_json(
            200,
            {"requestId": request_id, "state": row["state"], "cancelRequested": bool(row["cancel_requested"]), "accepted": accepted},
            cache_control="no-store",
        )

    def _get_action_control(self, request_id: str) -> None:
        if not ACTION_ID.fullmatch(request_id):
            self._error(404, "action_not_found", "Action request was not found.")
            return
        authorization = self.headers.get("Authorization", "")
        scheme, _, capability = authorization.partition(" ")
        if scheme.lower() != "bearer" or not ACTION_ID.fullmatch(capability):
            self._error(401, "runner_authorization_required", "The runner result capability is required.")
            return
        request_hash = self._action_hash(request_id)
        capability_hash = self._action_hash(capability)
        with _connect() as connection:
            row = connection.execute(
                "SELECT claim_capability_hash, state, cancel_requested, upload_expires_at FROM action_requests WHERE request_hash = ?",
                (request_hash,),
            ).fetchone()
        if row is None:
            self._error(404, "action_not_found", "Action request was not found.")
            return
        if not hmac.compare_digest(row["claim_capability_hash"] or "", capability_hash):
            self._error(401, "runner_authorization_required", "The runner result capability is invalid.")
            return
        if float(row["upload_expires_at"] or 0) <= time.time():
            self._error(410, "action_upload_expired", "The result-upload window expired.")
            return
        self._send_json(
            200,
            {"cancelRequested": bool(row["cancel_requested"]), "state": row["state"]},
            cache_control="no-store",
        )

    @staticmethod
    def _validate_action_step(step: object) -> dict[str, str] | None:
        if not isinstance(step, dict) or set(step) != {"name", "status", "detail"}:
            return None
        name, status, detail = step["name"], step["status"], step["detail"]
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 120
            or not isinstance(status, str)
            or status not in ACTION_STEP_STATES
            or not isinstance(detail, str)
            or len(detail) > MAX_ACTION_DETAIL_CHARS
        ):
            return None
        return {"name": name.strip(), "status": status, "detail": detail}

    @staticmethod
    def _validate_action_result(action: str, result: object) -> dict[str, object] | None:
        if not isinstance(result, dict) or set(result) != {"summary", "steps", "data"}:
            return None
        summary, steps, data = result["summary"], result["steps"], result["data"]
        if not isinstance(summary, str) or len(summary) > MAX_ACTION_DETAIL_CHARS:
            return None
        if not isinstance(steps, list) or len(steps) > 32:
            return None
        normalized_steps = []
        for step in steps:
            normalized = ApiHandler._validate_action_step(step)
            if normalized is None:
                return None
            normalized_steps.append(normalized)
        if action == "detect-pc":
            if not isinstance(data, dict) or set(data) != {"steam", "cs2"}:
                return None
            steam, cs2 = data["steam"], data["cs2"]
            if not isinstance(steam, dict) or set(steam) != {"status", "executable", "libraries", "activeSessionPresent"}:
                return None
            if not isinstance(cs2, dict) or set(cs2) != {"status", "installPath", "configPath"}:
                return None
            if (
                not isinstance(steam["status"], str)
                or steam["status"] not in {"DETECTED", "NOT_DETECTED"}
                or not isinstance(steam["activeSessionPresent"], bool)
            ):
                return None
            if not isinstance(cs2["status"], str) or cs2["status"] not in {"DETECTED", "NOT_UNIQUELY_DETECTED"}:
                return None
            paths = [steam["executable"], cs2["installPath"], cs2["configPath"]]
            if any(value is not None and (not isinstance(value, str) or len(value) > 1024) for value in paths):
                return None
            libraries = steam["libraries"]
            if not isinstance(libraries, list) or len(libraries) > 32 or any(
                not isinstance(value, str) or len(value) > 1024 for value in libraries
            ):
                return None
            normalized_data: object = {
                "steam": {
                    "status": steam["status"],
                    "executable": steam["executable"],
                    "libraries": libraries,
                    "activeSessionPresent": steam["activeSessionPresent"],
                },
                "cs2": {
                    "status": cs2["status"],
                    "installPath": cs2["installPath"],
                    "configPath": cs2["configPath"],
                },
            }
        elif action == "play-exec":
            if not isinstance(data, dict) or set(data) != {
                "steamLaunchRequested", "cs2AlreadyRunning", "cfgName", "consoleCommands", "executionOutcomeKnown"
            }:
                return None
            if (
                not isinstance(data["steamLaunchRequested"], bool)
                or not isinstance(data["cs2AlreadyRunning"], bool)
                or not isinstance(data["executionOutcomeKnown"], bool)
            ):
                return None
            cfg_name = data["cfgName"]
            if not isinstance(cfg_name, str) or cfg_name not in set(PROFILE_CFG_FILES.values()):
                return None
            commands = data["consoleCommands"]
            if not isinstance(commands, list) or len(commands) > 2 or any(
                not isinstance(value, str) or len(value) > 200 for value in commands
            ):
                return None
            if commands and commands != [
                "exec cafelauncher_profile_baseline.cfg",
                f"exec {cfg_name}",
            ]:
                return None
            normalized_data = {
                "steamLaunchRequested": data["steamLaunchRequested"],
                "cs2AlreadyRunning": data["cs2AlreadyRunning"],
                "executionOutcomeKnown": data["executionOutcomeKnown"],
                "cfgName": cfg_name,
                "consoleCommands": commands,
            }
        elif action in {"apply-profile", "apply-graphics"}:
            if not isinstance(data, dict) or set(data) != {"executionOutcomeKnown"}:
                return None
            if not isinstance(data["executionOutcomeKnown"], bool):
                return None
            normalized_data = {"executionOutcomeKnown": data["executionOutcomeKnown"]}
        else:
            return None
        return {"summary": summary, "steps": normalized_steps, "data": normalized_data}

    def _post_action_event(self, request_id: str) -> None:
        if not ACTION_ID.fullmatch(request_id):
            self._error(404, "action_not_found", "Action request was not found.")
            return
        auth_scheme, _, capability = self.headers.get("Authorization", "").partition(" ")
        if auth_scheme.lower() != "bearer" or not ACTION_ID.fullmatch(capability):
            self._error(401, "runner_authorization_required", "A valid runner result capability is required.")
            return
        payload = self._action_json_body(MAX_ACTION_BODY_BYTES)
        if payload is None:
            return
        if not isinstance(payload, dict) or not {"eventId", "state"}.issubset(payload):
            self._error(400, "invalid_action_event", "Action event is missing required fields.")
            return
        event_id, state = payload["eventId"], payload["state"]
        if not isinstance(event_id, str) or not ACTION_EVENT_ID.fullmatch(event_id):
            self._error(422, "invalid_event_id", "eventId must be a UUID.")
            return
        if not isinstance(state, str) or state not in ACTION_PROGRESS_STATES | ACTION_TERMINAL_STATES:
            self._error(422, "invalid_action_state", "Action event state is unsupported.")
            return
        allowed_fields = {"eventId", "state", "step", "result"}
        if set(payload) - allowed_fields:
            self._error(400, "invalid_action_event", "Action event contains unsupported fields.")
            return
        step = None
        if "step" in payload:
            step = self._validate_action_step(payload["step"])
            if step is None:
                self._error(422, "invalid_action_step", "Action step is invalid or too large.")
                return
        result = None
        if state in ACTION_TERMINAL_STATES:
            if "result" not in payload:
                self._error(400, "action_result_required", "A terminal event must include a result.")
                return
        elif "result" in payload:
            self._error(400, "unexpected_action_result", "Only terminal events may include a result.")
            return

        request_hash = self._action_hash(request_id)
        capability_hash = self._action_hash(capability)
        now = time.time()
        with DB_LOCK, _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM action_requests WHERE request_hash = ?", (request_hash,)
            ).fetchone()
            if row is None:
                connection.rollback()
                self._error(404, "action_not_found", "Action request was not found.")
                return
            if not hmac.compare_digest(row["claim_capability_hash"] or "", capability_hash):
                connection.rollback()
                self._error(401, "runner_authorization_required", "The runner result capability is invalid.")
                return
            if float(row["upload_expires_at"] or 0) <= now:
                connection.rollback()
                self._error(410, "action_upload_expired", "The result-upload window expired.")
                return
            duplicate = connection.execute(
                "SELECT 1 FROM action_events WHERE request_hash = ? AND event_id = ?", (request_hash, event_id)
            ).fetchone()
            if duplicate is not None:
                connection.commit()
                self._send_json(200, {"accepted": True, "duplicate": True, "state": row["state"]})
                return
            if row["state"] in ACTION_TERMINAL_STATES:
                connection.rollback()
                self._error(409, "action_already_finished", "A terminal action result has already been recorded.")
                return
            if row["state"] == "CLAIMED" and state not in {"RUNNING", "FAILED", "CANCELED", "INTERRUPTED"}:
                connection.rollback()
                self._error(409, "invalid_action_transition", "The runner must report RUNNING before this event.")
                return
            if row["state"] in {"REQUESTED", "EXPIRED"}:
                connection.rollback()
                self._error(409, "invalid_action_transition", "An unclaimed request cannot report results.")
                return
            try:
                events = json.loads(row["events_json"])
            except (json.JSONDecodeError, TypeError):
                connection.rollback()
                self._error(503, "action_state_unavailable", "The action event history could not be read.")
                return
            if not isinstance(events, list) or len(events) >= MAX_ACTION_EVENTS:
                connection.rollback()
                self._error(429, "too_many_action_events", "The action has reached its event limit.")
                return
            event = {"eventId": event_id, "state": state, "createdUtc": _utc_now()}
            if step is not None:
                event["step"] = step
            if state in ACTION_TERMINAL_STATES:
                result = self._validate_action_result(row["action"], payload["result"])
                if result is None:
                    connection.rollback()
                    self._error(422, "invalid_action_result", "Action result does not match the bounded schema.")
                    return
                if row["action"] in {"play-exec", "apply-profile", "apply-graphics"}:
                    outcome_known = result["data"]["executionOutcomeKnown"]
                    if outcome_known == (state == "INTERRUPTED"):
                        connection.rollback()
                        self._error(422, "invalid_action_result", "Play + Exec outcome certainty does not match its terminal state.")
                        return
                event["result"] = result
            events.append(event)
            connection.execute(
                "INSERT INTO action_events (request_hash, event_id, created_utc) VALUES (?, ?, ?)",
                (request_hash, event_id, event["createdUtc"]),
            )
            connection.execute(
                "UPDATE action_requests SET state = ?, events_json = ?, result_json = ? "
                "WHERE request_hash = ?",
                (
                    state,
                    _json_bytes(events).decode("utf-8"),
                    _json_bytes(result).decode("utf-8") if result is not None else row["result_json"],
                    request_hash,
                ),
            )
            connection.commit()
        self._send_json(200, {"accepted": True, "duplicate": False, "state": state})

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

    def _send_viewer_asset(self, asset_name: str) -> None:
        assets = {
            "app": ("app.html", "text/html; charset=utf-8"),
            "app.css": ("app.css", "text/css; charset=utf-8"),
            "app.js": ("app.js", "text/javascript; charset=utf-8"),
            "favicon.png": ("favicon.png", "image/png"),
            "viewer": ("viewer.html", "text/html; charset=utf-8"),
            "viewer.css": ("viewer.css", "text/css; charset=utf-8"),
            "viewer.js": ("viewer.js", "text/javascript; charset=utf-8"),
            "status.css": ("status.css", "text/css; charset=utf-8"),
            "status.js": ("status.js", "text/javascript; charset=utf-8"),
        }
        asset = assets.get(asset_name)
        if asset is None:
            self._error(404, "not_found", "Viewer asset was not found.")
            return
        filename, content_type = asset
        try:
            body = (Path(__file__).resolve().parent / "web" / filename).read_bytes()
        except OSError:
            self._error(503, "viewer_unavailable", "The profile viewer is unavailable.")
            return
        headers = {"Referrer-Policy": "no-referrer"}
        if filename in {"app.html", "viewer.html"}:
            headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; connect-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            )
            headers["X-Frame-Options"] = "DENY"
        if filename == "app.html":
            body = body.replace(b"@@APP_VERSION@@", html.escape(APP_VERSION).encode("ascii"))
            body = body.replace(b"@@APP_UPDATED_UTC@@", _app_page_updated_utc().encode("ascii"))
        self._send_bytes(200, body, content_type, cache_control="no-cache", headers=headers)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]

        if parts == ["v1", "actions", "session"]:
            self._get_action_session()
            return
        if len(parts) == 3 and parts[:2] == ["v1", "actions"]:
            if not self._check_action_origin():
                return
            browser = self._action_browser_session(require_csrf=False)
            if browser is None:
                return
            session_hash, _session_row = browser
            request_id = parts[2]
            if not ACTION_ID.fullmatch(request_id):
                self._error(404, "action_not_found", "Action request was not found.")
                return
            with _connect() as connection:
                row = connection.execute(
                    "SELECT * FROM action_requests WHERE request_hash = ? AND session_hash = ?",
                    (self._action_hash(request_id), session_hash),
                ).fetchone()
            if row is None:
                self._error(404, "action_not_found", "Action request was not found.")
                return
            self._send_action_state(row, request_id)
            return

        if len(parts) == 4 and parts[:2] == ["v1", "actions"] and parts[3] == "control":
            self._get_action_control(parts[2])
            return

        if not parts:
            self._send_viewer_asset("app")
            return

        if len(parts) == 1 and parts[0] in ("app.css", "app.js", "favicon.png", "viewer", "viewer.css", "viewer.js", "status.css", "status.js"):
            self._send_viewer_asset(parts[0])
            return

        if parts in (["status"], ["stats"]):
            try:
                with _connect() as connection:
                    profile_count = connection.execute(
                        "SELECT COUNT(DISTINCT profile_name) FROM profile_revisions"
                    ).fetchone()[0]
                    latest_profile_save = connection.execute(
                        "SELECT profile_name, created_utc FROM profile_revisions "
                        "ORDER BY created_utc DESC, revision DESC LIMIT 1"
                    ).fetchone()
                    mirror = connection.execute(
                        "SELECT generation, mirrored_generation, last_success_utc "
                        "FROM github_mirror_state WHERE singleton = 1"
                    ).fetchone()
                    releases = connection.execute("SELECT version FROM releases").fetchall()
            except sqlite3.Error:
                self._error(503, "storage_unavailable", "Backend storage is unavailable.")
                return
            mirror_ready = mirror is not None and mirror["last_success_utc"] is not None
            mirror_current = mirror_ready and mirror["generation"] == mirror["mirrored_generation"]
            mirror_status = (
                "Up to date" if mirror_current else "Waiting to sync" if mirror_ready else "Not synced yet"
            )
            mirror_generation = mirror["generation"] if mirror is not None else 0
            mirrored_generation = mirror["mirrored_generation"] if mirror is not None else 0
            last_success_utc = mirror["last_success_utc"] if mirror_ready else None
            latest_release = max(
                (row["version"] for row in releases), key=_launcher_version_key, default=None
            )
            if latest_profile_save is not None:
                profile_save_utc = latest_profile_save["created_utc"]
                try:
                    profile_save_datetime = datetime.fromisoformat(profile_save_utc.replace("Z", "+00:00"))
                    profile_save_label = profile_save_datetime.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                except (AttributeError, ValueError):
                    profile_save_label = profile_save_utc
                latest_profile_save_markup = (
                    f'<time datetime="{html.escape(profile_save_utc, quote=True)}">'
                    f'{html.escape(profile_save_label)}</time>'
                )
                latest_profile_save_profile = html.escape(latest_profile_save["profile_name"])
            else:
                latest_profile_save_markup = "No profile saves yet"
                latest_profile_save_profile = ""
            revision_dir = Path(__file__).resolve().parent.name
            deployed_revision = revision_dir if re.fullmatch(r"[0-9a-f]{40}", revision_dir) else None
            if deployed_revision:
                deployment_value = (
                    f'<a href="https://github.com/fabjoncela/cafelauncher-backend/commit/{deployed_revision}">'
                    f'{deployed_revision[:12]}</a>'
                )
                deployment_label = "Deployed commit"
            else:
                deployment_value = "Unavailable in this source checkout"
                deployment_label = "Deployment revision"
            mirror_time = (
                f'<time datetime="{html.escape(last_success_utc, quote=True)}">'
                f'{html.escape(last_success_utc.removesuffix("Z"))} UTC</time> '
                f'<span class="relative-age">({html.escape(_relative_age(last_success_utc))})</span>'
                if last_success_utc else "No successful sync yet"
            )
            release_value = html.escape(latest_release) if latest_release else "No backend-hosted package published yet"
            body = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>CafeLauncher Backend Status</title><link rel="stylesheet" href="/status.css"><script src="/status.js" defer></script></head>
<body>
<header class="site-header">
  <div><p class="eyebrow">CAFE LAUNCHER · PUBLIC SERVICE</p><h1>Backend status</h1>
    <p class="subhead">Status and public data freshness for the CafeLauncher backend.</p></div>
  <span class="health-badge"><span class="status-dot"></span>Service healthy</span>
</header>
<main>
  <p class="purpose">This service supplies CafeLauncher profiles, graphics settings, CFG files, and release assets. Use the profile viewer to inspect current settings. Profile edits and release publishing require authorization and are not available on this page.</p>
  <section class="status-grid" aria-label="Backend status details">
    <article class="status-card"><p class="card-label">API</p><p class="card-value">Running</p><p class="card-detail">Service version {html.escape(APP_VERSION)}</p></article>
    <article class="status-card"><p class="card-label">Profiles</p><p class="card-value">{profile_count}</p><p class="card-detail">Profiles with saved backend data</p></article>
    <article class="status-card"><p class="card-label">GitHub data mirror</p><p class="card-value">{html.escape(mirror_status)}</p><p class="card-detail">Generation {mirror_generation} · mirrored {mirrored_generation}</p><p class="card-detail">Last successful sync: {mirror_time}</p></article>
    <article class="status-card"><p class="card-label">Latest profile save</p><p class="card-value release-value">{latest_profile_save_markup}</p><p class="card-detail">{latest_profile_save_profile or "No saved profile bundles"}</p></article>
    <article class="status-card"><p class="card-label">Backend-hosted release</p><p class="card-value release-value">{release_value}</p><p class="card-detail">The launcher currently auto-updates from Cs-script on GitHub.</p></article>
  </section>
  <section class="repository-activity" aria-labelledby="repository-activity-heading">
    <div class="repository-activity-heading">
      <div><p class="eyebrow">GITHUB · MAIN BRANCHES</p><h2 id="repository-activity-heading">Last repository pushes</h2></div>
      <span id="repository-activity-checked" class="relative-age" role="status" aria-live="polite">Checking GitHub…</span>
    </div>
    <div id="repository-activity-grid" class="status-grid" aria-live="polite">
      <article class="status-card"><p class="card-label">Cs-script</p><p class="card-value">Checking…</p><p class="card-detail">Launcher source</p></article>
      <article class="status-card"><p class="card-label">CafeLauncher Backend</p><p class="card-value">Checking…</p><p class="card-detail">Backend API</p></article>
      <article class="status-card"><p class="card-label">Backend Mirror</p><p class="card-value">Checking…</p><p class="card-detail">Public fallback snapshot</p></article>
    </div>
  </section>
  <nav class="quick-links" aria-label="Backend links">
    <a class="primary-link" href="/">CafeLauncher home</a>
    <a href="/viewer">Open profile viewer</a>
    <a href="https://github.com/fabjoncela/cafelauncher-backend#api-contract">API documentation</a>
    <a href="/v1/health">Health JSON</a>
    <a href="https://github.com/fabjoncela/cafelauncher-backend-mirror/blob/main/mirror-manifest.json">Mirror manifest</a>
    <button id="copy-diagnostics" type="button">Copy diagnostics</button>
    <span id="copy-diagnostics-status" class="copy-status" role="status" aria-live="polite"></span>
  </nav>
  <footer class="deployment-info"><span>{deployment_label}: {deployment_value}</span></footer>
</main>
</body>
</html>
""".encode("utf-8")
            self._send_bytes(
                200,
                body,
                "text/html; charset=utf-8",
                headers={
                    "Content-Security-Policy": "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                    "Referrer-Policy": "no-referrer",
                },
            )
            return

        if parts == ["v1", "repository-status"]:
            self._send_json(200, _github_repository_status(), cache_control="no-store")
            return

        if parts == ["v1", "health"]:
            try:
                with _connect() as connection:
                    connection.execute("SELECT 1").fetchone()
            except sqlite3.Error:
                self._error(503, "storage_unavailable", "Backend storage is unavailable.")
                return
            self._send_json(200, {"status": "ok", "version": APP_VERSION}, cache_control="no-store")
            return

        if parts == ["v1", "viewer-schema"]:
            self._send_json(200, viewer_schema(), cache_control="no-cache")
            return

        if len(parts) == 4 and parts[:2] == ["v1", "profiles"] and parts[3] == "history":
            profile_name = parts[2]
            if not PROFILE_NAME.fullmatch(profile_name):
                self._error(400, "invalid_profile_name", "Profile name is invalid.")
                return
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["20"])[0])
                before_revision = int(query["beforeRevision"][0]) if "beforeRevision" in query else None
            except (ValueError, IndexError):
                self._error(400, "invalid_history_cursor", "History limit and revision cursor must be integers.")
                return
            if limit < 1 or limit > 50 or (
                before_revision is not None and not 1 <= before_revision <= (1 << 63) - 1
            ):
                self._error(400, "invalid_history_cursor", "History limit must be 1–50 and revision cursor must be positive.")
                return
            with _connect() as connection:
                if before_revision is None:
                    rows = connection.execute(
                        "SELECT revision, created_utc FROM profile_revisions "
                        "WHERE profile_name = ? ORDER BY revision DESC LIMIT ?",
                        (profile_name, limit + 1),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT revision, created_utc FROM profile_revisions "
                        "WHERE profile_name = ? AND revision < ? ORDER BY revision DESC LIMIT ?",
                        (profile_name, before_revision, limit + 1),
                    ).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            self._send_json(
                200,
                {
                    "profile": profile_name,
                    "revisions": [{"revision": row["revision"], "createdUtc": row["created_utc"]} for row in page],
                    "hasMore": has_more,
                    "nextBeforeRevision": page[-1]["revision"] if has_more and page else None,
                },
                cache_control="no-cache",
            )
            return

        if len(parts) == 4 and parts[:2] == ["v1", "profiles"] and parts[3] == "bundle":
            profile_name = parts[2]
            if not PROFILE_NAME.fullmatch(profile_name):
                self._error(400, "invalid_profile_name", "Profile name is invalid.")
                return
            row = self._revision_row(profile_name)
            if row is None:
                self._error(404, "not_found", "Profile has not been published.")
                return
            etag = f'"{profile_name}-{row["revision"]}"'
            headers = {"ETag": etag, "X-Profile-Revision": str(row["revision"])}
            if self.headers.get("If-None-Match") == etag:
                self._status = 304
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("X-Profile-Revision", str(row["revision"]))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            self._send_json(
                200,
                {
                    "profile": json.loads(row["profile_json"]),
                    "graphics": json.loads(row["graphics_json"]),
                    "cfg": row["cfg_text"],
                    "revision": row["revision"],
                    "createdUtc": row["created_utc"],
                },
                cache_control="no-cache",
                headers=headers,
            )
            return

        if len(parts) == 5 and parts[:2] == ["v1", "profiles"] and parts[3] == "history":
            profile_name = parts[2]
            if not PROFILE_NAME.fullmatch(profile_name):
                self._error(400, "invalid_profile_name", "Profile name is invalid.")
                return
            try:
                revision = int(parts[4])
            except ValueError:
                self._error(400, "invalid_revision", "Revision must be a positive integer.")
                return
            if not 1 <= revision <= (1 << 63) - 1:
                self._error(400, "invalid_revision", "Revision must be a positive integer.")
                return
            row = self._revision_row(profile_name, revision)
            if row is None:
                self._error(404, "revision_not_found", "Profile revision was not found.")
                return
            self._send_json(
                200,
                {
                    "profile": json.loads(row["profile_json"]),
                    "graphics": json.loads(row["graphics_json"]),
                    "cfg": row["cfg_text"],
                    "revision": row["revision"],
                    "createdUtc": row["created_utc"],
                },
                cache_control="no-cache",
            )
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

        if len(parts) == 4 and parts[:2] == ["v1", "releases"] and parts[3] == "manifest":
            version = parts[2]
            if not _is_launcher_release_version(version):
                self._error(400, "invalid_release_version", "Release version is invalid.")
                return
            with _connect() as connection:
                row = connection.execute(
                    "SELECT manifest_json FROM releases WHERE version = ?", (version,)
                ).fetchone()
            if row is None:
                self._error(404, "release_not_found", "Release was not found.")
                return
            self._send_bytes(
                200,
                row["manifest_json"].encode("utf-8"),
                "application/json; charset=utf-8",
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

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
        if parts == ["v1", "actions"]:
            self._create_action_request()
            return
        if len(parts) == 4 and parts[:2] == ["v1", "actions"] and parts[3] == "claim":
            self._claim_action_request(parts[2])
            return
        if len(parts) == 4 and parts[:2] == ["v1", "actions"] and parts[3] == "cancel":
            self._cancel_action_request(parts[2])
            return
        if len(parts) == 4 and parts[:2] == ["v1", "actions"] and parts[3] == "events":
            self._post_action_event(parts[2])
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
