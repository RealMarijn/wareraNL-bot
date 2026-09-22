#!/usr/bin/env bash
set -e

cd /home/warera/live/wareraNL-bot

# Preserve live-edited template files that are managed by bot commands
cp templates/mus.json /tmp/mus.json.bak 2>/dev/null || true
cp templates/roles.json /tmp/roles.json.bak 2>/dev/null || true

git fetch origin main
git reset --hard origin/main

# Restore live-edited mus.json (bot commands write MU data to this file)
cp /tmp/mus.json.bak templates/mus.json 2>/dev/null || true

# Merge saved role_id values back into the freshly pulled roles.json.
# This preserves role IDs for existing buttons while still picking up new
# buttons / description changes from git (new buttons keep role_id=0 and
# are auto-created when /generalroles is next run).
if [ -f /tmp/roles.json.bak ]; then
    ./.venv/bin/python3 - <<'PYEOF'
import json, sys

with open("/tmp/roles.json.bak") as f:
    backup = json.load(f)
with open("templates/roles.json") as f:
    fresh = json.load(f)

# Build label -> role_id lookup from backup
saved_ids: dict = {}
for embed in backup.get("embeds", []):
    for btn in embed.get("buttons", []):
        label = btn.get("label")
        role_id = btn.get("role_id") or 0
        if label and role_id:
            saved_ids[label] = role_id

# Apply saved IDs to fresh template (new buttons keep role_id=0)
for embed in fresh.get("embeds", []):
    for btn in embed.get("buttons", []):
        label = btn.get("label")
        if label in saved_ids:
            btn["role_id"] = saved_ids[label]

with open("templates/roles.json", "w") as f:
    json.dump(fresh, f, indent=2, ensure_ascii=False)
    f.write("\n")
PYEOF
fi

./.venv/bin/pip install -e .

sudo systemctl restart wareranl-bot
sudo systemctl restart wareranl-web
sudo systemctl --no-pager --full status wareranl-bot

# ── Data fetcher (all-countries citizen/company/region sweep) ──────────────
# The main bot only ever refreshes NL itself, every hour (see
# cogs/tasks/citizens.py's citizen_refresh — enable_all_countries_sweep is
# false here, on purpose, so the bot's own task loop doesn't race this
# process over database/external.db). Every OTHER country's cached data
# (/paraatheid land:<land>, /fabrieken, /tax-breakdown, etc.) depends
# entirely on this separate process — without it running, a country's data
# just freezes at whatever it last was. This was never provisioned on this
# server, which is why /paraatheid land:Germany was stuck showing July data
# while NL stayed fresh.
#
# Deliberately placed AFTER the bot/web restart above, not before: this repo
# has no SSH access to this server outside of this script, so the unit file
# and its enable/restart have to be provisioned here too, on every deploy.
#
# Confirmed live: this server's passwordless sudo does NOT cover writing unit
# files / daemon-reload / enable (only "systemctl restart/status" on the two
# services above is proven to work) — `sudo tee ...` below fails with
# "sudo: a password is required". So this whole block is wrapped to fail
# SOFT (warn, exit 0) rather than aborting the job — the bot/web restart
# above has already completed by the time this runs either way, and a
# missing data-fetcher shouldn't make every future deploy show as failed.
#
# One-time fix (needs someone with real server access — this repo/CI can't
# do it): either (a) they run the tee + systemctl commands below manually,
# once, via their own sudo, then add a NOPASSWD sudoers line for
# "systemctl restart/status wareranl-datafetcher" (same pattern as the two
# services above) so this block succeeds on its own from then on, or
# (b) broaden this SSH user's sudoers to also cover
# "tee /etc/systemd/system/wareranl-datafetcher.service", "systemctl
# daemon-reload" and "systemctl enable wareranl-datafetcher", so this
# whole block keeps itself up to date automatically on every deploy.
echo "── Provisioning wareranl-datafetcher service ──"
set +e
sudo tee /etc/systemd/system/wareranl-datafetcher.service > /dev/null <<UNIT
[Unit]
Description=WarEra NL - all-countries data fetcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/warera/live/wareraNL-bot
Environment=RW_API_KEYS_PATH=_api_keys_datafetcher.json
ExecStart=/home/warera/live/wareraNL-bot/.venv/bin/python -m services.full_fetcher
Restart=always
RestartSec=15
User=$(whoami)

[Install]
WantedBy=multi-user.target
UNIT
tee_status=$?
provision_status=1
if [[ "$tee_status" -eq 0 ]]; then
    sudo systemctl daemon-reload \
        && sudo systemctl enable wareranl-datafetcher \
        && sudo systemctl restart wareranl-datafetcher \
        && sudo systemctl --no-pager --full status wareranl-datafetcher
    provision_status=$?
fi
set -e

if [[ "$tee_status" -ne 0 || "$provision_status" -ne 0 ]]; then
    echo "⚠️  Could not provision wareranl-datafetcher — sudo declined a password prompt." >&2
    echo "   Needs a one-time manual setup by someone with real server access; see the" >&2
    echo "   comment above this block in deploy.sh for the exact steps." >&2
    echo "   (The bot/web restart above already succeeded regardless.)" >&2
fi
