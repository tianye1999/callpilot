#!/usr/bin/env bash
# Run as the service user, outside a call. No install.sh/udev/unit changes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE=callpilot-edge.service
REPOSITORY=https://github.com/tianye1999/callpilot.git
ADOPT=false
FORCE=false
DRY_RUN=false
BLUETOOTH=false
BRANCH=""
TMP=""
BACKUP=""
LOCKED=false
ADOPT_WRITING=false
UPDATE_STARTED=false
OLD_SHA=""

info() { printf '[linux-update] %s\n' "$*"; }
die() { printf '[linux-update] ERROR: %s\n' "$*" >&2; exit 1; }
restore_adopt() {
    tar -xOf "$BACKUP" callpilot-adopt-rollback.sh | bash -s -- "$ROOT" "$BACKUP"
}
finish() {
    local status=$?
    trap - EXIT
    if [ "$status" -ne 0 ]; then
        printf '[linux-update] Logs: journalctl -u %s -b -n 100 --no-pager (add sudo if needed)\n' "$SERVICE" >&2
        if [ "$ADOPT_WRITING" = true ]; then
            printf '[linux-update] Adoption failed; restoring the snapshot from %s\n' "$BACKUP" >&2
            restore_adopt || printf '[linux-update] Automatic restore failed; use the backup rollback command below.\n' >&2
        fi
        if [ -n "$BACKUP" ]; then
            printf '[linux-update] Snapshot rollback (before further edits): tar -xOf %q callpilot-adopt-rollback.sh | bash -s -- %q %q\n' "$BACKUP" "$ROOT" "$BACKUP" >&2
        fi
        if [ -n "$OLD_SHA" ]; then
            if [ "$UPDATE_STARTED" = false ]; then
                printf '[linux-update] Code and service were not changed; rollback is not needed.\n' >&2
            fi
            printf '[linux-update] Code rollback, only when idle: cd %q && git checkout %s && sudo systemctl restart %s\n' "$ROOT" "$OLD_SHA" "$SERVICE" >&2
            printf '[linux-update] This does not roll back installed dependencies; re-run setup.sh at the old SHA if needed.\n' >&2
        fi
    fi
    [ -z "$TMP" ] || rm -rf -- "$TMP"
    [ "$LOCKED" = false ] || rmdir -- "$ROOT/.callpilot-update.lock"
    exit "$status"
}
trap finish EXIT

while [ "$#" -gt 0 ]; do
    case "$1" in
        --adopt) ADOPT=true ;;
        --force) FORCE=true ;;
        --dry-run) DRY_RUN=true ;;
        --bluetooth) BLUETOOTH=true ;;
        --branch)
            [ "$#" -ge 2 ] && [ -n "$2" ] || die "--branch requires a branch name."
            BRANCH="$2"; shift ;;
        -h|--help)
            printf 'Usage: bash scripts/linux/update.sh [--branch NAME] [--force] [--dry-run] [--bluetooth]\n'
            printf '%s\n' '--bluetooth confirms the phone itself is idle and runs optional HFP setup.'
            printf '       bash scripts/linux/update.sh --adopt --branch NAME [--force] [--dry-run]\n'
            printf 'Run without sudo. --force accepts UNKNOWN call state, never a known busy line.\n'
            exit 0 ;;
        *) die "Unknown argument: $1 (use --help)" ;;
    esac
    shift
done

[ "$(uname -s)" = Linux ] || die "This updater is Linux-only."
[ "$(id -u)" -ne 0 ] || die "Run as an ordinary user, not with sudo; privileged steps request sudo separately."
[ "$ADOPT" = false ] || [ -n "$BRANCH" ] || die "--adopt requires --branch NAME."
HEALTH_TIMEOUT="${CALLPILOT_UPDATE_HEALTH_TIMEOUT:-60}"
case "$HEALTH_TIMEOUT" in ''|*[!0-9]*) die "Health timeout must be a positive integer." ;; esac
[ "$HEALTH_TIMEOUT" -gt 0 ] || die "Health timeout must be positive."
cd "$ROOT"
if [ "$DRY_RUN" = true ]; then
    if [ "$BLUETOOTH" = true ]; then
        info "Would install Bluetooth extra/oFono and configure WirePlumber 0.4.x; phone must be idle."
    fi
    info "DRY RUN: no fetch, backup, checkout, dependency installation, API request, or sudo."
    info "Check repository ownership, tracked changes, and local Web call/queue state (--force only accepts unknown state)."
    if [ "$ADOPT" = true ]; then
        info "Fetch $REPOSITORY branch $BRANCH into temporary Git metadata."
        info "Back up overwritten tracked paths to ~/callpilot-adopt-backup-<time>.tgz with a rollback helper."
        info "Adopt tracked files including data/*.example.json; preserve ignored runtime data and unknown snapshot files."
        info "Track origin/$BRANCH, then ask you to run update.sh again; do not setup or restart."
    else
        info "Fetch ${BRANCH:-the current branch upstream}; reject tracked changes, non-fast-forward and untracked/ignored collisions."
        info "Record old SHA; sudo true; git merge --ff-only --no-overwrite-ignore <target>."
        info "uv python install 3.13; uv python find 3.13; PYTHON=<interpreter> bash scripts/setup.sh."
        info "Print missing keys from both .env.example files without values or writes."
        info "Recheck call/queue state; sudo systemctl restart $SERVICE."
        info "Poll loopback /api/meta for HTTP 200 and systemctl is-active for up to 60 seconds; print old SHA -> new SHA or rollback guidance."
    fi
    exit 0
