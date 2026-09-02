#!/bin/bash
launchctl bootout "gui/$(id -u)/com.voice2cursor" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.voice2cursor.plist"
# 录音留档是用户数据，卸载不删。只清 logs/lock/run.sh。
find "$HOME/.voice2cursor" -mindepth 1 -maxdepth 1 ! -name recordings -exec rm -rf {} +
echo "已卸载常驻服务和状态目录（代码和 venv 保留在本目录，删不删随你）"
echo "录音留档保留在 ~/.voice2cursor/recordings，不要了自己删。"
