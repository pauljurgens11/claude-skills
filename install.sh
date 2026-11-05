#!/bin/bash
# Build the panel and start it now and at every login. Re-run after pulling changes.
# Uninstall: ./install.sh --uninstall
set -euo pipefail
HOME_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.claude-skills.panel"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$PLIST"
  echo "Removed. Your XP data stays in ~/Library/Application Support/ClaudeSkills (delete it to start over)."
  exit 0
fi

command -v swiftc >/dev/null || { echo "Needs Xcode command line tools: xcode-select --install"; exit 1; }
PYTHON="$(command -v python3)"
GH="$(command -v gh || true)"   # Shipping reads merged PRs through the GitHub CLI; without it Shipping stays at 1
mkdir -p "$HOME_DIR/build" "$HOME/Library/LaunchAgents"
swiftc -O "$HOME_DIR/panel/main.swift" -o "$HOME_DIR/build/ClaudeSkills"

echo "Scanning your Claude Code history (first run takes a minute)..."
"$PYTHON" "$HOME_DIR/tracker.py" --print

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$HOME_DIR/build/ClaudeSkills</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CLAUDE_SKILLS_HOME</key><string>$HOME_DIR</string>
    <key>CLAUDE_SKILLS_PYTHON</key><string>$PYTHON</string>
    <key>CLAUDE_SKILLS_GH</key><string>$GH</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/ClaudeSkills.log</string>
</dict>
</plist>
EOF
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Running. It starts at every login and restarts itself if it ever stops. Remove with ./install.sh --uninstall"
