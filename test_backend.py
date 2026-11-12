from __future__ import annotations

import json
import base64
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import import_repository
import publish
import server
import backup
import github_mirror
import inspect_staged_releases
from validation import PROFILE_CFG_FILES, PROFILE_DEFAULTS, normalize_graphics, normalize_profile, validate_cfg


def profile_payload(name: str = "FabMode") -> dict:
    profile = {
        "SchemaVersion": 1,
        "ProfileName": name,
        **PROFILE_DEFAULTS[name],
        "CfgDownloadUrl": f"https://raw.githubusercontent.com/fabjoncela/Cs-script/main/{PROFILE_CFG_FILES[name]}",
    }
    return normalize_profile(name, profile)


def launcher_asset(label: bytes) -> bytes:
    return label + b"\n" + b"x" * max(0, 10_000 - len(label) - 1)


def graphics_payload(name: str = "FabMode") -> dict:
    return {
        "SchemaVersion": 1,
        "ProfileName": name,
        "DisplayPolicy": "ApplyPreset",
        "RefreshPolicy": "Exact",
        "ScalingMode": 2,
        "Settings": {
            "Width": 1280,
            "Height": 960,
            "RefreshHz": 360,
            "AspectRatioMode": 0,
            "Fullscreen": 1,
            "MSAASamples": 2,
            "ShadowQuality": 2,
            "DynamicShadows": 1,
            "TextureDetail": 1,
            "ShaderQuality": 0,
            "ParticleDetail": 1,
            "AmbientOcclusion": 0,
            "HDRDetail": 3,
            "NvidiaReflex": 2,
            "TextureFiltering": 3,
            "FSRDetail": 0,
            "VSync": 0,
            "CMAA": 0,
        },
    }


