# voice2cursor

按住 **右 Option** 说话，松开自动转文字并粘贴到当前光标处（Cursor 或任何 app）。
全本地：mlx-whisper (large-v3-turbo, Apple Silicon GPU)，中英混说，可选 Ollama 后处理。

## 安装

```bash
./install.sh    # venv + 依赖 + 下载模型(~1.6GB)
```

装完按提示做两件事：

1. **系统设置 → 隐私与安全性 → 输入监听 + 辅助功能**，两处都添加
   `.venv/bin/python3`（绝对路径见 install.sh 输出）。
   没有输入监听 → 热键没反应；没有辅助功能 → 只复制不粘贴。
2. `./start.sh` 启动常驻。第一次录音弹**麦克风**授权，点允许。

## 日常

| 操作 | 命令 |
|---|---|
| 用 | 按住右 Option 说话，松开出字（听到 Tink=开始录，Pop=已粘贴，Basso=出错） |
| 看日志 | `tail -f ~/.voice2cursor/logs/voice2cursor.log` |
| 回听/查录音 | `open ~/.voice2cursor/recordings`（每段一个 `.wav` + 同名 `.txt` 转写，按时间命名） |
| 停 / 启 | `./stop.sh` / `./start.sh` |
| 前台调试 | `./.venv/bin/python3 voice2cursor.py`（先 `./stop.sh`，有单实例锁） |
| 卸载 | `./uninstall.sh` |

## 配置 config.json（改完 `./start.sh` 重启生效）

- `hotkey`: 任意键。命名键如 `alt_r`(默认)/`cmd_r`/`shift_r`/`f13`~`f20`，或任意物理键写 `"vk:键码"`（跑 `./.venv/bin/python3 voice2cursor.py --keytest` 按一下想用的键即可查到）
- `language`: `null`=自动检测（默认）。若短句语言检测不稳或出繁体，改 `"zh"`
- `initial_prompt`: 引导简体+中英混排的提示词
- `auto_paste`: `false` 则只进剪贴板不自动 Cmd+V
- `save_recordings`: `true`(默认) 每次录音都存 WAV + 同名 `.txt` 转写到 `recordings_dir`（默认 `~/.voice2cursor/recordings`）。16kHz 单声道，约 2MB/分钟，不自动清理，满了自己删旧的。`false` 则不留档
- `ollama.enabled`: `true` 开启后处理（先 `ollama pull qwen2.5:3b`），每句多 1~3 秒

## 已知坑（外置盘 + launchd）

- plist 里**不能出现任何 /Volumes 路径**，否则任务 EX_CONFIG 静默死。
  所以 plist → `~/.voice2cursor/run.sh` → 再进外置盘。别"简化"这一层。
- TCC 权限按**可执行文件**授权。venv 用了 `--copies`，授权对象就是
  `.venv/bin/python3` 这个文件本身；重建 venv 后要重新授权。
- 模型常驻内存约 1.5GB（`preload_model: true`）。不想常驻占内存改 `false`，
  代价是每次重启服务后第一句慢几秒。