fi

for cmd in git curl sudo tar mktemp; do
    command -v "$cmd" >/dev/null 2>&1 || die "Missing $cmd; see docs/raspberry-pi.md prerequisites."
done
if [ -n "$BRANCH" ]; then
    git check-ref-format "refs/heads/$BRANCH" >/dev/null || die "Invalid branch name."
fi
TOP="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ "$ADOPT" = true ]; then
    [ -z "$TOP" ] && [ ! -e .git ] && [ ! -L .git ] || die "--adopt is only for a non-git snapshot, not an existing/nested worktree."
else
    [ "$TOP" = "$ROOT" ] || die "Not a git worktree rooted here. First run: bash scripts/linux/update.sh --adopt --branch <name>"
    [ -f .env ] || die "Existing .env is required; configure the service before updating (no .env was created)."
fi
BOOT_PY="$ROOT/.venv/bin/python"
if [ ! -x "$BOOT_PY" ]; then
    BOOT_PY="$(command -v python3 || true)"
fi
[ -n "$BOOT_PY" ] || die "An existing Python 3 interpreter is required for read-only JSON checks."
# Never source .env: it is configuration data, not shell code.
PORT="$("$BOOT_PY" - "$ROOT/.env" <<'PY'
import os, re, shlex, sys
from pathlib import Path
port = os.environ.get("WEB_PORT")
if port is None:
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    for line in text.splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?WEB_PORT\s*=\s*(.*)", line)
        if match:
            values = shlex.split(match[1], comments=True)
            if len(values) > 1:
                sys.exit("Invalid WEB_PORT; expected a single port number.")
            port = values[0] if values else ""
if port is None or port == "":
    port = "47100"
if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
    sys.exit("Invalid WEB_PORT; expected a port number.")
print(int(port))
PY
)"
BASE_URL="http://127.0.0.1:$PORT"
check_idle() {
    local meta queue remote state
    meta="$(curl --noproxy '*' --fail --silent --show-error --connect-timeout 2 --max-time 3 "$BASE_URL/api/meta" 2>/dev/null)" || meta=""
    queue="$(curl --noproxy '*' --fail --silent --show-error --connect-timeout 2 --max-time 3 "$BASE_URL/api/call/queue" 2>/dev/null)" || queue=""
    remote="$(curl --noproxy '*' --fail --silent --show-error --connect-timeout 2 --max-time 3 "$BASE_URL/api/remote_dialer/status" 2>/dev/null)" || remote=""
    state="$(META_JSON="$meta" QUEUE_JSON="$queue" REMOTE_JSON="$remote" "$BOOT_PY" - <<'PY'
import json, os
def payload(key):
    try:
        value = json.loads(os.environ[key])
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}
meta, queue, remote = payload("META_JSON"), payload("QUEUE_JSON"), payload("REMOTE_JSON")
busy = (meta.get("call_active") is True or meta.get("dial_queue_busy") is True
        or queue.get("active") is True or bool(queue.get("pending"))
        or queue.get("current") is not None or remote.get("active") is True
        or remote.get("call_active") is True)
queue_idle = (meta.get("dial_queue_busy") is False
              or (queue.get("ok") is True and queue.get("active") is False
                  and queue.get("pending") == [] and "current" in queue
                  and queue["current"] is None))
