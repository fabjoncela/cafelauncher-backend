# CafeLauncher Backend Prototype

This is a standalone backend project in its own folder. It does not modify or import code into `ProjectCafe`.

The prototype provides anonymous read endpoints for profile bundles and release assets, a bearer-token-protected profile sync endpoint, graphics revision history, and a separate token-protected release publishing path. It uses Python's standard library and SQLite; no package installation is needed.

## Current status

- API prototype implemented in `server.py`.
- Validation rules mirrored from the current CafeLauncher source for profile fields, graphics values/policies, CFG sensitivity, CFG URL migration, and website URLs.
- `import_repository.py` validates and imports the six checked-in profile/graphics/CFG bundles into the local SQLite database. The existing profile CFG URLs are rewritten to `/v1/cfg/{file}` routes.
- Twelve standard-library unit and HTTP integration tests cover validation, import, CFG serving, backup, sync conflicts/history, publishing, immutable assets, and publisher URL checks.
- No CafeLauncher client integration has been made.
- Nothing has been deployed to the Ubuntu server or exposed with Tailscale Funnel.
- The API contract is provisional and must be wired into CafeLauncher before these endpoints affect café PCs.

## Run locally on Windows

Requires Python 3.10 or later.

Set two different random tokens in the current PowerShell session, then start the server. Keep the values private and do not add them to this folder:

```powershell
$env:CAFELAUNCHER_SYNC_TOKEN = '<random sync token, at least 32 characters>'
$env:CAFELAUNCHER_PUBLISH_TOKEN = '<different random publishing token, at least 32 characters>'
python .\server.py
```

