#!/usr/bin/env python3
"""Durably mirror the latest backend snapshot into a public GitHub repository."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

import server
from validation import PROFILE_CFG_FILES


REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
MAX_BLOB_BYTES = 100 * 1024 * 1024
API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"


class MirrorError(RuntimeError):
    pass


class GitHubHTTPError(MirrorError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()


def _github_config() -> tuple[str, str, str]:
    repo = os.environ.get("CAFELAUNCHER_GITHUB_REPO", "").strip()
    branch = os.environ.get("CAFELAUNCHER_GITHUB_BRANCH", "main").strip()
    token = os.environ.get("CAFELAUNCHER_GITHUB_TOKEN", "")
    if not REPO_PATTERN.fullmatch(repo):
        raise MirrorError("CAFELAUNCHER_GITHUB_REPO must be owner/repository.")
    if not BRANCH_PATTERN.fullmatch(branch) or branch in (".", ".."):
        raise MirrorError("CAFELAUNCHER_GITHUB_BRANCH must be a simple branch name.")
    if len(token) < 32:
        raise MirrorError("CAFELAUNCHER_GITHUB_TOKEN must be set to a token with mirror-repository Contents write access.")
    return repo, branch, token


def _asset_file(data: bytes, expected_size: int, expected_sha256: str, label: str) -> bytes:
    if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise MirrorError(f"Stored {label} does not match its database size/hash; refusing to mirror inconsistent data.")
    if len(data) > MAX_BLOB_BYTES:
        raise MirrorError(f"Stored {label} exceeds GitHub's 100 MiB Git blob limit.")
    return data


def build_snapshot() -> tuple[int, dict[str, bytes]]:
    """Read one consistent database generation and its immutable asset files."""
    server._initialize()
    files: dict[str, bytes] = {}
    with server._connect() as connection:
        connection.execute("BEGIN")
        state = connection.execute("SELECT generation FROM github_mirror_state WHERE singleton = 1").fetchone()
        generation = int(state["generation"])
        profile_rows = connection.execute(
            "SELECT p.profile_name, p.revision, p.created_utc, p.profile_json, p.graphics_json, p.cfg_text "
            "FROM profile_revisions p JOIN ("
            "SELECT profile_name, MAX(revision) AS revision FROM profile_revisions GROUP BY profile_name"
            ") latest ON latest.profile_name = p.profile_name AND latest.revision = p.revision "
            "ORDER BY p.profile_name"
        ).fetchall()
        graphics_history_rows = connection.execute(
            "WITH graphics_changes AS ("
            " SELECT profile_name, revision, created_utc, graphics_json, "
            " LAG(graphics_json) OVER (PARTITION BY profile_name ORDER BY revision) AS previous_graphics "
            " FROM profile_revisions"
            "), ranked_changes AS ("
            " SELECT profile_name, revision, created_utc, graphics_json, "
            " ROW_NUMBER() OVER (PARTITION BY profile_name ORDER BY revision DESC) AS history_rank "
            " FROM graphics_changes "
            " WHERE previous_graphics IS NULL OR graphics_json != previous_graphics"
            ") SELECT profile_name, revision, created_utc, graphics_json FROM ranked_changes "
            "WHERE history_rank <= 20 ORDER BY profile_name, revision DESC"
        ).fetchall()
        release_rows = connection.execute(
            "SELECT version, published_utc, manifest_json, launcher_sha256, launcher_size, module_sha256, module_size "
            "FROM releases"
        ).fetchall()
        tool_rows = connection.execute(
            "SELECT tool_name, version, published_utc, sha256, size FROM tool_assets ORDER BY tool_name, published_utc, rowid"
        ).fetchall()
        connection.commit()

    profile_manifest: dict[str, object] = {}
    for row in profile_rows:
        name = row["profile_name"]
        profile_path = f"profiles/{name}.json"
        graphics_path = f"graphics/{name}.json"
        cfg_path = f"cfg/{PROFILE_CFG_FILES[name]}"
        history_path = f"history/graphics/{name}.json"
        profile_bytes = row["profile_json"].encode("utf-8")
        graphics_bytes = row["graphics_json"].encode("utf-8")
        cfg_bytes = row["cfg_text"].encode("utf-8")
        history = [
            {
                "revision": item["revision"],
                "createdUtc": item["created_utc"],
                "graphics": json.loads(item["graphics_json"]),
            }
            for item in graphics_history_rows
            if item["profile_name"] == name
        ]
        history_bytes = _json_bytes({"profile": name, "revisions": history})
        if not history:
            raise MirrorError(f"Profile {name} has no graphics history to mirror.")
        graphics_revision = int(history[0]["revision"])
        for path, data in ((profile_path, profile_bytes), (graphics_path, graphics_bytes), (cfg_path, cfg_bytes), (history_path, history_bytes)):
            files[path] = data
        profile_manifest[name] = {
            "revision": row["revision"],
            "graphicsRevision": graphics_revision,
            "createdUtc": row["created_utc"],
            "files": {"profile": profile_path, "graphics": graphics_path, "cfg": cfg_path, "graphicsHistory": history_path},
            "sha256": {
                "profile": hashlib.sha256(profile_bytes).hexdigest(),
                "graphics": hashlib.sha256(graphics_bytes).hexdigest(),
                "cfg": hashlib.sha256(cfg_bytes).hexdigest(),
                "graphicsHistory": hashlib.sha256(history_bytes).hexdigest(),
            },
        }

    release_rows = sorted(release_rows, key=lambda row: server._launcher_version_key(row["version"]))
    releases: dict[str, object] = {}
    for row in release_rows:
        version = row["version"]
        root = f"releases/{version}"
        launcher_path = server._data_dir() / "releases" / version / "launcher.bin"
        module_path = server._data_dir() / "releases" / version / "graphics-module.ps1"
        try:
            launcher = _asset_file(launcher_path.read_bytes(), row["launcher_size"], row["launcher_sha256"], f"launcher release {version}")
            module = _asset_file(module_path.read_bytes(), row["module_size"], row["module_sha256"], f"graphics module {version}")
            stored_manifest = json.loads(row["manifest_json"])
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise MirrorError(f"Could not read release {version}: {error}") from error
        launcher_path_name = f"{root}/launcher.bin"
        module_path_name = f"{root}/graphics-module.ps1"
        manifest_path_name = f"{root}/manifest.json"
        files[launcher_path_name] = launcher
        files[module_path_name] = module
        files[manifest_path_name] = row["manifest_json"].encode("utf-8")
        releases[version] = {
            "publishedUtc": row["published_utc"],
            "manifest": manifest_path_name,
            "launcher": launcher_path_name,
            "graphicsModule": module_path_name,
            "sha256": {"launcher": row["launcher_sha256"], "graphicsModule": row["module_sha256"]},
            "size": {"launcher": row["launcher_size"], "graphicsModule": row["module_size"]},
        }

    tools: dict[str, object] = {}
    for row in tool_rows:
        name, version = row["tool_name"], row["version"]
        path = server._data_dir() / "tools" / name / version / "asset.bin"
        try:
            asset = _asset_file(path.read_bytes(), row["size"], row["sha256"], f"{name} {version}")
        except OSError as error:
            raise MirrorError(f"Could not read helper {name} {version}: {error}") from error
        relative_path = f"tools/{name}/{version}/asset.bin"
        files[relative_path] = asset
        tools.setdefault(name, {})
        tools[name][version] = {
            "publishedUtc": row["published_utc"],
            "asset": relative_path,
            "size": row["size"],
            "sha256": row["sha256"],
        }

    for tool_name, versions in tools.items():
        latest_version = list(versions)[-1]
        latest_path = f"tools/{tool_name}/latest.json"
        files[latest_path] = _json_bytes({"tool": tool_name, "version": latest_version, **versions[latest_version]})
        tools[tool_name] = {"latestVersion": latest_version, "latest": latest_path, "versions": versions}

    latest_release_version = release_rows[-1]["version"] if release_rows else None
    latest_release_path = f"releases/latest.json" if latest_release_version else None
    if latest_release_version:
        files[latest_release_path] = release_rows[-1]["manifest_json"].encode("utf-8")

    repo, branch, _ = _github_config()
    manifest = {
        "schemaVersion": 1,
        "generation": generation,
        "repository": repo,
        "branch": branch,
        "profiles": profile_manifest,
        "releases": {"latestVersion": latest_release_version, "latest": latest_release_path, "versions": releases},
        "tools": tools,
    }
    files["mirror-manifest.json"] = _json_bytes(manifest)
    for path, data in files.items():
        if len(data) > MAX_BLOB_BYTES:
            raise MirrorError(f"Mirror file {path} exceeds GitHub's 100 MiB Git blob limit.")
    return generation, files


class GitHubClient:
    def __init__(self, repo: str, token: str):
        self.owner, self.repository = repo.split("/", 1)
        self.token = token
        self.opener = build_opener(_RejectRedirects)

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = _json_bytes(payload) if payload is not None else None
        request = Request(
            API_ROOT + f"/repos/{quote(self.owner, safe='')}/{quote(self.repository, safe='')}" + path,
            data=body,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "CafeLauncher-Backend-Mirror",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                response_data = response.read(4 * 1024 * 1024 + 1)
        except HTTPError as error:
            detail = error.read(2048).decode("utf-8", errors="replace")
            detail = detail.replace(self.token, "[redacted]")
            raise GitHubHTTPError(error.code, f"GitHub API returned HTTP {error.code}: {detail[:1000]}") from error
        except URLError as error:
            raise MirrorError(f"Could not reach GitHub API: {error.reason}") from error
        if len(response_data) > 4 * 1024 * 1024:
            raise MirrorError("GitHub API response exceeded the 4 MiB safety limit.")
        if not response_data:
            return {}
        try:
            result = json.loads(response_data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MirrorError("GitHub API returned invalid JSON.") from error
        if not isinstance(result, dict):
            raise MirrorError("GitHub API returned an unexpected response shape.")
        return result

    def current_tree(self, branch: str) -> tuple[str | None, str | None, dict[str, str]]:
        ref_path = f"/git/ref/heads/{quote(branch, safe='')}"
        try:
            ref = self.request("GET", ref_path)
        except GitHubHTTPError as error:
            if error.status == 404:
                return None, None, {}
            raise
        commit_sha = ref.get("object", {}).get("sha")
        if not isinstance(commit_sha, str):
            raise MirrorError("GitHub branch ref did not include a commit SHA.")
        commit = self.request("GET", f"/git/commits/{commit_sha}")
        tree_sha = commit.get("tree", {}).get("sha")
        if not isinstance(tree_sha, str):
            raise MirrorError("GitHub commit did not include a tree SHA.")
        tree = self.request("GET", f"/git/trees/{tree_sha}?recursive=1")
        if tree.get("truncated"):
            raise MirrorError("GitHub mirror tree is truncated; refusing an incomplete update.")
        entries = {
            item["path"]: item["sha"]
            for item in tree.get("tree", [])
            if isinstance(item, dict) and item.get("type") == "blob" and isinstance(item.get("path"), str) and isinstance(item.get("sha"), str)
        }
        return commit_sha, tree_sha, entries

    def read_json_blob(self, blob_sha: str) -> dict | None:
        result = self.request("GET", f"/git/blobs/{blob_sha}")
        content = result.get("content")
        if result.get("encoding") != "base64" or not isinstance(content, str):
            raise MirrorError("GitHub mirror manifest blob was not base64 JSON.")
        try:
            parsed = json.loads(base64.b64decode(content).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MirrorError("GitHub mirror manifest could not be parsed.") from error
        return parsed if isinstance(parsed, dict) else None

    def publish_snapshot(self, branch: str, generation: int, files: dict[str, bytes]) -> str:
        for attempt in range(1, 4):
            parent_sha, base_tree_sha, remote_tree = self.current_tree(branch)
            if not parent_sha or not base_tree_sha:
                raise MirrorError("The configured GitHub branch must already exist; create the repository with an initial README commit first.")
            remote_manifest = None
            manifest_sha = remote_tree.get("mirror-manifest.json")
            if manifest_sha:
                remote_manifest = self.read_json_blob(manifest_sha)
                remote_generation = remote_manifest.get("generation") if remote_manifest else None
                if type(remote_generation) is int and remote_generation > generation:
                    raise MirrorError(
                        f"GitHub mirror generation {remote_generation} is ahead of local generation {generation}; refusing to overwrite it."
                    )

            updates = []
            for path, content in sorted(files.items()):
                blob_sha = _git_blob_sha(content)
                if remote_tree.get(path) == blob_sha:
                    continue
                created = self.request(
                    "POST",
                    "/git/blobs",
                    {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
                )
                created_sha = created.get("sha")
                if created_sha != blob_sha:
                    raise MirrorError(f"GitHub returned an unexpected Git blob SHA for {path}.")
                updates.append({"path": path, "mode": "100644", "type": "blob", "sha": created_sha})

            if not updates:
                return parent_sha or ""
            tree_payload: dict[str, object] = {"tree": updates}
            tree_payload["base_tree"] = base_tree_sha
            new_tree = self.request("POST", "/git/trees", tree_payload)
            new_tree_sha = new_tree.get("sha")
            if not isinstance(new_tree_sha, str):
                raise MirrorError("GitHub did not return the created tree SHA.")
            commit = self.request(
                "POST",
                "/git/commits",
                {
                    "message": f"Mirror CafeLauncher backend generation {generation}",
                    "tree": new_tree_sha,
                    "parents": [parent_sha],
                },
            )
            new_commit_sha = commit.get("sha")
            if not isinstance(new_commit_sha, str):
                raise MirrorError("GitHub did not return the created commit SHA.")
            try:
                self.request("PATCH", f"/git/refs/heads/{quote(branch, safe='')}", {"sha": new_commit_sha, "force": False})
                return new_commit_sha
            except GitHubHTTPError as error:
                if error.status not in (409, 422) or attempt == 3:
                    raise
        raise MirrorError("Could not update the GitHub mirror ref after concurrent updates.")


def _state() -> dict:
    server._initialize()
    with server._connect() as connection:
        return dict(connection.execute("SELECT * FROM github_mirror_state WHERE singleton = 1").fetchone())


def _record_success(generation: int) -> None:
    completed_utc = _utc_now()
    with server.DB_LOCK, server._connect() as connection:
        connection.execute(
            "UPDATE github_mirror_state SET mirrored_generation = MAX(mirrored_generation, ?), last_attempt_utc = ?, last_success_utc = ?, last_error = NULL WHERE singleton = 1",
            (generation, completed_utc, completed_utc),
        )


def _record_failure(error: Exception) -> None:
    message = str(error).replace(os.environ.get("CAFELAUNCHER_GITHUB_TOKEN", "\0"), "[redacted]")[:1000]
    with server.DB_LOCK, server._connect() as connection:
        connection.execute(
            "UPDATE github_mirror_state SET last_attempt_utc = ?, last_error = ? WHERE singleton = 1",
            (_utc_now(), message),
        )


def sync_once() -> bool:
    """Publish pending state. Return False when the current generation is already mirrored."""
    repo, branch, token = _github_config()
    state = _state()
    generation = int(state["generation"])
    if generation <= int(state["mirrored_generation"]):
        return False
    try:
        snapshot_generation, files = build_snapshot()
        if snapshot_generation != generation:
            generation = snapshot_generation
        commit_sha = GitHubClient(repo, token).publish_snapshot(branch, generation, files)
        _record_success(generation)
    except Exception as error:
        _record_failure(error)
        if isinstance(error, MirrorError):
            raise
        raise MirrorError(str(error)) from error
    print(f"Mirrored backend generation {generation} to {repo}@{branch} ({commit_sha or 'already current'}).")
    return True


def _print_status() -> None:
    state = _state()
    print(json.dumps({key: state[key] for key in ("generation", "mirrored_generation", "last_attempt_utc", "last_success_utc", "last_error")}, indent=2))


def _watch(poll_seconds: int) -> int:
    failures = 0
    while True:
        state = _state()
        if state["generation"] > state["mirrored_generation"]:
            try:
                sync_once()
                failures = 0
                time.sleep(poll_seconds)
            except Exception as error:
                failures += 1
                delay = min(poll_seconds * (2 ** min(failures, 10)), 20 * 60)
                print(f"GitHub mirror retry in {delay}s: {error}", file=sys.stderr, flush=True)
                time.sleep(delay)
        else:
            failures = 0
            time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Retryably mirror the backend's latest public data to GitHub.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Attempt one pending mirror update and exit")
    mode.add_argument("--watch", action="store_true", help="Poll for changes and retry failures until stopped")
    mode.add_argument("--status", action="store_true", help="Print the durable local mirror queue status")
    parser.add_argument("--poll-seconds", type=int, default=10, help="Change-detection interval in watch mode (default: 10)")
    args = parser.parse_args()
    if args.status:
        _print_status()
        return 0
    if args.poll_seconds < 1 or args.poll_seconds > 3600:
        parser.error("--poll-seconds must be between 1 and 3600.")
    try:
        if args.watch:
            return _watch(args.poll_seconds)
        sync_once()
        return 0
    except Exception as error:
        print(f"GitHub mirror failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
