# CafeLauncher Backend Prototype

This is a standalone backend project in its own folder. It does not modify or import code into `ProjectCafe`.

The prototype provides anonymous read endpoints for profile bundles and release assets, a bearer-token-protected profile sync endpoint, graphics revision history, and a separate token-protected release publishing path. It uses Python's standard library and SQLite; no package installation is needed.

## Current status

- API prototype implemented in `server.py`.
- Validation rules mirrored from the current CafeLauncher source for profile fields, graphics values/policies, CFG sensitivity, CFG URL migration, and website URLs.
- `import_repository.py` validates and imports the six checked-in profile/graphics/CFG bundles into the local SQLite database. The existing profile CFG URLs are rewritten to `/v1/cfg/{file}` routes.
- Twenty standard-library unit and HTTP integration tests cover validation, import, CFG serving, backup, sync conflicts/history, publishing, asset integrity, and GitHub mirror ref-conflict/restart recovery.
- `github_mirror.py` publishes an atomic public GitHub snapshot and keeps a durable SQLite generation marker for restart-safe retries.
- No CafeLauncher client integration has been made.
- The `/v1` API is not wire-compatible with GitHub REST. CafeLauncher still calls GitHub Contents, commits/compare/history, raw CFG/module, and updater endpoints directly; a client adapter and opt-in migration are required before this backend can replace GitHub.
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
| `GET /v1/graphics/{name}/history?limit=3` | Current distinct graphics snapshot plus up to three older graphics-only versions. |
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

The example is only a shape example; CafeLauncher profiles and graphics presets have additional fields and validation. `expectedRevision: 0` creates the first revision. Later writes must send the current revision. A stale revision returns `409 Conflict` and the current revision number. A successful write returns the revision and SHA-256 hashes for all three values. An identical bundle returns the existing revision with `unchanged: true`; it does not add history or queue another GitHub mirror generation.

Sync validation checks the launcher profile field types and ranges, supported push-to-talk key names, website URL shape, graphics setting ranges/enums and required values, refresh policy, and exactly one valid CFG `sensitivity` command in the range `(0, 20]`. Graphics `DisplayPolicy` is canonicalized to `ApplyPreset`, matching CafeLauncher's `ConvertTo-CafeGraphicsPreset`. It rejects unknown profile fields such as the retired `CS2Sensitivity` JSON field. Legacy raw GitHub CFG URLs are accepted only for the current profile-to-file mapping and normalized to backend routes. The launcher must still validate before applying values.

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

Launcher release versions must use CafeLauncher's four-part dotted `CAFE_RELEASE` format (`YYYY.M.D.N`). The `/v1/releases/latest` route and GitHub mirror choose the numerically highest version even if an older release is uploaded later. Helper versions keep their independent tag format.

Launcher release uploads must be between 10,000 and 1,000,000 bytes, matching the current CafeLauncher updater's metadata validation. The graphics module is limited to 8 MiB.

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

Use separate `systemd` services for the API and mirror worker, with secrets in root-readable environment files outside the project folder. The step-by-step Ubuntu checklist is in [`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md). Back up the SQLite database plus `data/releases/` and `data/tools/` together with `backup.py`. Deployment and Funnel configuration have not been performed.

## GitHub fallback mirror

The backend can mirror its public data to a **separate public, data-only GitHub repository**. Initialize the configured branch (default `main`) with a README commit; the GitHub Git API cannot create a branch in an empty repository ([GitHub refs API](https://docs.github.com/en/rest/git/refs)). Create a fine-grained GitHub token scoped only to that repository with Contents: write permission. Store it on the Ubuntu server in `/etc/cafelauncher-github-mirror.env` (mode `0600`), separate from the API sync and publishing tokens:

```text
CAFELAUNCHER_GITHUB_REPO=owner/cafelauncher-data
CAFELAUNCHER_GITHUB_BRANCH=main
CAFELAUNCHER_GITHUB_TOKEN=<fine-grained-token>
```

The backend records a new mirror generation in the same SQLite transaction as every changed profile bundle, release publication, helper publication, and repository import. Identical profile bundles do not create a revision or mirror job. Graphics history records only revisions where the graphics JSON changed, matching CafeLauncher's GitHub path history; profile-only or CFG-only syncs do not appear as graphics versions. Run `python3 github_mirror.py --watch` as a separate persistent service. It checks for new generations every 10 seconds, retries failures with increasing delays capped at 20 minutes, and resumes pending generations after process or machine restart. `python3 github_mirror.py --once` attempts one update; `--status` shows whether the last generation reached GitHub. The example systemd unit is `deploy/cafelauncher-github-mirror.service`; adjust its paths/user and use the same `CAFELAUNCHER_DATA_DIR` as the API service before installing it.

Each successful update is one GitHub commit containing a `mirror-manifest.json`, current profile/graphics/CFG files, recent graphics history, published releases, and mirrored helper assets. The manifest maps logical API resources to raw GitHub paths and includes revisions and SHA-256 values for a future CafeLauncher fallback adapter. Existing binary limits stay below GitHub's 100 MiB per-blob limit, although frequent large-file updates increase mirror repository history and storage. [GitHub Git blobs API](https://docs.github.com/en/rest/git/blobs), [GitHub repository limits](https://docs.github.com/en/repositories/creating-and-managing-repositories/repository-limits)

If GitHub is unreachable, the saved backend data and dirty generation remain in SQLite; the worker retries later. If the machine hosting the backend is powered off, no new API saves can reach it and its worker cannot run. CafeLauncher must keep unsent edits in its local pending queue and retry them when the backend returns. The mirror currently prepares fallback data only; CafeLauncher read failover and pending-write integration have not been implemented.

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
- The GitHub mirror token belongs only on the server. The café client needs no GitHub credential for anonymous fallback reads.
- The app logs request method/path/status but not authorization headers or request bodies.
- The service should remain behind HTTPS in deployment; Funnel provides the public HTTPS entry point. The local prototype listens on loopback by default.
- Keep backups private. Do not commit `data/`, `.env`, or any real token.
