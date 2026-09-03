#!/bin/bash
launchctl bootout "gui/$(id -u)/com.voice2cursor" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.voice2cursor.plist"
# 录音留档、配置和词表都是用户数据，卸载不删。只清 logs/lock/run.sh。
find "$HOME/.voice2cursor" -mindepth 1 -maxdepth 1 \
  ! -name recordings ! -name config.json ! -name vocabulary.json \
  -exec rm -rf {} +
echo "已卸载常驻服务（代码和 venv 保留在仓库里，删不删随你）"
echo "保留在 ~/.voice2cursor 的用户数据: recordings/ config.json vocabulary.json"
