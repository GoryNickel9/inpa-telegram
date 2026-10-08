#!/bin/sh
# Install units and ownership after code, private environment and database upload.
set -eu
test "$(id -u)" -eq 0
test -x /opt/inpa-telegram/.venv/bin/python
test -f /etc/inpa-bot.env
test -f /var/lib/inpa-bot/inpa.sqlite3
if ! id inpa-bot >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/inpa-bot --shell /usr/sbin/nologin inpa-bot
fi
install -d -m 700 -o inpa-bot -g inpa-bot /var/lib/inpa-bot /var/lib/inpa-bot/backups
chown inpa-bot:inpa-bot /var/lib/inpa-bot/inpa.sqlite3
chmod 600 /var/lib/inpa-bot/inpa.sqlite3 /etc/inpa-bot.env
for unit in inpa-bot.service inpa-watch.service inpa-backup.service inpa-backup.timer; do
    install -m 644 "/opt/inpa-telegram/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/inpa-bot.service /etc/systemd/system/inpa-watch.service /etc/systemd/system/inpa-backup.service /etc/systemd/system/inpa-backup.timer
echo 'Units installed; no service started by this script.'
