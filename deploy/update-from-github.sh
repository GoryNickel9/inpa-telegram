#!/bin/sh
# Run as root through SSH, passing the full, locally tested Git commit.
main() {
set -eu
test "$(id -u)" -eq 0
target=${1:?Usage: sh deploy/update-from-github.sh FULL_COMMIT_SHA}
case "$target" in *[!0-9a-f]*|'') echo 'Invalid commit' >&2; exit 2;; esac
test "${#target}" -eq 40
cd /opt/inpa-telegram
test "$(git rev-parse --show-toplevel)" = /opt/inpa-telegram
test -z "$(git status --porcelain --untracked-files=no)"
previous=$(git rev-parse HEAD)
git fetch --prune origin main
git cat-file -e "$target^{commit}"
git merge-base --is-ancestor "$target" origin/main
# Dependencies and service units need a separately reviewed migration.
git show "$target:requirements-dev.lock" | cmp -s requirements-dev.lock -
for unit in inpa-bot.service inpa-watch.service inpa-backup.service inpa-backup.timer; do
    git show "$target:deploy/systemd/$unit" | cmp -s "/etc/systemd/system/$unit" -
done
stage=$(mktemp -d /opt/inpa-validation.XXXXXX)
restore_needed=0
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ "$restore_needed" -eq 1 ]; then
        git checkout --force -B main "$previous"
        systemctl start inpa-bot.service
        echo "Restored code to $previous; database not rolled back" >&2
    fi
    rm -rf -- "$stage"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
git archive "$target" | tar -x -C "$stage"
(
    cd "$stage"
    export PYTHONPATH="$stage/src"
    /opt/inpa-telegram/.venv/bin/python -m pytest tests -q
    /opt/inpa-telegram/.venv/bin/python -m ruff check src tests scripts
)
.venv/bin/python -m pip check
systemctl start inpa-backup.service
test "$(systemctl show inpa-backup.service -p Result --value)" = success
watch_started=$(systemctl show inpa-watch.service -p ActiveEnterTimestamp --value)
restore_needed=1
systemctl stop inpa-bot.service
git checkout --force -B main "$target"
systemctl start inpa-bot.service
sleep 5
systemctl is-active --quiet inpa-bot.service
test "$(systemctl show inpa-watch.service -p ActiveEnterTimestamp --value)" = "$watch_started"
systemctl is-active inpa-bot.service inpa-watch.service inpa-backup.timer
restore_needed=0
printf 'Deployed commit: %s\n' "$(git rev-parse HEAD)"
}
# Parse the function before checkout can replace this script on disk.
main "$@"
