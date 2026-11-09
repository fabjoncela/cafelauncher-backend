#!/usr/bin/env python3
"""Publish launcher releases and mirrored helper artifacts to the backend API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import server


def _base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise ValueError("Backend URL is invalid.") from error
    local_http = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not local_http:
        raise ValueError("Backend URL must use HTTPS (HTTP is allowed only for localhost testing).")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Backend URL must be an origin URL without credentials, query, or fragment.")
    return value.rstrip("/")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _put(url: str, token: str, body: bytes, content_type: str) -> dict:
    request = Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        method="PUT",
    )
    try:
        opener = build_opener(_RejectRedirects)
        with opener.open(request, timeout=60) as response:
            response_body = response.read()
    except HTTPError as error:
        detail = error.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"Backend rejected upload with HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach backend: {error.reason}") from error
    try:
        return json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Backend returned a non-JSON upload response.") from error


def _get(url: str, maximum: int) -> tuple[int, bytes]:
    request = Request(url, headers={"Accept": "application/json, application/octet-stream"}, method="GET")
    try:
        opener = build_opener(_RejectRedirects)
        with opener.open(request, timeout=60) as response:
            body = response.read(maximum + 1)
            status = response.status
    except HTTPError as error:
        if error.code == 404:
            return 404, b""
        detail = error.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"Backend state check failed with HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach backend to verify existing publication: {error.reason}") from error
    if len(body) > maximum:
        raise RuntimeError("Backend state response exceeded its size limit.")
    return status, body


def _read_asset(path: Path, maximum: int, minimum: int = 1) -> tuple[bytes, str]:
    try:
        body = path.read_bytes()
    except OSError as error:
        raise ValueError(f"Could not read artifact {path}: {error}") from error
    if len(body) < minimum or len(body) > maximum:
        raise ValueError(f"Artifact {path} must contain between {minimum} and {maximum} bytes.")
    return body, hashlib.sha256(body).hexdigest()


def publish_release(args: argparse.Namespace, token: str, base: str) -> None:
    version = quote(args.version, safe=".-_")
    artifacts = (
        ("launcher", args.launcher, server.MAX_LAUNCHER_BYTES, "application/octet-stream", server.MIN_LAUNCHER_BYTES),
        ("graphics-module", args.graphics_module, server.MAX_MODULE_BYTES, "application/octet-stream", 1),
    )
    bodies: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for kind, path, maximum, _content_type, minimum in artifacts:
        body, digest = _read_asset(path, maximum, minimum)
        bodies[kind] = body
        hashes[kind] = digest
        sizes[kind] = len(body)

    def published_release_matches() -> bool:
        status, response_body = _get(f"{base}/v1/releases/{version}/manifest", 64 * 1024)
        if status == 404:
            return False
        if status != 200:
            raise RuntimeError(f"Backend returned unexpected HTTP {status} while checking the published release.")
        try:
            existing = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Backend returned an invalid existing release manifest.") from error
        if not isinstance(existing, dict) or not isinstance(existing.get("artifacts"), dict):
            raise RuntimeError("Backend returned an incomplete existing release manifest.")
        artifacts_value = existing["artifacts"]
        launcher = artifacts_value.get("launcher", {})
        module = artifacts_value.get("graphicsModule", {})
        if not isinstance(launcher, dict) or not isinstance(module, dict):
            raise RuntimeError("Backend returned invalid artifact metadata in the existing release manifest.")
        matches = (
            existing.get("version") == args.version
            and existing.get("releaseNotes") == args.release_notes
            and launcher.get("sha256") == hashes["launcher"]
            and launcher.get("size") == sizes["launcher"]
            and module.get("sha256") == hashes["graphics-module"]
            and module.get("size") == sizes["graphics-module"]
        )
        if not matches:
            raise RuntimeError(
                f"Release version {args.version} is already published with different files or release notes; use a new version."
            )
        return True

    if published_release_matches():
        print(f"CafeLauncher release {args.version} is already published and matches these files.")
        return

    try:
        for kind, _path, _maximum, content_type, _minimum in artifacts:
            result = _put(f"{base}/v1/admin/releases/{version}/{kind}", token, bodies[kind], content_type)
            if result.get("sha256") != hashes[kind] or result.get("size") != sizes[kind]:
                raise RuntimeError(f"Backend read-back metadata did not match the uploaded {kind}.")

        manifest_body = json.dumps({"releaseNotes": args.release_notes}, ensure_ascii=False).encode("utf-8")
        manifest = _put(f"{base}/v1/admin/releases/{version}/manifest", token, manifest_body, "application/json")
        for kind in hashes:
            artifact_key = "launcher" if kind == "launcher" else "graphicsModule"
            published = manifest.get("artifacts", {}).get(artifact_key, {})
            if published.get("sha256") != hashes[kind] or published.get("size") != sizes[kind]:
                raise RuntimeError(f"Published manifest metadata did not match the uploaded {kind}.")
    except RuntimeError:
        if published_release_matches():
            print(f"CafeLauncher release {args.version} is already published and matches these files.")
            return
        raise
    print(f"Published CafeLauncher release {args.version}.")


def publish_tool(args: argparse.Namespace, token: str, base: str) -> None:
    version = quote(args.version, safe=".-_")
    body, digest = _read_asset(args.asset, server.MAX_TOOL_BYTES)
    asset_url = f"{base}/v1/tools/{quote(args.tool, safe='')}/{version}"

    def published_tool_matches() -> bool:
        status, existing = _get(asset_url, server.MAX_TOOL_BYTES)
        if status == 404:
            return False
        if status != 200:
            raise RuntimeError(f"Backend returned unexpected HTTP {status} while checking the published tool.")
        if len(existing) != len(body) or hashlib.sha256(existing).hexdigest() != digest:
            raise RuntimeError(f"Tool version {args.version} is already published with different bytes; use a new version.")
        return True

    if published_tool_matches():
        print(f"{args.tool} {args.version} is already published and matches this file.")
        return
    try:
        result = _put(f"{base}/v1/admin/tools/{quote(args.tool, safe='')}/{version}", token, body, "application/octet-stream")
        if result.get("sha256") != digest or result.get("size") != len(body):
            raise RuntimeError("Backend read-back metadata did not match the uploaded tool artifact.")
    except RuntimeError:
        if published_tool_matches():
            print(f"{args.tool} {args.version} is already published and matches this file.")
            return
        raise
    print(f"Published {args.tool} {args.version} ({len(body)} bytes, sha256 {digest}).")


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish immutable artifacts to CafeLauncher Backend.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    release = subparsers.add_parser("release", help="Publish a launcher and graphics-module release")
    release.add_argument("--base-url", required=True)
    release.add_argument("--version", required=True)
    release.add_argument("--launcher", required=True, type=Path)
    release.add_argument("--graphics-module", required=True, type=Path)
    release.add_argument("--release-notes", default="")

    tool = subparsers.add_parser("tool", help="Mirror a helper binary such as logihub.exe")
    tool.add_argument("--base-url", required=True)
    tool.add_argument("--tool", required=True, choices=("logihub",))
    tool.add_argument("--version", required=True)
    tool.add_argument("--asset", required=True, type=Path)

    args = parser.parse_args()
    token = os.environ.get("CAFELAUNCHER_PUBLISH_TOKEN", "")
    if len(token) < 32:
        parser.error("CAFELAUNCHER_PUBLISH_TOKEN must be set to the publishing token.")
    try:
        base = _base_url(args.base_url)
        if args.command == "release":
            publish_release(args, token, base)
        else:
            publish_tool(args, token, base)
        return 0
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
