# CafeLauncher Backend

This is a standalone backend project in its own folder. It does not modify or import code into `ProjectCafe`.

The service provides anonymous read endpoints for profile bundles and release assets, a bearer-token-protected atomic profile sync endpoint, complete profile-bundle revision history, and a separate token-protected release publishing path. It uses Python's standard library and SQLite; no package installation is needed.

## Current status

- API implementation in `server.py`.
- Validation rules mirrored from the current CafeLauncher source for profile fields, graphics values/policies, CFG sensitivity, CFG URL migration, and website URLs.
- `import_repository.py` validates and imports the six checked-in profile/graphics/CFG bundles into the local SQLite database. The existing profile CFG URLs are rewritten to `/v1/cfg/{file}` routes.
- Twenty-five standard-library unit and HTTP integration tests cover validation, import, CFG serving, backup, sync conflicts/history, publishing retries, staged-release cleanup, asset integrity, and GitHub mirror ref-conflict/restart recovery.
- `github_mirror.py` publishes an atomic public GitHub snapshot and keeps a durable SQLite generation marker for restart-safe retries.
- CafeLauncher uses this API as its primary profile, graphics, CFG, and graphics-history source. When the API is unavailable, the launcher can read the public GitHub mirror; pending client edits remain local until the API returns.
- The backend's root page is a responsive status dashboard with API health, profile count, latest profile save, mirror generation and last successful sync with relative age, backend-hosted releases, and deployed commit. It includes a copyable diagnostics summary, notes that the current launcher updater uses `Cs-script` on GitHub, and links to API docs, health JSON, the public mirror manifest, and the read-only profile viewer at `/viewer`.
- The backend is deployed on the Ubuntu server as two `systemd` services, with SQLite/assets under `/var/lib/cafelauncher-backend` and the API bound to `127.0.0.1:8090`. Its public HTTPS path is provided by the existing Tailscale Funnel route; see [`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md).
- `.github/workflows/backend-ci-deploy.yml` runs tests on Python 3.10 and 3.14, then deploys successful `main` pushes through the dedicated server runner.
- Backend CI checks viewer metadata against the current CafeLauncher source. CafeLauncher CI runs the same parity check when profile, graphics, or launcher schema files change. Launcher-supported fields and profiles must exist in the backend; the backend may add metadata before the matching launcher feature merges.
- Keep the API validation rules aligned with the deployed CafeLauncher client; profile writes require a backend sync token and use revision checks.

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

The server requires two different tokens at startup: one for profile sync and one for artifact publishing. Each must be at least 32 characters; there are no default development tokens. Public read routes do not require a bearer token. Sync and publishing routes require the matching bearer token.

## API contract

### Public reads

The browser viewer at `/viewer` presents current profile values, graphics settings, and CFG text. Search filters current setting rows and CFG lines; profile comparison loads a consistent backend snapshot and displays only differences. Its “Profile saves” history lists every saved bundle revision and lazily displays the profile, graphics, and CFG values from that same save, with an option to filter unchanged profile/graphics values. It is read-only and requests the `/v1` endpoints listed below from the backend host.

| Method and route | Response |
| --- | --- |
| `GET /v1/health` | Service status and application version; returns `503` if SQLite storage is unavailable. |
| `GET /v1/viewer-schema` | Profile/CFG choices, viewer sections, field labels/groups, and enum labels used by `/viewer`. |
| `GET /v1/profiles/{name}` | Latest profile JSON. |
| `GET /v1/graphics/{name}` | Latest graphics preset JSON. |
| `GET /v1/cfg/{filename}` | Latest CFG as UTF-8 text, such as `fab.cfg`. |
| `GET /v1/graphics/{name}/history?limit=3` | Current distinct graphics snapshot plus up to three older graphics-only versions. |
| `GET /v1/profiles/{name}/history?limit=20&beforeRevision=...` | Revision dates for profile saves, newest first. Optional cursor pages through older revisions. |
| `GET /v1/profiles/{name}/history/{revision}` | Complete saved profile, graphics, and CFG bundle for one revision. |
| `GET /v1/releases/latest` | Most recently published release manifest. |
| `GET /v1/releases/{version}/manifest` | Manifest for one exact published release version; used by the publisher to recover safely from an ambiguous retry. |
| `GET /v1/releases/{version}/launcher` | Immutable launcher asset. |
| `GET /v1/releases/{version}/graphics-module` | Immutable graphics module asset. |
| `GET /v1/tools/logihub/latest` | Latest mirrored Logitech helper metadata. |
| `GET /v1/tools/logihub/{version}` | Immutable mirrored Logitech helper asset. |

Profile, graphics, and CFG reads include an `ETag` and `X-Profile-Revision`. Mutable reads use `Cache-Control: no-cache`.

The viewer fetches `/v1/viewer-schema` before loading profile data. The backend derives profile choices from its profile-to-CFG registry and serves display metadata alongside the API contract. Settings fields are rendered from API values and schema sections; field groups, friendly labels, and known enum values come from that metadata. If a launcher field or supported enum changes, update the backend validation and viewer metadata together.

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

The manifest request is JSON and may contain a `releaseNotes` string. Both assets must be uploaded first. The server computes their sizes and SHA-256 hashes; clients retrieve these values from `GET /v1/releases/latest`. A staged asset cannot be replaced by different bytes, even before its manifest is published; identical upload retries are allowed. Use a new version after abandoning a partially staged release.

The optional helper mirror supports `PUT /v1/admin/tools/logihub/{version}` with the publishing token. It computes and returns the helper's size and SHA-256; anonymous clients can fetch the latest metadata and the immutable versioned asset. The public mirror currently has no helper version published.

Launcher release versions must use CafeLauncher's four-part dotted `CAFE_RELEASE` format (`YYYY.M.D.N`) and a real calendar date. The `/v1/releases/latest` route and GitHub mirror choose the numerically highest version even if an older release is uploaded later. Helper versions keep their independent tag format.

Launcher release uploads must be between 10,000 and 1,000,000 bytes, matching the current CafeLauncher updater's metadata validation. The graphics module is limited to 8 MiB.

`publish.py` uploads a launcher/module release or helper binary and checks the server-returned hashes. It can be used from a trusted local machine or a separate release workflow; the backend CI workflow does not publish launcher artifacts.

Manual publishing examples. This publisher is separate from the active CafeLauncher updater, which continues to use the Cs-script GitHub and Google Drive distribution paths:

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

Create each backup in a new or empty private directory outside the live backend data directory. The script uses SQLite's online backup API so committed WAL data is included, then copies the release and helper asset directories:

```powershell
python .\backup.py 'D:\CafeLauncherBackups\2026-11-07'
```

Pause publishing while the backup runs to keep the database and asset snapshot aligned. Restore by stopping the service, preserving the current data directory, copying the backed-up database and asset directories into the configured data directory, then starting the service and checking `/v1/health` and the latest profile/release endpoints. Keep backups off the public web root and do not include tokens.

Incomplete launcher uploads can leave files under `data/releases/<version>/` without a published database record. `inspect_staged_releases.py` reads the data directory resolved by `server._data_dir()`. From a local checkout this defaults to that checkout's `data/`; the deployed service uses `/var/lib/cafelauncher-backend`. Run the production commands on the Ubuntu backend host as the `cafelauncher` service user so they inspect the deployed database and files:

```bash
sudo -u cafelauncher env CAFELAUNCHER_DATA_DIR=/var/lib/cafelauncher-backend /usr/bin/python3 /opt/cafelauncher-backend/current/inspect_staged_releases.py
sudo -u cafelauncher env CAFELAUNCHER_DATA_DIR=/var/lib/cafelauncher-backend /usr/bin/python3 /opt/cafelauncher-backend/current/inspect_staged_releases.py --version 2026.11.09.1
```

These commands inspect the deployed server because they set its data directory explicitly. For a local development checkout, use `python .\inspect_staged_releases.py` from PowerShell and remember that it inspects local `data/`, not the deployed server. Same-byte retries are safe. If an unpublished staged version contains different intended bytes, use a new version or explicitly clean the orphan after reviewing the inspection report. Cleanup prompts for the exact version, rechecks under a SQLite write lock that it is unpublished, and verifies that the file names, sizes, and hashes still match the displayed inventory before removal. To clean one production orphan, pause publishing and run this on the backend host; automatic cleanup is not performed:

```bash
sudo -u cafelauncher env CAFELAUNCHER_DATA_DIR=/var/lib/cafelauncher-backend /usr/bin/python3 /opt/cafelauncher-backend/current/inspect_staged_releases.py --remove-version 2026.11.09.1
```

## Tailscale Funnel deployment shape

The Ubuntu service is bound to loopback and exposed through Funnel. CaféLauncher clients can use the HTTPS `*.ts.net` URL without joining the tailnet. Do not bind the application to `0.0.0.0` when relying on Funnel as the public entry point.

The API and mirror worker use separate `systemd` services, with secrets in root-readable environment files outside the project folder. The step-by-step Ubuntu checklist and pending automated-deployment bootstrap are in [`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md). Back up the SQLite database plus `data/releases/` and `data/tools/` together with `backup.py`.

