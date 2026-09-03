#!/bin/bash
launchctl bootout "gui/$(id -u)/com.voice2cursor" 2>/dev/null && echo 已停止 || echo 本来就没在跑
