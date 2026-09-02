#!/bin/bash
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/com.voice2cursor.plist"
launchctl bootout "gui/$(id -u)/com.voice2cursor" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 1
launchctl print "gui/$(id -u)/com.voice2cursor" | grep -E "state|pid" | head -3
echo "已启动。日志: tail -f ~/.voice2cursor/logs/voice2cursor.log"
