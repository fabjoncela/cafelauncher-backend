# Ubuntu deployment checklist

This deploys the API and GitHub mirror as two `systemd` services. It does not use Docker and does not modify any other server stack. The API listens only on `127.0.0.1:8090`; Tailscale Funnel can proxy to that local endpoint.

## 1. Check the server and port

- Confirm the Ubuntu server is the intended host and Python 3.10 or later is installed.
- Check whether port 8090 is already in use:

  ```sh
  sudo ss -ltnp 'sport = :8090'
  ```

- If another service uses 8090, select a free port and change both `CAFELAUNCHER_PORT` in the API service and the Funnel target. Do not change or stop Nextcloud/Jellyfin services to make room.
- Confirm the Funnel endpoint will target `http://127.0.0.1:<port>`; no router port forwarding is needed.

## 2. Copy the backend source

The backend repository is local. After committing the revision you want to deploy, create a Git bundle on Windows and copy it to Ubuntu over SSH/SFTP:

```powershell
git -C 'C:\Users\Admin\Desktop\CafeLauncherBackend' bundle create 'C:\Users\Admin\Desktop\cafelauncher-backend.bundle' main
scp 'C:\Users\Admin\Desktop\cafelauncher-backend.bundle' '<ubuntu-user>@<server>:/tmp/cafelauncher-backend.bundle'
```

On Ubuntu, install a service account and clone the bundle into `/opt`:

```sh
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin cafelauncher
sudo git clone --branch main /tmp/cafelauncher-backend.bundle /opt/cafelauncher-backend
sudo chown -R root:root /opt/cafelauncher-backend
sudo chmod -R a+rX /opt/cafelauncher-backend
sudo install -d -o cafelauncher -g cafelauncher -m 0750 /var/lib/cafelauncher-backend
```

Keep `data/`, `.env`, backups, and all tokens out of the source bundle. The live database and published assets belong in `/var/lib/cafelauncher-backend`.

## 3. Add service secrets

Create the API environment file as root with mode `0600`:

```sh
sudo install -o root -g root -m 0600 /dev/null /etc/cafelauncher-backend.env
sudoedit /etc/cafelauncher-backend.env
```

Add separate random tokens (each at least 32 characters):

```text
CAFELAUNCHER_SYNC_TOKEN=<sync-token>
CAFELAUNCHER_PUBLISH_TOKEN=<different-publish-token>
```

For the GitHub mirror, create `/etc/cafelauncher-github-mirror.env` separately with mode `0600`. It should contain only the public data repository, branch, and a fine-grained token with Contents: write access to that repository:

```text
CAFELAUNCHER_GITHUB_REPO=<owner/data-repository>
CAFELAUNCHER_GITHUB_BRANCH=main
CAFELAUNCHER_GITHUB_TOKEN=<repository-scoped-token>
```

The mirror repository must be public and its branch must already exist with an initial README commit. Never put the GitHub token in the API environment file or the CafeLauncher client.

## 4. Seed the database

Choose one method before starting the mirror worker:

- Import the six current profile bundles from a read-only source checkout that contains `profiles/`, `graphics/`, and the CFG files. Run `import_repository.py --dry-run` first, then run it without `--dry-run` using `CAFELAUNCHER_DATA_DIR=/var/lib/cafelauncher-backend`.
- Or restore a verified `backup.py` snapshot into `/var/lib/cafelauncher-backend`.

Do not start mirroring an empty database if you intend to publish the current profiles. The import/restore data must be owned by `cafelauncher` and mode-restricted to that service account.

## 5. Install and start the API

```sh
sudo install -m 0644 /opt/cafelauncher-backend/deploy/cafelauncher-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cafelauncher-backend.service
sudo systemctl status cafelauncher-backend.service
curl --fail http://127.0.0.1:8090/v1/health
```

Check logs if startup fails:

```sh
sudo journalctl -u cafelauncher-backend.service -n 100 --no-pager
```

The health endpoint should return JSON with `"status":"ok"`. The service has no public listener; Funnel is the public HTTPS entry point.

## 6. Start and verify the mirror worker

Start the mirror only after the API database has been seeded and the GitHub environment file is in place:

```sh
sudo install -m 0644 /opt/cafelauncher-backend/deploy/cafelauncher-github-mirror.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cafelauncher-github-mirror.service
sudo systemctl status cafelauncher-github-mirror.service
sudo journalctl -u cafelauncher-github-mirror.service -n 100 --no-pager
sudo -u cafelauncher env CAFELAUNCHER_DATA_DIR=/var/lib/cafelauncher-backend /usr/bin/python3 /opt/cafelauncher-backend/github_mirror.py --status
```