## GitHub fallback mirror

The backend publishes its public data to the separate, public, data-only repository [`fabjoncela/cafelauncher-backend-mirror`](https://github.com/fabjoncela/cafelauncher-backend-mirror). For a new installation, initialize the configured branch (default `main`) with a README commit; the GitHub Git API cannot create a branch in an empty repository ([GitHub refs API](https://docs.github.com/en/rest/git/refs)). Create a fine-grained GitHub token scoped only to that repository with Contents: write permission. Store it on the Ubuntu server in `/etc/cafelauncher-github-mirror.env` (mode `0600`), separate from the API sync and publishing tokens:

```text
CAFELAUNCHER_GITHUB_REPO=fabjoncela/cafelauncher-backend-mirror
CAFELAUNCHER_GITHUB_BRANCH=main
CAFELAUNCHER_GITHUB_TOKEN=<fine-grained-token>
```

The backend records a new mirror generation in the same SQLite transaction as every changed profile bundle, release publication, helper publication, and repository import. Identical profile bundles do not create a revision or mirror job. Graphics history records only revisions where the graphics JSON changed, matching CafeLauncher's GitHub path history; profile-only or CFG-only syncs do not appear as graphics versions. Run `python3 github_mirror.py --watch` as a separate persistent service. It checks for new generations every 10 seconds, retries failures with increasing delays capped at 20 minutes, and resumes pending generations after process or machine restart. `python3 github_mirror.py --once` attempts one update; `--status` shows whether the last generation reached GitHub. The example systemd unit is `deploy/cafelauncher-github-mirror.service`; adjust its paths/user and use the same `CAFELAUNCHER_DATA_DIR` as the API service before installing it.

Each successful update is one GitHub commit containing a `mirror-manifest.json`, current profile/graphics/CFG files, recent graphics history, published releases, and mirrored helper assets. The manifest maps logical API resources to raw GitHub paths and includes revisions and SHA-256 values consumed by the current CafeLauncher mirror fallback. Existing binary limits stay below GitHub's 100 MiB per-blob limit, although frequent large-file updates increase mirror repository history and storage. [GitHub Git blobs API](https://docs.github.com/en/rest/git/blobs), [GitHub repository limits](https://docs.github.com/en/repositories/creating-and-managing-repositories/repository-limits)

If GitHub is unreachable, the saved backend data and dirty generation remain in SQLite; the worker retries later. If the machine hosting the backend is powered off, new API saves cannot reach it. CafeLauncher keeps failed edits in a local pending queue and retries them when the backend returns while a session token is available. If a previous request may have committed but its response was lost, the editor requires the user to retry that exact queued request, review/discard it, or cancel before saving another edit.

## Current CafeLauncher integration and release boundaries

The backend-integrated CafeLauncher client is active on the source project's `main` branch and uses this API for profile, graphics, and CFG reads plus atomic profile-bundle writes. Reads fall back to the public GitHub mirror only after validating manifest identity, expected paths, and payload hashes. The client checks revisions, reads successful writes back, and keeps failed writes locally for retry.

Launcher distribution and profile data are separate. **Launcher updates remain on the original `Cs-script` GitHub update endpoints and the existing Google Drive distribution workflow.** The backend release manifest and publisher are available, but they are not wired into the launcher updater; no launcher release has been published through this backend as of 2026-11-09. Keep the graphics module's reviewed Git blob SHA pin and its GitHub source. The optional Logitech helper download also remains on its separate GitHub release. These are current boundaries, not unimplemented profile API integration.

The initial data import created revision 1 for each profile and did not import old GitHub commit history. Backend profile history begins with the first imported/synced bundle; graphics history includes only revisions that changed graphics data. A restored older database can lower profile revisions, so the launcher warns before accepting a lower primary-backend revision and defaults to cancel.

Keep mirrored validation rules synchronized with CafeLauncher's profile and graphics schema. The source project remains the authority for launcher behavior. Continue using the read-only profile viewer and public mirror as documented; do not treat backend release publishing as the active updater until a separate release-channel migration is planned, staged, and verified.

## Security notes

- Public read URLs expose the published profile, graphics, CFG, and release data to anyone who knows or discovers the URL.
- A bearer token entered on a shared PC may be recoverable from that PC. Do not embed the sync or publish token in the launcher.
- The GitHub mirror token belongs only on the server. The café client needs no GitHub credential for anonymous fallback reads.
- The app logs request method/path/status but not authorization headers or request bodies.
- The API binds to loopback locally and in deployment; Tailscale Funnel provides the public HTTPS entry point. Do not expose the API listener directly to the internet.
- Keep backups private. Do not commit `data/`, `.env`, or any real token.