idle = meta.get("call_active") is False and queue_idle
print("busy" if busy else "idle" if idle else "unknown")
PY
)"
    [ "$state" != busy ] || die "Line/queue is busy; refusing even with --force. Wait until all calls and queued work end."
    [ "$state" != idle ] || return 0
    if [ "$FORCE" != true ]; then
        die "Cannot verify idle: /api/meta call_active or queue state is missing/invalid, or the API is unavailable (possibly an older Edge). Confirm idle yourself, then explicitly pass --force."
    fi
    info "WARNING: --force accepts unknown call/queue state. Known busy calls or queues still block updates."
}
check_idle
mkdir -- "$ROOT/.callpilot-update.lock" 2>/dev/null || die "Another update is running, or .callpilot-update.lock is stale; inspect before removing it."
LOCKED=true
umask 077
TMP="$(mktemp -d "$ROOT/.callpilot-update.XXXXXX")"

if [ "$ADOPT" = true ]; then
    adopt_git() { git --git-dir="$TMP/git" --work-tree="$ROOT" "$@"; }
    git init --bare "$TMP/git" >/dev/null
    adopt_git config core.bare false
    adopt_git config core.worktree "$ROOT"
    adopt_git remote add origin "$REPOSITORY"
    adopt_git fetch origin "refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
    adopt_git symbolic-ref HEAD "refs/heads/$BRANCH"
    adopt_git update-ref HEAD "refs/remotes/origin/$BRANCH"
    adopt_git config "branch.$BRANCH.remote" origin
    adopt_git config "branch.$BRANCH.merge" "refs/heads/$BRANCH"
    BACKUP="$(GIT_DIR="$TMP/git" GIT_WORK_TREE="$ROOT" "$BOOT_PY" - "$ROOT" "$TMP" "$HOME" <<'PY'
import datetime, io, os, shlex, subprocess, sys, tarfile
from pathlib import Path
root, temp, home = map(Path, sys.argv[1:])
entries = subprocess.check_output(["git", "ls-tree", "-rz", "--full-tree", "HEAD"]).split(b"\0")
paths = []
for entry in filter(None, entries):
    metadata, name = entry.split(b"\t", 1)
    path = os.fsdecode(name)
    if metadata.split()[0] not in {b"100644", b"100755", b"120000"}:
        sys.exit("Cannot adopt submodule/special path: " + path)
    paths.append(path)
raw = b"".join(os.fsencode(p) + b"\0" for p in paths)
ignored = subprocess.run(["git", "check-ignore", "--no-index", "-z", "--stdin"], input=raw, capture_output=True)
if ignored.returncode not in (0, 1):
    sys.exit("Could not determine ignored paths; refusing adoption.")
excluded = set(ignored.stdout.split(b"\0"))
paths = [p for p in paths if os.fsencode(p) not in excluded]
helper = "callpilot-adopt-rollback.sh"
if helper in paths or (root / helper).exists():
    sys.exit("Reserved rollback helper path already exists.")
existing, created, new_dirs = [], [], set()
for path in paths:
    dest = root / path
    for parent in dest.parents:
        if parent == root:
            break
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            sys.exit("Unsafe parent path; refusing adoption: " + path)
        if not parent.exists():
            new_dirs.add(str(parent.relative_to(root)))
    if dest.exists() and not dest.is_file() and not dest.is_symlink():
        sys.exit("Directory/special-file collision; refusing adoption: " + path)
    (existing if dest.exists() or dest.is_symlink() else created).append(path)
home.mkdir(parents=True, exist_ok=True)
backup = home / ("callpilot-adopt-backup-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".tgz")
restore = ['#!/usr/bin/env bash', 'set -euo pipefail', 'cd -- "$1"', 'backup="$2"',
           'if [ -e .git ] || [ -L .git ]; then',
           '  [ ! -e "$backup.git" ] && [ ! -L "$backup.git" ] || { echo "Rollback metadata destination exists" >&2; exit 1; }',
           '  mv -- .git "$backup.git"', 'fi']
restore += ["rm -f -- " + shlex.quote(p) for p in created]
restore += ['tar -xzf "$backup" --exclude=' + helper]
restore += ["rmdir -- " + shlex.quote(p) + " 2>/dev/null || true"
            for p in sorted(new_dirs, key=lambda p: len(Path(p).parts), reverse=True)]
payload = ("\n".join(restore) + "\n").encode("utf-8")
with tarfile.open(backup, "x:gz", dereference=False) as archive:
    for path in existing:
        archive.add(root / path, arcname=path, recursive=False)
    member = tarfile.TarInfo(helper)
    member.size, member.mode = len(payload), 0o700
    archive.addfile(member, io.BytesIO(payload))
(temp / "adopt-paths").write_bytes(b"".join(os.fsencode(p) + b"\0" for p in paths))
print(backup)
PY
)"
    info "Adopt backup: $BACKUP (private; includes an immediate snapshot rollback helper)."
    printf '[linux-update] Snapshot rollback, before further edits: tar -xOf %q callpilot-adopt-rollback.sh | bash -s -- %q %q\n' "$BACKUP" "$ROOT" "$BACKUP"
    check_idle
    [ ! -e .git ] && [ ! -L .git ] || die "Git metadata appeared during adoption; inspect before retrying."
    adopt_git read-tree HEAD
    ADOPT_WRITING=true
    adopt_git checkout-index --force -z --stdin < "$TMP/adopt-paths"
    mv -- "$TMP/git" "$ROOT/.git"
    ADOPT_WRITING=false
    info "Adopted origin/$BRANCH in place. Protected paths and unknown snapshot files were not changed."
    info "Review git status; then run: bash scripts/linux/update.sh (older running Edge may still need --force)."
    exit 0