class ValidationTests(unittest.TestCase):
    def test_profile_legacy_cfg_url_is_rewritten_and_optional_fields_default(self) -> None:
        raw = {"SchemaVersion": 1, "ProfileName": "OliMode", **PROFILE_DEFAULTS["OliMode"]}
        raw["CfgDownloadUrl"] = "https://raw.githubusercontent.com/fabjoncela/Cs-script/main/oli.cfg"
        raw.pop("LaunchWebsiteUrl")
        raw.pop("LaunchWebsiteEnabled")
        result = normalize_profile("OliMode", raw)
        self.assertEqual(result["CfgDownloadUrl"], "/v1/cfg/oli.cfg")
        self.assertTrue(result["LaunchWebsiteEnabled"])
        self.assertEqual(result["LaunchWebsiteUrl"], "https://metastack.gg/")

        raw["LaunchWebsiteUrl"] = "  HTTPS://Example.COM  "
        canonical = normalize_profile("OliMode", raw)
        self.assertEqual(canonical["LaunchWebsiteUrl"], "https://example.com/")

        raw.pop("CfgDownloadUrl")
        without_cfg_url = normalize_profile("OliMode", raw)
        self.assertEqual(without_cfg_url["CfgDownloadUrl"], "/v1/cfg/oli.cfg")

    def test_profile_rejects_invalid_ranges_types_urls_and_legacy_sensitivity(self) -> None:
        invalid_mutations = [
            ("PointerSpeed", 0),
            ("LogitechDPI", 99),
            ("RefreshHz", 601),
            ("Hue", -2),
            ("ScalingMode", 4),
            ("ApplyNvidia", 1),
            ("VoicePushToTalkKey", "F13"),
            ("CfgDownloadUrl", "https://evil.example/fab.cfg"),
            ("LaunchWebsiteUrl", "https://user:pass@example.com/"),
        ]
        for field, value in invalid_mutations:
            with self.subTest(field=field):
                raw = {"SchemaVersion": 1, "ProfileName": "FabMode", **PROFILE_DEFAULTS["FabMode"]}
                raw["CfgDownloadUrl"] = "https://raw.githubusercontent.com/fabjoncela/Cs-script/main/fab.cfg"
                raw[field] = value
                with self.assertRaises(ValueError):
                    normalize_profile("FabMode", raw)
        raw = profile_payload()
        raw["CS2Sensitivity"] = 1.2
        with self.assertRaises(ValueError):
            normalize_profile("FabMode", raw)

    def test_graphics_ranges_required_fields_and_display_policy(self) -> None:
        normalized = normalize_graphics("FabMode", graphics_payload(), profile_payload())
        self.assertEqual(normalized["Settings"]["MSAASamples"], 2)
        profile = profile_payload()
        profile["ScalingMode"] = 3
        missing_scaling = graphics_payload()
        missing_scaling.pop("ScalingMode")
        self.assertEqual(normalize_graphics("FabMode", missing_scaling, profile)["ScalingMode"], 3)
        invalid = graphics_payload()
        invalid["Settings"]["HDRDetail"] = 4
        with self.assertRaises(ValueError):
            normalize_graphics("FabMode", invalid, profile_payload())
        invalid = graphics_payload()
        del invalid["Settings"]["CMAA"]
        with self.assertRaises(ValueError):
            normalize_graphics("FabMode", invalid, profile_payload())
        invalid = graphics_payload()
        invalid["Settings"]["UnknownFutureSetting"] = 1
        with self.assertRaises(ValueError):
            normalize_graphics("FabMode", invalid, profile_payload())
        invalid = graphics_payload()
        invalid["DisplayPolicy"] = ""
        with self.assertRaises(ValueError):
            normalize_graphics("FabMode", invalid, profile_payload())
        invalid = graphics_payload()
        invalid["ScalingMode"] = None
        with self.assertRaises(ValueError):
            normalize_graphics("FabMode", invalid, profile_payload())
        invalid = graphics_payload()
        invalid["DisplayPolicy"] = "PreserveCurrent"
        normalized_legacy_policy = normalize_graphics("FabMode", invalid, profile_payload())
        self.assertEqual(normalized_legacy_policy["DisplayPolicy"], "ApplyPreset")
        self.assertEqual(normalized_legacy_policy["Settings"]["Width"], invalid["Settings"]["Width"])

    def test_cfg_matches_cafelauncher_sensitivity_rules(self) -> None:
        for cfg in ('sensitivity "0.001"\n', "sensitivity 20\n", "// sensitivity nope\nsensitivity 1.25 // ok\n"):
            with self.subTest(cfg=cfg):
                self.assertEqual(validate_cfg(cfg), cfg)
        for cfg in (
            "sensitivity 0\n",
            "sensitivity 20.1\n",
            "sensitivity nope\n",
            "sensitivity 1\nsensitivity 2\n",
            "bind x +jump\n",
        ):
            with self.subTest(cfg=cfg):
                with self.assertRaises(ValueError):
                    validate_cfg(cfg)
        with self.assertRaises(ValueError):
            validate_cfg("sensitivity 1\n\ud800")

    def test_publisher_rejects_non_origin_urls_and_bad_ports(self) -> None:
        for url in (
            "https://example.com/api",
            "https://example.com:invalid",
            "http://example.com",
            "https://user:pass@example.com",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                publish._base_url(url)
        self.assertEqual(publish._base_url("http://127.0.0.1:8090/"), "http://127.0.0.1:8090")


class GitHubMirrorTests(unittest.TestCase):
    def test_publisher_creates_atomic_commit_on_existing_mirror_branch(self) -> None:
        client = github_mirror.GitHubClient("example/data", "t" * 40)
        requests = []
        content = b'{"schemaVersion":1}'
        expected_blob = github_mirror._git_blob_sha(content)
        client.current_tree = lambda _branch: ("parent-sha", "base-tree-sha", {})

        def fake_request(method, path, payload=None):
            requests.append((method, path, payload))
            if path == "/git/blobs":
                raw = base64.b64decode(payload["content"])
                return {"sha": github_mirror._git_blob_sha(raw)}
            if path == "/git/trees":
                return {"sha": "tree-sha"}
            if path == "/git/commits":
                self.assertEqual(payload["parents"], ["parent-sha"])
                return {"sha": "commit-sha"}
            if path == "/git/refs/heads/main":
                self.assertEqual(payload, {"sha": "commit-sha", "force": False})
                return {}
            self.fail(f"Unexpected GitHub API request: {method} {path}")

        client.request = fake_request
        commit_sha = client.publish_snapshot("main", 1, {"mirror-manifest.json": content})
        self.assertEqual(commit_sha, "commit-sha")
        self.assertEqual(requests[0][0:2], ("POST", "/git/blobs"))
        self.assertEqual(expected_blob, github_mirror._git_blob_sha(content))

    def test_publisher_reloads_new_branch_head_after_ref_conflict(self) -> None:
        client = github_mirror.GitHubClient("example/data", "t" * 40)
        branch_heads = iter(
            [
                ("old-parent", "old-tree", {}),
                ("new-parent", "new-tree", {}),
            ]
        )
        client.current_tree = lambda _branch: next(branch_heads)
        requests = []
        content = b'{"generation":4}'
        expected_blob = github_mirror._git_blob_sha(content)

        def fake_request(method, path, payload=None):
            requests.append((method, path, payload))
            if path == "/git/blobs":
                return {"sha": github_mirror._git_blob_sha(base64.b64decode(payload["content"]))}
            if path == "/git/trees":
                return {"sha": f"tree-{len([r for r in requests if r[1] == '/git/trees'])}"}
            if path == "/git/commits":
                return {"sha": f"commit-{len([r for r in requests if r[1] == '/git/commits'])}"}
            if path == "/git/refs/heads/main":
                if payload["sha"] == "commit-1":
                    raise github_mirror.GitHubHTTPError(422, "simulated non-fast-forward update")
                self.assertEqual(payload, {"sha": "commit-2", "force": False})
                return {}
            self.fail(f"Unexpected GitHub API request: {method} {path}")

        client.request = fake_request
        result = client.publish_snapshot("main", 4, {"mirror-manifest.json": content})
        self.assertEqual(result, "commit-2")
        ref_updates = [request for request in requests if request[1] == "/git/refs/heads/main"]
        self.assertEqual(len(ref_updates), 2)
        self.assertEqual(ref_updates[0][2]["sha"], "commit-1")
        self.assertEqual(ref_updates[1][2]["sha"], "commit-2")
        self.assertEqual(expected_blob, github_mirror._git_blob_sha(content))

    def test_publisher_requires_initialized_repository_branch(self) -> None:
        client = github_mirror.GitHubClient("example/data", "t" * 40)
        client.current_tree = lambda _branch: (None, None, {})
        with self.assertRaisesRegex(github_mirror.MirrorError, "initial README commit"):
            client.publish_snapshot("main", 1, {"mirror-manifest.json": b"{}"})


class ImportTests(unittest.TestCase):
    def test_import_reads_all_profiles_and_rewrites_github_cfg_urls(self) -> None:
        with tempfile.TemporaryDirectory() as source_temp, tempfile.TemporaryDirectory() as data_temp:
            root = Path(source_temp)
            (root / "profiles").mkdir()
            (root / "graphics").mkdir()
            for name, cfg_name in PROFILE_CFG_FILES.items():
                profile = {"SchemaVersion": 1, "ProfileName": name, **PROFILE_DEFAULTS[name]}
                profile["CfgDownloadUrl"] = f"https://raw.githubusercontent.com/fabjoncela/Cs-script/main/{cfg_name}"
                if name == "OliMode":
                    profile.pop("LaunchWebsiteEnabled")
                    profile.pop("LaunchWebsiteUrl")
                (root / "profiles" / f"{name}.json").write_text(json.dumps(profile), encoding="utf-8")
                (root / "graphics" / f"{name}.json").write_text(json.dumps(graphics_payload(name)), encoding="utf-8")
                (root / cfg_name).write_bytes('echo "profile"\r\nsensitivity "1.2"\r\n'.encode("utf-8"))

            with patch.dict(os.environ, {"CAFELAUNCHER_DATA_DIR": data_temp}, clear=False):
                bundles = import_repository.load_bundle(root)
                self.assertEqual(len(bundles), 6)
                count = import_repository.import_bundles(bundles)
                self.assertEqual(count, 6)
                with server._connect() as connection:
                    rows = connection.execute("SELECT profile_name, revision, profile_json, cfg_text FROM profile_revisions").fetchall()
                self.assertEqual(len(rows), 6)
                for row in rows:
                    imported_profile = json.loads(row["profile_json"])
                    self.assertEqual(row["revision"], 1)
                    self.assertEqual(imported_profile["CfgDownloadUrl"], f"/v1/cfg/{PROFILE_CFG_FILES[row['profile_name']]}")
                    self.assertIn("\r\n", row["cfg_text"])

    def test_backup_uses_sqlite_backup_and_includes_assets(self) -> None:
        with tempfile.TemporaryDirectory() as data_temp, tempfile.TemporaryDirectory() as backup_temp:
            with patch.dict(os.environ, {"CAFELAUNCHER_DATA_DIR": data_temp}, clear=False):
                server._initialize()
                with server._connect() as connection:
                    connection.execute(
                        "INSERT INTO profile_revisions "
                        "(profile_name, revision, created_utc, profile_json, graphics_json, cfg_text) "
                        "VALUES (?, 1, ?, ?, ?, ?)",
                        ("FabMode", server._utc_now(), "{}", "{}", 'sensitivity "1.2"\n'),
                    )
                asset = Path(data_temp) / "releases" / "v1" / "launcher.bin"
                asset.parent.mkdir(parents=True)
                asset.write_bytes(b"backup asset")
                result = backup.create_backup(Path(backup_temp) / "snapshot")
                with closing(sqlite3.connect(result / "cafelauncher.sqlite3")) as connection:
                    count = connection.execute("SELECT COUNT(*) FROM profile_revisions").fetchone()[0]
                self.assertEqual(count, 1)
                self.assertEqual((result / "releases" / "v1" / "launcher.bin").read_bytes(), b"backup asset")

                nested_destination = Path(data_temp) / "releases" / "nested-backup"
                with self.assertRaisesRegex(ValueError, "outside and separate"):
                    backup.create_backup(nested_destination)
                self.assertFalse(nested_destination.exists())


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {
                "CAFELAUNCHER_DATA_DIR": self.temp.name,
                "CAFELAUNCHER_SYNC_TOKEN": "sync-token-" + "s" * 40,
                "CAFELAUNCHER_PUBLISH_TOKEN": "publish-token-" + "p" * 40,
                "CAFELAUNCHER_ACTION_ORIGIN": "https://cafelauncher.example:10000",
            },
            clear=False,
        )
        self.environment.start()
        server._initialize()
        self.httpd = server.BoundedHTTPServer(("127.0.0.1", 0), server.ApiHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.environment.stop()
        self.temp.cleanup()

    def request(self, path: str, *, method: str = "GET", body: bytes | None = None, token: str | None = None, content_type: str = "application/json", headers: dict[str, str] | None = None):
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = content_type
        request = Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            return error.code, error.headers, error.read()

    def create_action_session(self) -> tuple[str, str]:
        status, headers, body = self.request(
            "/v1/actions/session",
            headers={"Origin": "https://cafelauncher.example:10000", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 200)
        session_cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("; Secure; HttpOnly; SameSite=Strict", headers["Set-Cookie"])
        return session_cookie, json.loads(body)["csrfToken"]

    @staticmethod
    def action_browser_headers(session_cookie: str, csrf: str | None = None, *, origin: str = "https://cafelauncher.example:10000") -> dict[str, str]:
        headers = {
            "Origin": origin,
            "Sec-Fetch-Site": "same-origin",
            "Cookie": session_cookie,
        }
        if csrf is not None:
            headers["X-Cafe-CSRF"] = csrf
        return headers

    def create_action(self, action: str, profile: str | None = None) -> tuple[str, str, str]:
        session_cookie, csrf = self.create_action_session()
        request_id = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        body = json.dumps({"action": action, "profile": profile, "clientRequestId": request_id}).encode()
        status, _, response_body = self.request(
            "/v1/actions",
            method="POST",
            body=body,
            headers=self.action_browser_headers(session_cookie, csrf),
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(response_body)["requestId"], request_id)
        return request_id, session_cookie, csrf

    def test_web_profile_actions_require_known_profile_and_bounded_results(self) -> None:
        valid = {
            "summary": "CafeLauncher action finished.",
            "steps": [{"name": "CS2 settings", "status": "SUCCESS", "detail": "Verified."}],
            "data": {"executionOutcomeKnown": True},
        }
        for action in ("apply-profile", "apply-graphics"):
            with self.subTest(action=action):
                self.assertEqual(server.ApiHandler._validate_action_result(action, valid), valid)
                self.assertIsNone(server.ApiHandler._validate_action_result(action, {
                    **valid, "data": {"executionOutcomeKnown": True, "extra": "not allowed"}
                }))
                request_id, _session, _csrf = self.create_action(action, "FabMode")
                self.assertTrue(request_id)

        session_cookie, csrf = self.create_action_session()
        request_id = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        body = json.dumps({"action": "apply-graphics", "profile": "Unknown", "clientRequestId": request_id}).encode()
        status, _, _ = self.request(
            "/v1/actions", method="POST", body=body,
            headers=self.action_browser_headers(session_cookie, csrf),
        )
        self.assertEqual(status, 422)

    def test_web_action_rejects_outdated_runner_before_claim_with_update_result(self) -> None:
        for action, profile in (
            ("detect-pc", None),
            ("apply-profile", "Provmode"),
            ("apply-graphics", "Provmode"),
            ("play-exec", "Provmode"),
        ):
            with self.subTest(action=action):
                request_id, session_cookie, _csrf = self.create_action(action, profile)
                capability = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
                claim_body = json.dumps({
                    "runnerVersion": "0.2.0",
                    "protocolVersion": 1,
                    "claimCapability": capability,
                }).encode()
                status, _, response = self.request(f"/v1/actions/{request_id}/claim", method="POST", body=claim_body)
                self.assertEqual(status, 200)
                payload = json.loads(response)
                self.assertEqual(payload["state"], "FAILED")
                self.assertEqual(payload["runnerVersion"], "0.2.0")
                self.assertEqual(payload["minimumRunnerVersion"], server.MIN_WEB_ACTION_RUNNER_VERSION)
                self.assertTrue(payload["runnerUpdateRequired"])
                self.assertIn("open the latest CafeLauncher runner", payload["result"]["summary"])
                self.assertEqual(payload["result"]["steps"][0]["name"], "Runner update")
                self.assertTrue(payload["result"]["data"].get("executionOutcomeKnown", True))
                self.assertEqual(
                    server.ApiHandler._validate_action_result(action, payload["result"]), payload["result"]
                )
                state_status, _, state_body = self.request(
                    f"/v1/actions/{request_id}", headers=self.action_browser_headers(session_cookie)
                )
                self.assertEqual(state_status, 200)
                self.assertTrue(json.loads(state_body)["runnerUpdateRequired"])
                with server._connect() as connection:
                    row = connection.execute(
                        "SELECT state, claim_capability_hash FROM action_requests WHERE request_hash = ?",
                        (server.ApiHandler._action_hash(request_id),),
                    ).fetchone()
                self.assertEqual(row["state"], "FAILED")
                self.assertIsNone(row["claim_capability_hash"])

    def test_current_web_runner_version_can_claim(self) -> None:
        request_id, _session_cookie, _csrf = self.create_action("apply-profile", "Provmode")
        capability = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        claim_body = json.dumps({
            "runnerVersion": server.MIN_WEB_ACTION_RUNNER_VERSION,
            "protocolVersion": 1,
            "claimCapability": capability,
        }).encode()
        status, _, response = self.request(f"/v1/actions/{request_id}/claim", method="POST", body=claim_body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)["state"], "CLAIMED")

    def test_web_action_concurrent_request_retries_create_one_ticket(self) -> None:
        session_cookie, csrf = self.create_action_session()
        request_id = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        body = json.dumps({"action": "detect-pc", "profile": None, "clientRequestId": request_id}).encode()
        headers = self.action_browser_headers(session_cookie, csrf)
        barrier = threading.Barrier(3)
        responses: list[tuple[int, bytes]] = []

        def create_request() -> None:
            barrier.wait(timeout=5)
            status, _, response_body = self.request(
                "/v1/actions", method="POST", body=body, headers=headers
            )
            responses.append((status, response_body))

        workers = [threading.Thread(target=create_request) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=10)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sorted(status for status, _ in responses), [200, 201])
        self.assertEqual({json.loads(response)["requestId"] for _, response in responses}, {request_id})
        with server._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count, MIN(state) AS state FROM action_requests WHERE request_hash = ?",
                (server.ApiHandler._action_hash(request_id),),
            ).fetchone()
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["state"], "REQUESTED")

    def put_bundle(self, expected_revision: int, *, token: str | None = None, profile: dict | None = None, graphics: dict | None = None, cfg: str = 'sensitivity "1.2"\n'):
        payload = {
            "expectedRevision": expected_revision,
            "profile": profile if profile is not None else profile_payload(),
            "graphics": graphics if graphics is not None else graphics_payload(),
            "cfg": cfg,
        }
        return self.request("/v1/profiles/FabMode/sync", method="PUT", body=json.dumps(payload).encode(), token=token)

    def test_web_app_status_page_and_profile_viewer_are_served(self) -> None:
        status, headers, home = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"CafeLauncher", home)
        self.assertIn(f'id="site-version">{server.APP_VERSION}</span>'.encode("ascii"), home)
        self.assertRegex(home.decode("utf-8"), r'datetime="20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"')
        self.assertNotIn(b"@@APP_VERSION@@", home)
        self.assertNotIn(b"@@APP_UPDATED_UTC@@", home)
        self.assertIn(b'id="update-runner-link"', home)
        self.assertIn(b'id="unlock-button"', home)
        self.assertIn(b'class="unlock-recovery-button"', home)
        self.assertLess(home.index(b'id="unlock-button"'), home.index(b"<main>"))
        self.assertNotIn(b'id="unlock-button"', home[home.index(b'id="result-panel"'):])
        self.assertIn(b"Runner update required", self.request("/app.js")[2])
        self.assertIn(b"1. Apply profile", home)
        self.assertIn(b"2. Apply game graphics", home)
        self.assertIn(b"3. Play + Exec", home)
        self.assertNotIn(b"Detect this PC", home)
        self.assertNotIn(b"Capture CS2", home)
        self.assertNotIn(b"Edit Profile", home)
        self.assertIn(b"Play + Exec", home)
        self.assertIn(b'id="apply-profile-button"', home)
        self.assertIn(b'id="apply-graphics-button"', home)
        self.assertIn(b'id="cancel-action-button"', home)
        self.assertIn(b'id="download-action-log-button"', home)
        self.assertIn(b'id="current-step-timing"', home)
        self.assertIn(b"confirm-dialog", home)
        self.assertIn(b'id="install-heading"', home)
        self.assertIn(b"Download CafeLauncher", home)
        self.assertIn(b"reboot", home)
        self.assertIn(b"https://github.com/fabjoncela/Cs-script/releases/latest/download/CafeLauncherWebRunner.exe", home)

        status, _, home = self.request("/status")
        self.assertEqual(status, 200)
        self.assertIn(b'href="/viewer"', home)
        self.assertIn(b'href="/"', home)
        self.assertIn(b"CafeLauncher home", home)
        self.assertIn(b"Service healthy", home)
        self.assertIn(b"Last successful sync:", home)
        self.assertIn(b"Copy diagnostics", home)
        self.assertIn(b"/status.js", home)
        self.assertIn(b"No backend-hosted package published yet", home)
        self.assertIn(b"Backend-hosted release", home)
        self.assertIn(b"The launcher currently auto-updates from Cs-script on GitHub.", home)
        self.assertIn(b"Latest profile save", home)
        self.assertIn(b"No profile saves yet", home)
        self.assertIn(b'href="https://github.com/fabjoncela/cafelauncher-backend#api-contract"', home)

        stats_status, _, stats = self.request("/stats")
        self.assertEqual(stats_status, 200)
        self.assertIn(b"Backend status", stats)
        self.assertIn(b'href="/"', stats)

        status, headers, page = self.request("/viewer")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b'href="/"', page)
        self.assertIn(b"CafeLauncher home", page)
        self.assertIn(b'href="/status"', page)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn(b'<select id="profile-select">', page)
        self.assertIn(b"Loading profiles", page)
        self.assertIn(b'id="viewer-search-input"', page)
        self.assertIn(b'id="compare-select"', page)
        self.assertIn(b'id="history-changed-only"', page)
        self.assertIn(b"Show only changed profile and graphics settings", page)
        self.assertIn(b">Profile saves<", page)
        self.assertIn(b'id="load-history-button"', page)

        for path, expected_type in (
            ("/app.css", "text/css"),
            ("/app.js", "text/javascript"),
            ("/viewer.css", "text/css"),
            ("/viewer.js", "text/javascript"),
            ("/status.css", "text/css"),
            ("/status.js", "text/javascript"),
        ):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertIn(expected_type, headers["Content-Type"])
                self.assertTrue(body)
                if path == "/viewer.js":
                    self.assertIn(b"/v1/profiles/${encodeURIComponent(profileName)}/history?", body)
                    self.assertIn(b"Changes vs current", body)
                    self.assertIn(b"/v1/viewer-schema", body)
                    self.assertIn(b"buildLineDiff", body)
                    self.assertIn(b"Copy saved CFG", body)
                    self.assertIn(b"Download saved CFG", body)
                    self.assertIn(b"current-cfg-actions", body)
                    self.assertIn(b"renderProfileComparison", body)
                if path == "/app.js":
                    self.assertIn(b"requestActionCancellation", body)
                    self.assertIn(b"downloadActionLog", body)
                    self.assertIn(b"redactDiagnosticText", body)
                    self.assertIn(b"since previous event", body)
                    self.assertIn(b"Server-observed time to last event", body)
                    self.assertIn(b"formatElapsedMs", body)
                    self.assertIn(b"updateLiveActionTimer", body)
                    self.assertIn(b"Current stage:", body)
                if path == "/status.js":
                    self.assertIn(b"copyDiagnostics", body)

        status, headers, body = self.request("/v1/viewer-schema")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(headers["Cache-Control"], "no-cache")
        schema = json.loads(body)
        self.assertEqual(schema["version"], 1)
        self.assertEqual(schema["profiles"][-1], {"name": "Provmode", "cfg": "prov.cfg"})
        self.assertIn("graphicsSettings", {section["id"] for section in schema["sections"]})
        self.assertEqual(schema["fields"]["NvidiaReflex"]["values"]["2"], "Enabled + Boost")

    def test_web_action_session_ticket_claim_and_detect_result_roundtrip(self) -> None:
        session_cookie, csrf = self.create_action_session()
        request_id = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        create_body = json.dumps({"action": "detect-pc", "profile": None, "clientRequestId": request_id}).encode()
        browser_headers = self.action_browser_headers(session_cookie, csrf)
        status, _, created = self.request("/v1/actions", method="POST", body=create_body, headers=browser_headers)
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(created)["state"], "REQUESTED")

        status, _, retried = self.request("/v1/actions", method="POST", body=create_body, headers=browser_headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(retried)["requestId"], request_id)

        capability = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        claim_body = json.dumps({
            "runnerVersion": server.MIN_WEB_ACTION_RUNNER_VERSION,
            "protocolVersion": 1,
            "claimCapability": capability,
        }).encode()
        status, _, claimed = self.request(f"/v1/actions/{request_id}/claim", method="POST", body=claim_body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(claimed)["state"], "CLAIMED")
        status, _, retried_claim = self.request(f"/v1/actions/{request_id}/claim", method="POST", body=claim_body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(retried_claim)["requestId"], request_id)

        progress = {
            "eventId": "11111111-1111-4111-8111-111111111111",
            "state": "RUNNING",
            "step": {"name": "PC discovery", "status": "RUNNING", "detail": "Detecting local paths."},
        }
        status, _, _ = self.request(
            f"/v1/actions/{request_id}/events",
            method="POST",
            body=json.dumps(progress).encode(),
            headers={"Authorization": f"Bearer {capability}"},
        )
        self.assertEqual(status, 200)

        detect_result = {
            "summary": "PC detection completed for the current Windows user.",
            "steps": [
                {"name": "Steam discovery", "status": "SUCCESS", "detail": "Steam detected."},
                {"name": "CS2 path discovery", "status": "SUCCESS", "detail": "A unique config path was detected."},
            ],
            "data": {
                "steam": {
                    "status": "DETECTED",
                    "executable": "C:\\Steam\\steam.exe",
                    "libraries": ["C:\\SteamLibrary"],
                    "activeSessionPresent": True,
                },
                "cs2": {
                    "status": "DETECTED",
                    "installPath": "C:\\SteamLibrary\\steamapps\\common\\Counter-Strike Global Offensive",
                    "configPath": "C:\\SteamLibrary\\steamapps\\common\\Counter-Strike Global Offensive\\game\\csgo\\cfg",
                },
            },
        }
        terminal = {
            "eventId": "22222222-2222-4222-8222-222222222222",
            "state": "COMPLETED",
            "result": detect_result,
        }
        status, _, accepted = self.request(
            f"/v1/actions/{request_id}/events",
            method="POST",
            body=json.dumps(terminal).encode(),
            headers={"Authorization": f"Bearer {capability}"},
        )
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(accepted)["duplicate"])
        status, _, duplicate = self.request(
            f"/v1/actions/{request_id}/events",
            method="POST",
            body=json.dumps(terminal).encode(),
            headers={"Authorization": f"Bearer {capability}"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(duplicate)["duplicate"])

        status, _, state = self.request(
            f"/v1/actions/{request_id}", headers=self.action_browser_headers(session_cookie)
        )
        self.assertEqual(status, 200)
        state_payload = json.loads(state)
        self.assertEqual(state_payload["state"], "COMPLETED")
        self.assertEqual(state_payload["result"]["data"], detect_result["data"])

        other_session, _ = self.create_action_session()
        status, _, _ = self.request(
            f"/v1/actions/{request_id}", headers=self.action_browser_headers(other_session)
        )
        self.assertEqual(status, 404)

    def test_web_action_cancel_is_session_bound_and_runner_can_read_flag(self) -> None:
        request_id, session_cookie, csrf = self.create_action("apply-graphics", "FabMode")
        browser_headers = self.action_browser_headers(session_cookie, csrf)
        browser_headers["Content-Type"] = "application/json"
        status, _, canceled = self.request(
            f"/v1/actions/{request_id}/cancel", method="POST", body=b"{}", headers=browser_headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(canceled)["state"], "CANCELED")
        self.assertTrue(json.loads(canceled)["cancelRequested"])
        status, _, claim_error = self.request(
            f"/v1/actions/{request_id}/claim",
            method="POST",
            body=json.dumps({"runnerVersion": server.MIN_WEB_ACTION_RUNNER_VERSION, "protocolVersion": 1, "claimCapability": "A" * 43}).encode(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(claim_error)["error"], "action_not_claimable")

        running_id, running_cookie, running_csrf = self.create_action("apply-graphics", "FabMode")
        capability = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        claim_body = json.dumps({"runnerVersion": server.MIN_WEB_ACTION_RUNNER_VERSION, "protocolVersion": 1, "claimCapability": capability}).encode()
        status, _, _ = self.request(f"/v1/actions/{running_id}/claim", method="POST", body=claim_body)
        self.assertEqual(status, 200)
        status, _, initial = self.request(
            f"/v1/actions/{running_id}/control", headers={"Authorization": f"Bearer {capability}"}
        )
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(initial)["cancelRequested"])
        status, _, pending = self.request(
            f"/v1/actions/{running_id}/cancel", method="POST", body=b"{}",
            headers={**self.action_browser_headers(running_cookie, running_csrf), "Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(pending)["cancelRequested"])
        status, _, control = self.request(
            f"/v1/actions/{running_id}/control", headers={"Authorization": f"Bearer {capability}"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(control)["cancelRequested"])
        status, _, state = self.request(
            f"/v1/actions/{running_id}", headers=self.action_browser_headers(running_cookie)
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(state)["cancelRequested"])
        status, _, _ = self.request(
            f"/v1/actions/{running_id}/control", headers={"Authorization": f"Bearer {'B' * 43}"}
        )
        self.assertEqual(status, 401)

    def test_web_action_rejects_cross_origin_bad_csrf_and_uncertain_state_mismatch(self) -> None:
        status, _, _ = self.request(
            "/v1/actions/session",
            headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)
        with patch.dict(os.environ, {"CAFELAUNCHER_ACTION_ORIGIN": ""}):
            status, _, _ = self.request(
                "/v1/actions/session",
                headers={"Origin": "https://cafelauncher.example:10000", "Sec-Fetch-Site": "same-origin"},
            )
        self.assertEqual(status, 503)

        session_cookie, csrf = self.create_action_session()
        request_id = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        create_body = json.dumps({"action": "play-exec", "profile": "FabMode", "clientRequestId": request_id}).encode()
        status, _, _ = self.request(
            "/v1/actions", method="POST", body=create_body,
            headers=self.action_browser_headers(session_cookie, "A" * 43),
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "/v1/actions", method="POST", body=create_body,
            headers=self.action_browser_headers(session_cookie, csrf, origin="https://attacker.example"),
        )
        self.assertEqual(status, 403)
        status, _, created = self.request(
            "/v1/actions", method="POST", body=create_body,
            headers=self.action_browser_headers(session_cookie, csrf),
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(created)["profile"], "FabMode")

        capability = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        claim_body = json.dumps({"runnerVersion": server.MIN_WEB_ACTION_RUNNER_VERSION, "protocolVersion": 1, "claimCapability": capability}).encode()
        status, _, _ = self.request(f"/v1/actions/{request_id}/claim", method="POST", body=claim_body)
        self.assertEqual(status, 200)
        status, _, _ = self.request(
            f"/v1/actions/{request_id}/events",
            method="POST",
            body=json.dumps({"eventId": "33333333-3333-4333-8333-333333333333", "state": "RUNNING"}).encode(),
            headers={"Authorization": f"Bearer {capability}"},
        )
        self.assertEqual(status, 200)

        uncertain_result = {
            "summary": "Outcome is uncertain.",
            "steps": [],
            "data": {
                "steamLaunchRequested": False,
                "cs2AlreadyRunning": False,
                "executionOutcomeKnown": False,
                "cfgName": "fab.cfg",
                "consoleCommands": [],
            },
        }
        terminal = {
            "eventId": "44444444-4444-4444-8444-444444444444",
            "state": "FAILED",
            "result": uncertain_result,
        }
        status, _, _ = self.request(
            f"/v1/actions/{request_id}/events",
            method="POST",
            body=json.dumps(terminal).encode(),
            headers={"Authorization": f"Bearer {capability}"},
        )
        self.assertEqual(status, 422)
        terminal["state"] = "INTERRUPTED"
        status, _, accepted = self.request(
            f"/v1/actions/{request_id}/events",
            method="POST",
            body=json.dumps(terminal).encode(),
            headers={"Authorization": f"Bearer {capability}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(accepted)["state"], "INTERRUPTED")

    def test_web_action_upload_expiry_becomes_interrupted_and_rejects_late_results(self) -> None:
        self.assertEqual(server.ACTION_RESULT_TTL_SECONDS, 180)
        request_id, session_cookie, _csrf = self.create_action("detect-pc")
        capability = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        claim_body = json.dumps({"runnerVersion": server.MIN_WEB_ACTION_RUNNER_VERSION, "protocolVersion": 1, "claimCapability": capability}).encode()
        status, _, _ = self.request(f"/v1/actions/{request_id}/claim", method="POST", body=claim_body)
        self.assertEqual(status, 200)

        with server._connect() as connection:
            connection.execute(
                "UPDATE action_requests SET upload_expires_at = ? WHERE request_hash = ?",
                (0, server.ApiHandler._action_hash(request_id)),
            )
            connection.commit()

        status, _, state = self.request(
            f"/v1/actions/{request_id}", headers=self.action_browser_headers(session_cookie)
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(state)["state"], "INTERRUPTED")

        status, _, _ = self.request(f"/v1/actions/{request_id}/claim", method="POST", body=claim_body)
        self.assertEqual(status, 409)
        late_result = {
            "summary": "Late result.",
            "steps": [],
            "data": {
                "steam": {"status": "NOT_DETECTED", "executable": None, "libraries": [], "activeSessionPresent": False},
                "cs2": {"status": "NOT_UNIQUELY_DETECTED", "installPath": None, "configPath": None},
            },
        }
        status, _, _ = self.request(
            f"/v1/actions/{request_id}/events",
            method="POST",
            body=json.dumps({"eventId": "55555555-5555-4555-8555-555555555555", "state": "FAILED", "result": late_result}).encode(),
            headers={"Authorization": f"Bearer {capability}"},
        )
        self.assertEqual(status, 410)

    def test_health_reports_database_unavailable(self) -> None:
        with patch.object(server, "_connect", side_effect=sqlite3.OperationalError("simulated storage outage")):
            status, _, body = self.request("/v1/health")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "storage_unavailable")

    def test_token_required_atomic_revision_conflict_and_history(self) -> None:
        status, _, _ = self.put_bundle(0)
        self.assertEqual(status, 401)
        sync_token = os.environ["CAFELAUNCHER_SYNC_TOKEN"]
        status, _, body = self.put_bundle(0, token=sync_token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["revision"], 1)

        status, headers, body = self.request("/v1/profiles/FabMode")
        self.assertEqual(status, 200)
        profile = json.loads(body)
        self.assertEqual(profile["CfgDownloadUrl"], "/v1/cfg/fab.cfg")
        self.assertEqual(headers["X-Profile-Revision"], "1")

        status, headers, body = self.request("/v1/cfg/fab.cfg")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'sensitivity "1.2"\n')
        self.assertEqual(headers["X-Profile-Revision"], "1")

        status, _, _ = self.request("/v1/cfg/FabMode")
        self.assertEqual(status, 400)

        status, _, body = self.request("/v1/graphics/FabMode/history?limit=3")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["revisions"][0]["revision"], 1)

        status, _, body = self.put_bundle(0, token=sync_token)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["currentRevision"], 1)

    def test_identical_sync_does_not_create_revision_or_mirror_generation(self) -> None:
        sync_token = os.environ["CAFELAUNCHER_SYNC_TOKEN"]
        status, _, first_body = self.put_bundle(0, token=sync_token)
        self.assertEqual(status, 200)
        first = json.loads(first_body)
        with server._connect() as connection:
            generation_before = connection.execute(
                "SELECT generation FROM github_mirror_state WHERE singleton = 1"
            ).fetchone()[0]

        status, _, second_body = self.put_bundle(1, token=sync_token)
        self.assertEqual(status, 200)
        second = json.loads(second_body)
        self.assertTrue(second["unchanged"])
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(second["createdUtc"], first["createdUtc"])
        with server._connect() as connection:
            revision_count = connection.execute(
                "SELECT COUNT(*) FROM profile_revisions WHERE profile_name = 'FabMode'"
            ).fetchone()[0]
            generation_after = connection.execute(
                "SELECT generation FROM github_mirror_state WHERE singleton = 1"
            ).fetchone()[0]
        self.assertEqual(revision_count, 1)
        self.assertEqual(generation_after, generation_before)

    def test_graphics_history_omits_profile_and_cfg_only_revisions(self) -> None:
        sync_token = os.environ["CAFELAUNCHER_SYNC_TOKEN"]
        status, _, _ = self.put_bundle(0, token=sync_token)
        self.assertEqual(status, 200)

        profile_only = profile_payload()
        profile_only["PointerSpeed"] = 11
        status, _, _ = self.put_bundle(1, token=sync_token, profile=profile_only)
        self.assertEqual(status, 200)

        status, _, _ = self.put_bundle(2, token=sync_token, profile=profile_only, cfg='sensitivity "1.3"\n')
        self.assertEqual(status, 200)

        graphics_changed = graphics_payload()
        graphics_changed["Settings"]["MSAASamples"] = 4
        status, _, _ = self.put_bundle(3, token=sync_token, profile=profile_only, graphics=graphics_changed, cfg='sensitivity "1.3"\n')
        self.assertEqual(status, 200)

        status, _, body = self.request("/v1/graphics/FabMode/history?limit=3")
        self.assertEqual(status, 200)
        history = json.loads(body)["revisions"]
        self.assertEqual([entry["revision"] for entry in history], [4, 1])

        with patch.dict(
            os.environ,
            {
                "CAFELAUNCHER_GITHUB_REPO": "example/cafelauncher-data",
                "CAFELAUNCHER_GITHUB_BRANCH": "main",
                "CAFELAUNCHER_GITHUB_TOKEN": "github-token-" + "g" * 40,
            },
        ):
            _, files = github_mirror.build_snapshot()
        mirrored_history = json.loads(files["history/graphics/FabMode.json"])["revisions"]
        self.assertEqual([entry["revision"] for entry in mirrored_history], [4, 1])

    def test_profile_history_lists_every_bundle_revision_and_returns_complete_snapshot(self) -> None:
        sync_token = os.environ["CAFELAUNCHER_SYNC_TOKEN"]
        initial_profile = profile_payload()
        status, _, _ = self.put_bundle(0, token=sync_token, profile=initial_profile)
        self.assertEqual(status, 200)

        changed_profile = profile_payload()
        changed_profile["PointerSpeed"] = 11
        status, _, _ = self.put_bundle(1, token=sync_token, profile=changed_profile)
        self.assertEqual(status, 200)

        cfg = 'sensitivity "1.3"\nbind "SPACE" "+jump"\n'
        status, _, _ = self.put_bundle(2, token=sync_token, profile=changed_profile, cfg=cfg)
        self.assertEqual(status, 200)

        changed_graphics = graphics_payload()
        changed_graphics["Settings"]["MSAASamples"] = 4
        status, _, _ = self.put_bundle(3, token=sync_token, profile=changed_profile, graphics=changed_graphics, cfg=cfg)
        self.assertEqual(status, 200)

        status, _, body = self.request("/v1/profiles/FabMode/history?limit=2")
        self.assertEqual(status, 200)
        history = json.loads(body)
        self.assertEqual([entry["revision"] for entry in history["revisions"]], [4, 3])
        self.assertTrue(history["hasMore"])
        self.assertEqual(history["nextBeforeRevision"], 3)

        status, _, body = self.request("/v1/profiles/FabMode/history?limit=2&beforeRevision=3")
        self.assertEqual(status, 200)
        older = json.loads(body)
        self.assertEqual([entry["revision"] for entry in older["revisions"]], [2, 1])
        self.assertFalse(older["hasMore"])

        status, _, body = self.request("/v1/profiles/FabMode/history/3")
        self.assertEqual(status, 200)
        snapshot = json.loads(body)
        self.assertEqual(snapshot["revision"], 3)
        self.assertEqual(snapshot["profile"], changed_profile)
        self.assertEqual(snapshot["graphics"], graphics_payload())
        self.assertEqual(snapshot["cfg"], cfg)
        self.assertTrue(snapshot["createdUtc"])

        status, _, body = self.request("/v1/profiles/FabMode/history/99")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "revision_not_found")

        status, _, body = self.request("/v1/profiles/FabMode/history?limit=0")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid_history_cursor")

        status, _, body = self.request("/v1/profiles/FabMode/history/not-a-number")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid_revision")

    def test_api_rejects_invalid_profile_graphics_and_cfg(self) -> None:
        sync_token = os.environ["CAFELAUNCHER_SYNC_TOKEN"]
        bad_profile = profile_payload()
        bad_profile["PointerSpeed"] = 0
        status, _, _ = self.put_bundle(0, token=sync_token, profile=bad_profile)
        self.assertEqual(status, 422)
        bad_graphics = graphics_payload()
        bad_graphics["Settings"]["FSRDetail"] = 5
        status, _, _ = self.put_bundle(0, token=sync_token, graphics=bad_graphics)
        self.assertEqual(status, 422)
        status, _, _ = self.put_bundle(0, token=sync_token, cfg="sensitivity 21\n")
        self.assertEqual(status, 422)
        invalid_unicode = profile_payload()
        invalid_unicode["LaunchWebsiteUrl"] = "https://example.com/\ud800"
        status, _, _ = self.put_bundle(0, token=sync_token, profile=invalid_unicode)
        self.assertEqual(status, 422)
        status, _, _ = self.request(
            "/v1/profiles/FabMode/sync",
            method="PUT",
            body=json.dumps({"expectedRevision": 0, "profile": {}, "graphics": {}, "cfg": "", "unexpected": True}).encode(),
            token=sync_token,
        )
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "/v1/admin/releases/2026.11.07.1/launcher",
            method="PUT",
            body=b"too small",
            token=os.environ["CAFELAUNCHER_PUBLISH_TOKEN"],
            content_type="application/octet-stream",
        )
        self.assertEqual(status, 422)
        status, _, _ = self.request(
            "/v1/admin/releases/2026.11.07.1/launcher",
            method="PUT",
            body=b"x" * 1_000_001,
            token=os.environ["CAFELAUNCHER_PUBLISH_TOKEN"],
            content_type="application/octet-stream",
        )
        self.assertEqual(status, 413)
        status, _, _ = self.request(
            "/v1/admin/releases/v1/launcher",
            method="PUT",
            body=b"not a CafeLauncher release version",
            token=os.environ["CAFELAUNCHER_PUBLISH_TOKEN"],
            content_type="application/octet-stream",
        )
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "/v1/admin/releases/2026.13.07.1/launcher",
            method="PUT",
            body=launcher_asset(b"invalid calendar date"),
            token=os.environ["CAFELAUNCHER_PUBLISH_TOKEN"],
            content_type="application/octet-stream",
        )
        self.assertEqual(status, 400)

    def test_release_publish_manifest_hashes_and_download(self) -> None:
        token = os.environ["CAFELAUNCHER_PUBLISH_TOKEN"]
        launcher = launcher_asset(b"launcher prototype")
        module = b"# graphics module prototype\n"
        status, _, _ = self.request("/v1/admin/releases/2026.11.07.1/launcher", method="PUT", body=launcher, token=token, content_type="application/octet-stream")
        self.assertEqual(status, 201)
        status, _, _ = self.request("/v1/admin/releases/2026.11.07.1/graphics-module", method="PUT", body=module, token=token, content_type="application/octet-stream")
        self.assertEqual(status, 201)
        status, _, body = self.request(
            "/v1/admin/releases/2026.11.07.1/manifest",
            method="PUT",
            body=json.dumps({"releaseNotes": "Prototype release"}).encode(),
            token=token,
        )
        self.assertEqual(status, 201)
        manifest = json.loads(body)
        self.assertEqual(manifest["artifacts"]["launcher"]["size"], len(launcher))
        status, _, body = self.request("/v1/releases/latest")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["version"], "2026.11.07.1")
        status, _, body = self.request("/v1/releases/2026.11.07.1/manifest")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["version"], "2026.11.07.1")
        status, _, body = self.request("/v1/releases/2026.11.07.1/launcher")
        self.assertEqual(status, 200)
        self.assertEqual(body, launcher)

    def test_unpublished_release_asset_cannot_be_replaced_with_different_bytes(self) -> None:
        token = os.environ["CAFELAUNCHER_PUBLISH_TOKEN"]
        path = "/v1/admin/releases/2026.11.07.4/launcher"
        original = launcher_asset(b"first publisher")
        replacement = launcher_asset(b"second publisher")
        status, _, _ = self.request(path, method="PUT", body=original, token=token, content_type="application/octet-stream")
        self.assertEqual(status, 201)
        status, _, body = self.request(path, method="PUT", body=replacement, token=token, content_type="application/octet-stream")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "release_asset_conflict")
        staged = Path(self.temp.name) / "releases" / "2026.11.07.4" / "launcher.bin"
        self.assertEqual(staged.read_bytes(), original)
        status, _, body = self.request(path, method="PUT", body=original, token=token, content_type="application/octet-stream")
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["sha256"], hashlib.sha256(original).hexdigest())

    def test_staged_release_cleanup_requires_unchanged_inventory_and_unpublished_version(self) -> None:
        version = "2026.11.07.6"
        staged = Path(self.temp.name) / "releases" / version
        staged.mkdir(parents=True)
        asset = staged / "launcher.bin"
        asset.write_bytes(launcher_asset(b"staged launcher"))
        report = inspect_staged_releases.inspect_staged_releases(version)
        self.assertEqual(len(report), 1)

        asset.write_bytes(launcher_asset(b"changed after review"))
        with self.assertRaisesRegex(ValueError, "changed since they were displayed"):
            inspect_staged_releases.remove_staged_release(version, report[0]["files"])
        self.assertTrue(asset.exists())

        report = inspect_staged_releases.inspect_staged_releases(version)
        removed = inspect_staged_releases.remove_staged_release(version, report[0]["files"])
        self.assertEqual(removed, 1)
        self.assertFalse(staged.exists())

        token = os.environ["CAFELAUNCHER_PUBLISH_TOKEN"]
        published_version = "2026.11.07.7"
        for kind, body in (("launcher", launcher_asset(b"published launcher")), ("graphics-module", b"published module")):
            status, _, _ = self.request(
                f"/v1/admin/releases/{published_version}/{kind}",
                method="PUT",
                body=body,
                token=token,
                content_type="application/octet-stream",
            )
            self.assertEqual(status, 201)
        status, _, _ = self.request(
            f"/v1/admin/releases/{published_version}/manifest",
            method="PUT",
            body=b"{}",
            token=token,
        )
        self.assertEqual(status, 201)
        with self.assertRaisesRegex(ValueError, "this release version is published"):
            inspect_staged_releases.remove_staged_release(published_version, [])

    def test_latest_release_uses_launcher_version_not_upload_time(self) -> None:
        token = os.environ["CAFELAUNCHER_PUBLISH_TOKEN"]

        def publish(version: str) -> None:
            for kind, body in (("launcher", launcher_asset(f"launcher {version}".encode())), ("graphics-module", f"module {version}".encode())):
                status, _, _ = self.request(
                    f"/v1/admin/releases/{version}/{kind}",
                    method="PUT",
                    body=body,
                    token=token,
                    content_type="application/octet-stream",
                )
                self.assertEqual(status, 201)
            status, _, _ = self.request(
                f"/v1/admin/releases/{version}/manifest",
                method="PUT",
                body=b"{}",
                token=token,
            )
            self.assertEqual(status, 201)

        publish("2026.11.07.9")
        publish("2026.11.07.2")
        status, _, body = self.request("/v1/releases/latest")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["version"], "2026.11.07.9")

        with patch.dict(
            os.environ,
            {
                "CAFELAUNCHER_GITHUB_REPO": "example/cafelauncher-data",
                "CAFELAUNCHER_GITHUB_BRANCH": "main",
                "CAFELAUNCHER_GITHUB_TOKEN": "github-token-" + "g" * 40,
            },
        ):
            _, files = github_mirror.build_snapshot()
        mirror_manifest = json.loads(files["mirror-manifest.json"])
        self.assertEqual(mirror_manifest["releases"]["latestVersion"], "2026.11.07.9")
        self.assertEqual(json.loads(files["releases/latest.json"])["version"], "2026.11.07.9")

    def test_optional_logihub_mirror_is_token_published_and_immutable(self) -> None:
        asset = b"logihub helper sample binary"
        path = "/v1/admin/tools/logihub/v0.1.0-c54d.1"
        status, _, _ = self.request(path, method="PUT", body=asset, content_type="application/octet-stream")
        self.assertEqual(status, 401)
        token = os.environ["CAFELAUNCHER_PUBLISH_TOKEN"]
        status, _, body = self.request(path, method="PUT", body=asset, token=token, content_type="application/octet-stream")
        self.assertEqual(status, 201)
        result = json.loads(body)
        self.assertEqual(result["sha256"], hashlib.sha256(asset).hexdigest())
        status, _, body = self.request("/v1/tools/logihub/latest")
        self.assertEqual(status, 200)
        latest = json.loads(body)
        self.assertEqual(latest["version"], "v0.1.0-c54d.1")
        status, _, body = self.request(latest["asset"]["url"])
        self.assertEqual(status, 200)
        self.assertEqual(body, asset)
        status, _, _ = self.request(path, method="PUT", body=b"replacement", token=token, content_type="application/octet-stream")
        self.assertEqual(status, 409)

        asset_path = Path(self.temp.name) / "tools" / "logihub" / "v0.1.0-c54d.1" / "asset.bin"
        asset_path.write_bytes(b"tampered helper")
        status, _, body = self.request(latest["asset"]["url"])
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "tool_asset_integrity")

    def test_release_download_rejects_asset_changed_after_publish(self) -> None:
        token = os.environ["CAFELAUNCHER_PUBLISH_TOKEN"]
        version = "2026.11.07.3"
        for kind, body in (("launcher", launcher_asset(b"verified launcher")), ("graphics-module", b"verified module")):
            status, _, _ = self.request(
                f"/v1/admin/releases/{version}/{kind}",
                method="PUT",
                body=body,
                token=token,
                content_type="application/octet-stream",
            )
            self.assertEqual(status, 201)
        status, _, _ = self.request(
            f"/v1/admin/releases/{version}/manifest",
            method="PUT",
            body=b"{}",
            token=token,
        )
        self.assertEqual(status, 201)

        asset_path = Path(self.temp.name) / "releases" / version / "launcher.bin"
        asset_path.write_bytes(b"tampered launcher")
        status, _, body = self.request(f"/v1/releases/{version}/launcher")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "release_asset_integrity")

    def test_publish_client_uploads_and_verifies_server_hashes(self) -> None:
        from argparse import Namespace

        token = os.environ["CAFELAUNCHER_PUBLISH_TOKEN"]
        with tempfile.TemporaryDirectory() as temp:
            launcher_path = Path(temp) / "cafe.bat"
            module_path = Path(temp) / "ApplyGraphics.ps1"
            tool_path = Path(temp) / "logihub.exe"
            launcher_path.write_bytes(launcher_asset(b"launcher test artifact"))
            module_path.write_bytes(b"graphics test artifact")
            tool_path.write_bytes(b"logihub test artifact")
            release_args = Namespace(
                version="2026.11.07.2",
                launcher=launcher_path,
                graphics_module=module_path,
                release_notes="Publisher integration test",
            )
            publish.publish_release(release_args, token, publish._base_url(self.base))
            status, _, body = self.request("/v1/releases/latest")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["version"], "2026.11.07.2")

            # Simulate the server committing the manifest/tool while the upload response is lost.
            original_put = publish._put

            def put_then_lose_response(url: str, token_value: str, body: bytes, content_type: str):
                result = original_put(url, token_value, body, content_type)
                if url.endswith("/manifest") or "/admin/tools/" in url:
                    raise RuntimeError("simulated lost upload response")
                return result

            retry_args = Namespace(
                version="2026.11.07.8",
                launcher=launcher_path,
                graphics_module=module_path,
                release_notes="Retry after lost response",
            )
            with patch("publish._put", side_effect=put_then_lose_response):
                publish.publish_release(retry_args, token, publish._base_url(self.base))
            publish.publish_release(retry_args, token, publish._base_url(self.base))
            retry_args.release_notes = "Different notes must not mutate an immutable release"
            with self.assertRaisesRegex(RuntimeError, "already published with different files or release notes"):
                publish.publish_release(retry_args, token, publish._base_url(self.base))

            tool_args = Namespace(tool="logihub", version="v0.1.0-test", asset=tool_path)
            with patch("publish._put", side_effect=put_then_lose_response):
                publish.publish_tool(tool_args, token, publish._base_url(self.base))
            publish.publish_tool(tool_args, token, publish._base_url(self.base))
            tool_path.write_bytes(b"different helper bytes")
            with self.assertRaisesRegex(RuntimeError, "already published with different bytes"):
                publish.publish_tool(tool_args, token, publish._base_url(self.base))
            status, _, body = self.request("/v1/tools/logihub/latest")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["version"], "v0.1.0-test")

    def test_mirror_snapshot_and_durable_retry_after_github_failure(self) -> None:
        sync_token = os.environ["CAFELAUNCHER_SYNC_TOKEN"]
        status, _, _ = self.put_bundle(0, token=sync_token)
        self.assertEqual(status, 200)
        publish_token = os.environ["CAFELAUNCHER_PUBLISH_TOKEN"]
        launcher, module, tool = launcher_asset(b"launcher mirror data"), b"graphics mirror data", b"helper mirror data"
        for kind, body in (("launcher", launcher), ("graphics-module", module)):
            status, _, _ = self.request(
                f"/v1/admin/releases/2026.11.07.9/{kind}",
                method="PUT",
                body=body,
                token=publish_token,
                content_type="application/octet-stream",
            )
            self.assertEqual(status, 201)
        status, _, _ = self.request(
            "/v1/admin/releases/2026.11.07.9/manifest",
            method="PUT",
            body=json.dumps({"releaseNotes": "mirror fixture"}).encode(),
            token=publish_token,
        )
        self.assertEqual(status, 201)
        status, _, _ = self.request(
            "/v1/admin/tools/logihub/v1.2.3",
            method="PUT",
            body=tool,
            token=publish_token,
            content_type="application/octet-stream",
        )
        self.assertEqual(status, 201)
        with patch.dict(
            os.environ,
            {
                "CAFELAUNCHER_GITHUB_REPO": "example/cafelauncher-data",
                "CAFELAUNCHER_GITHUB_BRANCH": "main",
                "CAFELAUNCHER_GITHUB_TOKEN": "github-token-" + "g" * 40,
            },
        ):
            generation, files = github_mirror.build_snapshot()
            self.assertEqual(files["cfg/fab.cfg"], b'sensitivity "1.2"\n')
            self.assertEqual(files["releases/2026.11.07.9/launcher.bin"], launcher)
            self.assertEqual(files["releases/2026.11.07.9/graphics-module.ps1"], module)
            self.assertEqual(files["tools/logihub/v1.2.3/asset.bin"], tool)
            mirror_manifest = json.loads(files["mirror-manifest.json"])
            self.assertEqual(mirror_manifest["generation"], generation)
            self.assertEqual(mirror_manifest["profiles"]["FabMode"]["revision"], 1)
            self.assertEqual(
                [entry["revision"] for entry in json.loads(files["history/graphics/FabMode.json"])["revisions"]],
                [1],
            )

            with patch.object(github_mirror.GitHubClient, "publish_snapshot", side_effect=github_mirror.MirrorError("simulated GitHub outage")):
                with self.assertRaises(github_mirror.MirrorError):
                    github_mirror.sync_once()
            with server._connect() as connection:
                failed = connection.execute("SELECT generation, mirrored_generation, last_error FROM github_mirror_state").fetchone()
            self.assertGreater(failed["generation"], failed["mirrored_generation"])
            self.assertIn("simulated GitHub outage", failed["last_error"])

            restarted_environment = os.environ.copy()
            restarted_environment["CAFELAUNCHER_DATA_DIR"] = self.temp.name
            status = subprocess.run(
                [sys.executable, github_mirror.__file__, "--status"],
                check=True,
                capture_output=True,
                text=True,
                env=restarted_environment,
            )
            persisted = json.loads(status.stdout)
            self.assertGreater(persisted["generation"], persisted["mirrored_generation"])
            self.assertIn("simulated GitHub outage", persisted["last_error"])

            status, _, _ = self.put_bundle(1, token=sync_token, cfg='sensitivity "1.3"\n')
            self.assertEqual(status, 200)

            def verify_restarted_snapshot(branch, new_generation, new_files):
                self.assertEqual(branch, "main")
                self.assertGreater(new_generation, persisted["generation"])
                restarted_manifest = json.loads(new_files["mirror-manifest.json"])
                self.assertEqual(restarted_manifest["profiles"]["FabMode"]["revision"], 2)
                self.assertEqual(new_files["cfg/fab.cfg"], b'sensitivity "1.3"\n')
                return "b" * 40

            with patch.object(github_mirror.GitHubClient, "publish_snapshot", side_effect=verify_restarted_snapshot):
                self.assertTrue(github_mirror.sync_once())
            with server._connect() as connection:
                recovered = connection.execute("SELECT generation, mirrored_generation, last_error FROM github_mirror_state").fetchone()
            self.assertEqual(recovered["generation"], recovered["mirrored_generation"])
            self.assertIsNone(recovered["last_error"])


if __name__ == "__main__":
    unittest.main()
