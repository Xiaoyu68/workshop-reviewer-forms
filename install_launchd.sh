#!/usr/bin/env bash
# Install (or remove with --uninstall) a macOS launchd job that runs
# find_forms.py every day at the given hour (default 09:00).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
label="local.workshop-reviewer-forms"
plist="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "$domain/$label" 2>/dev/null || true
  rm -f "$plist"; echo "removed $label"; exit 0
fi

hour=${1:-9}
python=$(command -v python3)
mkdir -p "$here/state" "$(dirname "$plist")"
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$python</string><string>$here/find_forms.py</string></array>
  <key>WorkingDirectory</key><string>$here</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$here/state/last_run.log</string>
  <key>StandardErrorPath</key><string>$here/state/last_run.log</string>
</dict>
</plist>
PLIST
launchctl bootout "$domain/$label" 2>/dev/null || true
launchctl bootstrap "$domain" "$plist"
echo "installed $label: runs daily at $hour:00, log in state/last_run.log"