By default the API listens only on `127.0.0.1:8090`. The SQLite database and release files are created under `data\`. To use another data directory, set `CAFELAUNCHER_DATA_DIR`. `CAFELAUNCHER_PORT` can change the port; binding is restricted to loopback so the service can sit behind Funnel or another HTTPS proxy. Startup requires two distinct tokens, each at least 32 characters. The threaded HTTP server caps concurrent workers and applies a 30-second socket timeout.

Check the local health endpoint:

```text
http://127.0.0.1:8090/v1/health
```

The server requires configured tokens at request time. It deliberately has no default development token.

## API contract

### Public reads

| Method and route | Response |
| --- | --- |
| `GET /v1/health` | Service status and prototype version. |
| `GET /v1/profiles/{name}` | Latest profile JSON. |
| `GET /v1/graphics/{name}` | Latest graphics preset JSON. |
| `GET /v1/cfg/{filename}` | Latest CFG as UTF-8 text, such as `fab.cfg`. |
| `GET /v1/graphics/{name}/history?limit=3` | Latest graphics snapshots and revision IDs. |
| `GET /v1/releases/latest` | Most recently published release manifest. |
| `GET /v1/releases/{version}/launcher` | Immutable launcher asset. |
| `GET /v1/releases/{version}/graphics-module` | Immutable graphics module asset. |
| `GET /v1/tools/logihub/latest` | Latest mirrored Logitech helper metadata. |
| `GET /v1/tools/logihub/{version}` | Immutable mirrored Logitech helper asset. |

Profile, graphics, and CFG reads include an `ETag` and `X-Profile-Revision`. Mutable reads use `Cache-Control: no-cache`.

### Profile sync

Send a full bundle using `PUT /v1/profiles/{name}/sync` with `Authorization: Bearer <sync-token>`:

```json
{
  "expectedRevision": 0,
  "profile": {
    "SchemaVersion": 1,
    "ProfileName": "Example"
  },
  "graphics": {
    "SchemaVersion": 1,
    "ProfileName": "Example",
    "Settings": {}
  },
  "cfg": "sensitivity \"1.2\"\n"
}
```

The example is only a shape example; CafeLauncher profiles and graphics presets have additional fields and validation. `expectedRevision: 0` creates the first revision. Later writes must send the current revision. A stale revision returns `409 Conflict` and the current revision number. A successful write returns a new revision and SHA-256 hashes for all three values.

Sync validation checks the launcher profile field types and ranges, supported push-to-talk key names, website URL shape, graphics setting ranges/enums and required values, display/refresh policies, and exactly one valid CFG `sensitivity` command in the range `(0, 20]`. It rejects unknown profile fields such as the retired `CS2Sensitivity` JSON field. Legacy raw GitHub CFG URLs are accepted only for the current profile-to-file mapping and normalized to backend routes. The launcher must still validate before applying values.

CFG downloads are limited to 2 MiB, matching the current launcher's CFG API limit. Mutable reads expose revision metadata and ETags.

### Release publishing

Upload each asset with the separate publishing token:

```text
PUT /v1/admin/releases/{version}/launcher
PUT /v1/admin/releases/{version}/graphics-module
PUT /v1/admin/releases/{version}/manifest
```

The manifest request is JSON and may contain a `releaseNotes` string. Both assets must be uploaded first. The server computes their sizes and SHA-256 hashes; clients retrieve these values from `GET /v1/releases/latest`. Published versions are immutable.

The optional helper mirror supports `PUT /v1/admin/tools/logihub/{version}` with the publishing token. It computes and returns the helper's size and SHA-256; anonymous clients can fetch the latest metadata and the immutable versioned asset. The backend is ready to store this binary, but no helper executable has been copied yet.

`publish.py` uploads a launcher/module release or helper binary and checks the server-returned hashes. It can be used later from a trusted local machine or a CI job; no GitHub Actions workflow has been changed.

Example commands for a future deployment:

```powershell
$env:CAFELAUNCHER_PUBLISH_TOKEN = '<publishing token>'
python .\publish.py release --base-url 'https://<server>.ts.net' --version '2026.11.07.1' --launcher 'C:\path\cafe.bat' --graphics-module 'C:\path\ApplyGraphics.ps1'
python .\publish.py tool --base-url 'https://<server>.ts.net' --tool logihub --version 'v0.1.0-c54d.1' --asset 'C:\path\logihub.exe'
```

The tool artifact is optional. The second command mirrors the existing Logitech helper release; it does not fetch the upstream binary by itself.

## Import existing profiles

The import tool reads the selected repository checkout and writes only to this backend's ignored `data/` directory. It does not edit source files. It validates all six profile JSON files, six graphics presets, and six CFGs before making a single SQLite transaction. It refuses to import if the database already contains profile revisions.

Dry-run:

```powershell
python .\import_repository.py --repo-root 'C:\Users\Admin\Desktop\ProjectCafe' --dry-run
```

Import:

```powershell
python .\import_repository.py --repo-root 'C:\Users\Admin\Desktop\ProjectCafe'
```

The initial local import has been performed. It created revision 1 for FabMode, ArliMode, BrileMode, ArmlMode, OliMode, and Provmode. The imported checkout's `profiles/`, `graphics/`, and CFG files match the local `origin/main` asset paths at import time; later profile/graphics changes must be imported or synced separately. No old GitHub commit history is imported.

## Backup

Create each backup in a new or empty private directory. The script uses SQLite's online backup API so committed WAL data is included, then copies the release and helper asset directories:

```powershell
python .\backup.py 'D:\CafeLauncherBackups\2026-11-07'
```

Pause publishing while the backup runs to keep the database and asset snapshot aligned. Restore by stopping the service, preserving the current data directory, copying the backed-up database and asset directories into the configured data directory, then starting the service and checking `/v1/health` and the latest profile/release endpoints. Keep backups off the public web root and do not include tokens.

## Tailscale Funnel deployment shape

For a later Ubuntu deployment, run this service bound to loopback and expose only its local port through Funnel. CaféLauncher clients will use the resulting HTTPS `*.ts.net` URL; the clients will not need to join the tailnet. Do not bind the application to `0.0.0.0` when relying on Funnel as the public entry point.

Use a systemd service and keep the two tokens in a root-readable environment file outside the project folder. Back up the SQLite database plus `data/releases/` and `data/tools/` together with `backup.py`. Deployment and Funnel configuration have not been performed.

## Before connecting CafeLauncher

1. Keep the mirrored rules synchronized with future CafeLauncher changes. The source project remains the authority for profile and graphics behavior.
2. Decide how old GitHub graphics history should be treated; the prototype starts a new revision history.
3. Have CafeLauncher build its CFG URL from the new API base URL instead of trusting old `raw.githubusercontent.com` profile URLs.
4. Implement and verify the client adapter behind an opt-in backend setting before switching defaults. This includes the separate direct GitHub call in `graphics/ApplyGraphics.ps1`, token prompt changes, revision conflict handling, and removal of GitHub quota/commit ancestry UI.
5. Preserve local pending data, read-back verification, hash checks, conflict handling, and offline behavior.
6. Add the self-hosted publishing command to the chosen release workflow and publish the actual launcher/module/helper artifacts.
7. Decide whether to mirror the separate Logitech helper GitHub release or leave that dependency outside the migration.
8. Deploy to Ubuntu, set service secrets, expose through Funnel, and verify backup/restore before directing clients to it.
9. Only after migration, remove GitHub runtime calls. GitHub source hosting and CI remain separate unless those are also explicitly migrated.

## Security notes

- Public read URLs expose the published profile, graphics, CFG, and release data to anyone who knows or discovers the URL.
- A bearer token entered on a shared PC may be recoverable from that PC. Do not embed the sync or publish token in the launcher.
- The app logs request method/path/status but not authorization headers or request bodies.
- The service should remain behind HTTPS in deployment; Funnel provides the public HTTPS entry point. The local prototype listens on loopback by default.
- Keep backups private. Do not commit `data/`, `.env`, or any real token.
