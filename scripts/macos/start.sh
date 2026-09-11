#!/bin/bash
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/com.voice2cursor.plist"
SERVICE="gui/$(id -u)/com.voice2cursor"
launchctl bootout "$SERVICE" 2>/dev/null || true
# bootout 是异步的：旧进程没退干净就 bootstrap 会报 "5: Input/output error"
for _ in $(seq 20); do
  launchctl print "$SERVICE" >/dev/null 2>&1 || break
  sleep 0.25
done
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 1
launchctl print "$SERVICE" | grep -E "state|pid" | head -3
echo "已启动，右上角菜单栏会出现麦克风图标。日志: tail -f ~/.voice2cursor/logs/voice2cursor.log"