fi

check_clean() {
    local changes
    changes="$(git status --porcelain --untracked-files=no)"
    if [ -n "$changes" ]; then
        printf '[linux-update] Tracked local changes; refusing update:\n%s\n' "$changes" >&2
        exit 1
    fi
}
check_clean
CURRENT="$(git symbolic-ref --short HEAD)" || die "Detached HEAD; restore a tracking branch before updating."
if [ -n "$BRANCH" ]; then
    REMOTE=origin
    MERGE_REF="refs/heads/$BRANCH"
else
    REMOTE="$(git config --get "branch.$CURRENT.remote" || true)"
    MERGE_REF="$(git config --get "branch.$CURRENT.merge" || true)"
    [ -n "$REMOTE" ] && [ -n "$MERGE_REF" ] || die "Current branch has no upstream; pass --branch NAME (uses origin)."
fi
OLD_SHA="$(git rev-parse HEAD)"
info "Fetching $REMOTE $MERGE_REF; before SHA: $OLD_SHA"
git fetch -- "$REMOTE" "$MERGE_REF"
TARGET_SHA="$(git rev-parse 'FETCH_HEAD^{commit}')"
git merge-base --is-ancestor "$OLD_SHA" "$TARGET_SHA" || die "Target is not a fast-forward; no merge, reset, or rebase was performed."
UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ] && [ -x "$HOME/.local/bin/uv" ]; then
    UV_BIN="$HOME/.local/bin/uv"
fi
[ -n "$UV_BIN" ] || die "uv is required. Install explicitly: https://docs.astral.sh/uv/getting-started/installation/ ; then export PATH=\"\$HOME/.local/bin:\$PATH\"."
info "Using uv: $UV_BIN"
sudo true || die "sudo command authentication failed. Run from an interactive terminal to enter a password, or check this user's sudo command permissions."
check_idle
check_clean
[ "$(git rev-parse HEAD)" = "$OLD_SHA" ] || die "HEAD changed during update; retry after inspecting the worktree."
UPDATE_STARTED=true
git merge --ff-only --no-overwrite-ignore "$TARGET_SHA"
NEW_SHA="$(git rev-parse HEAD)"
"$UV_BIN" python install 3.13
PYTHON_BIN="$("$UV_BIN" python find 3.13)"
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || die "Selected interpreter must be Python >=3.12."
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    die "Existing .venv is older than 3.12. Move it aside manually; no files were removed."
fi
info "Reusing scripts/setup.sh; existing .env and untracked runtime data are preserved."
PYTHON="$PYTHON_BIN" bash scripts/setup.sh
if [ "$BLUETOOTH" = true ]; then
    check_idle
    bash scripts/linux/setup-bluetooth.sh --idle-confirmed
fi
info "Keys present in examples but missing from .env (names only; merge manually):"
.venv/bin/python - <<'PY'
from dotenv import dotenv_values
present = set(dotenv_values(".env"))
expected = set(dotenv_values(".env.example")) | set(dotenv_values("scripts/linux/.env.example"))
print("\n".join(sorted(expected - present)) or "(none)")
PY
check_idle
sudo systemctl restart "$SERVICE"
DEADLINE=$((SECONDS + HEALTH_TIMEOUT))
while [ "$SECONDS" -lt "$DEADLINE" ]; do
    if CODE="$(curl --noproxy '*' --silent --show-error --connect-timeout 1 --max-time 2 --output /dev/null --write-out '%{http_code}' "$BASE_URL/api/meta" 2>/dev/null)" \
        && [ "$CODE" = 200 ] && sudo systemctl is-active --quiet "$SERVICE"; then
        info "Update successful: $OLD_SHA -> $NEW_SHA"
        info "Service active; loopback /api/meta HTTP 200. Hardware/audio acceptance is still separate."
        exit 0
    fi
    sleep 1
done
die "Health check failed after about $HEALTH_TIMEOUT seconds; expected HTTP 200 and active $SERVICE."