Wait until `generation` equals `mirrored_generation`, then verify the public `mirror-manifest.json` and a few profile/CFG paths in the data repository. Restart the mirror service once and confirm it remains healthy. For outage recovery, test in a staging repository: leave a generation pending during a simulated GitHub failure, restore connectivity, and confirm the worker catches up without creating conflicting branch history.

## 7. Enable Funnel and do a final check

- Enable Tailscale Funnel to proxy HTTPS traffic to the verified loopback port.
- From outside the server, check the HTTPS health endpoint and public profile/CFG endpoints.
- Confirm public reads work without a token and sync/publishing requests still require their respective bearer tokens.
- Run `backup.py`, then verify that the database, `releases/`, and `tools/` are included in the backup. Keep the backup private.
- Do not point CafeLauncher clients at this backend until their opt-in adapter, GitHub read fallback, and local pending-write retry have been implemented and tested.

## Automated tests and deployment

`.github/workflows/backend-ci-deploy.yml` runs the standard-library test suite on Python 3.10 and 3.14 for pull requests and pushes to `main`. A successful push to `main` then deploys that exact commit. Pull requests never run a job on the server.

The deploy job needs a repository-scoped self-hosted GitHub Actions runner on this Ubuntu host with the label `cafe-backend-deploy`. It should run as a dedicated unprivileged `cafelauncher-deploy` account, not as root, and should be registered only to `fabjoncela/cafelauncher-backend`. Do not register it at organization scope or allow pull-request jobs onto it. GitHub warns against using self-hosted runners for public-repository pull requests because untrusted code could compromise the runner; this workflow keeps all PR tests on GitHub-hosted runners and gates deployment to trusted `main` pushes.

### One-time server setup

The following setup is still required before automatic deployment can run. It changes only the two CafeLauncher systemd units and adds a narrowly scoped runner account and deploy command. Do not remove or copy `/var/lib/cafelauncher-backend`, the API/mirror environment files, or any existing system data.

1. Confirm the existing source at `/opt/cafelauncher-backend` has no `data/` directory or `.env`; stop if either exists. Create `/opt/cafelauncher-backend/releases/bootstrap-current`, then copy the existing source files there while excluding `.git`, `releases`, `current`, and `.deploy-repository.git`. Make the snapshot root-owned and readable by the `cafelauncher` service account. Point `/opt/cafelauncher-backend/current` at `releases/bootstrap-current`.
2. Install the two unit files from this repository into `/etc/systemd/system/`. They use `/opt/cafelauncher-backend/current`; data and secret paths remain unchanged. Install `deploy/cafelauncher-backend-deploy` as root-owned `/usr/local/sbin/cafelauncher-backend-deploy` mode `0755`.
3. Create the local `cafelauncher-deploy` account and install a sudoers rule allowing that account to run only `/usr/local/sbin/cafelauncher-backend-deploy` as root, without a password. Validate the rule with `visudo -cf` before reloading systemd or restarting anything.
4. Reload systemd, restart the API, check `http://127.0.0.1:8090/v1/health`, then restart the mirror and confirm it is active. If any check fails, point `current` back to the original `/opt/cafelauncher-backend` source snapshot, restore the previous unit files, and restart the two CafeLauncher services.
5. Install and register a repository-scoped GitHub Actions runner using the commands GitHub generates under this repository's **Settings → Actions → Runners**. Run its service as `cafelauncher-deploy`; assign label `cafe-backend-deploy`. Keep the short-lived runner registration token private and out of shell history where possible.

The deploy script accepts exactly one commit SHA on standard input, fetches public `main`, and refuses commits outside its history. It extracts code into a versioned, root-owned directory, atomically switches `current`, restarts the API, waits for the health endpoint, then restarts and checks the mirror unit. If verification fails, it restores the previous `current` symlink and restarts both units. It does not write to the database, assets, or environment files. Keep the previous release directories until a later cleanup policy is reviewed.

After setup, the GitHub Actions `production` environment and runner label must be available. On each successful `main` push, GitHub Actions tests first; the server runner receives only the tested commit SHA. A test failure prevents deployment. `workflow_dispatch` works only when run from `main` and also passes through the same tests.

Until this one-time setup is complete, pushes still run CI but the deploy job will remain queued waiting for the dedicated runner. The database and published assets stay under `/var/lib/cafelauncher-backend` and must not be replaced by source updates.
