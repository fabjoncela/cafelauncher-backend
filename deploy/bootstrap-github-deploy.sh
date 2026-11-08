#!/usr/bin/env bash
set -Eeuo pipefail

if (( EUID != 0 )); then
  printf 'Run this one-time bootstrap with sudo.\n' >&2
  exit 1
fi

readonly APP_ROOT=/opt/cafelauncher-backend
readonly RELEASES="$APP_ROOT/releases"
readonly CURRENT="$APP_ROOT/current"
readonly RUNNER_USER=cafelauncher-deploy
readonly RUNNER_HOME=/var/lib/cafelauncher-deploy
readonly DEPLOY_COMMAND=/usr/local/sbin/cafelauncher-backend-deploy
readonly SUDOERS_FILE=/etc/sudoers.d/cafelauncher-github-deploy
readonly API_UNIT=cafelauncher-backend.service
readonly MIRROR_UNIT=cafelauncher-github-mirror.service
readonly SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

log() { printf '[cafelauncher-bootstrap] %s\n' "$*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

[[ -d "$APP_ROOT" && -f "$APP_ROOT/server.py" && -f "$APP_ROOT/github_mirror.py" ]] || fail 'expected installed backend source is missing'
[[ ! -e "$APP_ROOT/data" && ! -e "$APP_ROOT/.env" ]] || fail 'source tree contains data/ or .env; stopping without changing anything'
[[ -d /var/lib/cafelauncher-backend ]] || fail 'backend data directory is missing'
[[ ! -e "$CURRENT" && ! -L "$CURRENT" ]] || fail 'current symlink already exists; inspect the existing deployment layout'
[[ ! -e "$RELEASES" ]] || fail 'release directory already exists; inspect it before bootstrapping'
[[ ! -e "$DEPLOY_COMMAND" && ! -e "$SUDOERS_FILE" ]] || fail 'a deployment command or sudoers file already exists'
[[ -f "$SOURCE_ROOT/deploy/cafelauncher-backend.service" && -f "$SOURCE_ROOT/deploy/cafelauncher-github-mirror.service" && -x "$SOURCE_ROOT/deploy/cafelauncher-backend-deploy" ]] || fail 'deployment files are missing from this checkout'
id cafelauncher >/dev/null 2>&1 || fail 'cafelauncher service account is missing'
systemctl is-active --quiet "$API_UNIT" || fail 'backend API is not active; stopping before setup'
systemctl is-active --quiet "$MIRROR_UNIT" || fail 'GitHub mirror service is not active; stopping before setup'

if id "$RUNNER_USER" >/dev/null 2>&1; then
  runner_record=$(getent passwd "$RUNNER_USER")
  runner_home=$(cut -d: -f6 <<<"$runner_record")
  runner_shell=$(cut -d: -f7 <<<"$runner_record")
  [[ "$runner_home" == "$RUNNER_HOME" && "$runner_shell" == /bin/bash ]] || fail "$RUNNER_USER already exists with unexpected account settings"
else
  useradd --system --create-home --home-dir "$RUNNER_HOME" --shell /bin/bash "$RUNNER_USER"
fi
install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 0750 "$RUNNER_HOME"

readonly stamp="$(date -u +%Y%m%dT%H%M%SZ)"
readonly backup_root=/var/backups/cafelauncher-backend
readonly backup_dir="$backup_root/automation-bootstrap-$stamp"
readonly release="$RELEASES/bootstrap-current"
install -d -o root -g root -m 0700 "$backup_dir"
cp -a "/etc/systemd/system/$API_UNIT" "/etc/systemd/system/$MIRROR_UNIT" "$backup_dir/"
install -d -o root -g root -m 0755 "$RELEASES"
stage=$(mktemp -d "$RELEASES/.bootstrap.XXXXXX")
cleanup_stage() { [[ -z "${stage:-}" ]] || rm -rf -- "$stage"; }
trap cleanup_stage EXIT

system_changed=0
current_created=0
deploy_command_installed=0
sudoers_installed=0
rollback_on_error() {
  local result=$?
  trap - ERR INT TERM HUP
  if (( system_changed )); then
    log 'setup check failed; restoring the original units and source path'
    install -o root -g root -m 0644 "$backup_dir/$API_UNIT" "/etc/systemd/system/$API_UNIT" || true
    install -o root -g root -m 0644 "$backup_dir/$MIRROR_UNIT" "/etc/systemd/system/$MIRROR_UNIT" || true
    (( current_created )) && rm -f -- "$CURRENT"
    (( deploy_command_installed )) && rm -f -- "$DEPLOY_COMMAND"
    (( sudoers_installed )) && rm -f -- "$SUDOERS_FILE"
    systemctl daemon-reload || true
    systemctl restart "$API_UNIT" "$MIRROR_UNIT" || true
    log "original units saved at $backup_dir"
  fi
  exit "$result"
}

trap rollback_on_error ERR
trap 'rollback_on_error 130' INT
trap 'rollback_on_error 143' TERM HUP

tar -C "$APP_ROOT" \
  --exclude='./.git' \
  --exclude='./releases' \
  --exclude='./current' \
  --exclude='./.deploy-repository.git' \
  -cf - . | tar --extract --directory="$stage" --no-same-owner
[[ -f "$stage/server.py" && -f "$stage/github_mirror.py" ]] || fail 'source snapshot is incomplete'
chown -R root:root "$stage"
chmod -R go-w "$stage"
mv -- "$stage" "$release"
stage=

python3 - "$CURRENT" "$release" <<'PY'
import os
import sys

link, target = sys.argv[1:]
temporary = f"{link}.next.{os.getpid()}"
try:
    os.symlink(target, temporary)
    os.replace(temporary, link)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
current_created=1
system_changed=1

install -o root -g root -m 0644 "$SOURCE_ROOT/deploy/cafelauncher-backend.service" "/etc/systemd/system/$API_UNIT"
install -o root -g root -m 0644 "$SOURCE_ROOT/deploy/cafelauncher-github-mirror.service" "/etc/systemd/system/$MIRROR_UNIT"
install -o root -g root -m 0755 "$SOURCE_ROOT/deploy/cafelauncher-backend-deploy" "$DEPLOY_COMMAND"
deploy_command_installed=1

sudoers_temp=$(mktemp /tmp/cafelauncher-github-deploy.XXXXXX)
printf '%s ALL=(root) NOPASSWD: %s\n' "$RUNNER_USER" "$DEPLOY_COMMAND" >"$sudoers_temp"
visudo -cf "$sudoers_temp"
install -o root -g root -m 0440 "$sudoers_temp" "$SUDOERS_FILE"
rm -f -- "$sudoers_temp"
sudoers_installed=1

systemctl daemon-reload
systemctl restart "$API_UNIT"
python3 - <<'PY'
import json
import time
from urllib.request import urlopen

for _ in range(30):
    try:
        with urlopen("http://127.0.0.1:8090/v1/health", timeout=3) as response:
            if response.status == 200 and json.load(response).get("status") == "ok":
                break
    except Exception:
        time.sleep(2)
else:
    raise SystemExit("API health verification failed")
PY
systemctl restart "$MIRROR_UNIT"
systemctl is-active --quiet "$MIRROR_UNIT"

system_changed=0
trap - ERR INT TERM HUP
log 'systemd services verified; database, assets, API tokens, and mirror token were left in place'
log "rollback copy of the original units: $backup_dir"
log "runner account prepared: $RUNNER_USER"
