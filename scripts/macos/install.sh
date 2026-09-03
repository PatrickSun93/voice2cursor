#!/bin/bash
# voice2cursor 安装（macOS）：建 venv(--copies, 让 TCC 有专属 python 可授权)、
# 装依赖、预下载模型、写 ~/ 下的 wrapper 和 launchd plist。
#
# 注意：plist 里绝不能出现 /Volumes 路径(外置盘会让 launchd 任务 EX_CONFIG 静默死掉)，
#       所以 plist 只指向 $HOME/.voice2cursor/run.sh，由 wrapper 再进入外置盘。
#
# 对应的 Windows 脚本是 scripts/windows/setup.ps1 + install-autostart.ps1。
set -euo pipefail

# 本脚本在 scripts/macos/ 下，仓库根在上两级。
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
PY="${PYTHON:-python3}"

echo "==> 1/5 创建 venv (--copies)"
if [ ! -d .venv ]; then
  "$PY" -m venv --copies .venv
fi

echo "==> 2/5 安装依赖"
./.venv/bin/pip install -q -U pip
# requirements.txt 用平台标记分流，mac 上只会装到 mlx-whisper 那一支。
./.venv/bin/pip install -q -r requirements.txt

echo "==> 3/5 预下载/校验 whisper 模型（首次约 1.6GB，耐心）"
./.venv/bin/python - <<'PYCHK'
from voice2cursor.config import Config
from voice2cursor.transcriber import Transcriber
cfg = Config.load()
t = Transcriber(cfg)
t.ensure_loaded()
t.warmup()
print("模型就绪:", t.describe())
PYCHK

echo "==> 4/5 写 wrapper 和 launchd plist"
mkdir -p "$HOME/.voice2cursor/logs"
cat > "$HOME/.voice2cursor/run.sh" <<WRAP
#!/bin/bash
# launchd 入口。plist 不能含 /Volumes 路径，真正的程序路径藏在这里。
cd "$APP_DIR"
exec "$APP_DIR/.venv/bin/python3" -m voice2cursor --headless
WRAP
chmod +x "$HOME/.voice2cursor/run.sh"

PLIST="$HOME/Library/LaunchAgents/com.voice2cursor.plist"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.voice2cursor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$HOME/.voice2cursor/run.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.voice2cursor/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/.voice2cursor/logs/launchd.err.log</string>
</dict>
</plist>
PL

echo "==> 5/5 完成。接下来（重要，TCC 权限按可执行文件授权）："
echo ""
echo "  A. 系统设置 → 隐私与安全性 → 【输入监听】和【辅助功能】"
echo "     两处都点 + 添加这个文件（cmd+shift+G 粘贴路径）："
echo "       $APP_DIR/.venv/bin/python3"
echo ""
echo "  B. 然后启动常驻服务： ./scripts/macos/start.sh"
echo "     第一次按住热键录音时会弹【麦克风】授权，点允许。"
echo ""
echo "  配置在 ~/.voice2cursor/config.json（首次运行会从旧配置迁移过来）"
echo "  调试模式（前台跑，日志直出）： ./.venv/bin/python3 -m voice2cursor --headless"
echo "  自检： ./.venv/bin/python3 -m voice2cursor --doctor"
