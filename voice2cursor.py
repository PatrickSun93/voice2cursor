#!/usr/bin/env python3
"""voice2cursor — 按住热键说话，松开自动转文字并粘贴到当前光标处。

流程: 按住右 Option → 录音 → 松开 → mlx-whisper 本地转写(中英混说) → 复制剪贴板 → Cmd+V 粘贴
可选: Ollama 后处理(补标点/修中英混排)，config.json 里 ollama.enabled 开关，默认关。
留档: 每次录音存 WAV + 同名 .txt 转写到 ~/.voice2cursor/recordings/（save_recordings 开关，默认开）。

常驻方式: launchd LaunchAgent(见 install.sh)。日志在 ~/.voice2cursor/logs/。
"""
import fcntl
import json
import logging
import logging.handlers
import os
import queue
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime

import numpy as np
import sounddevice as sd
from pynput import keyboard

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.expanduser("~/.voice2cursor")
LOG_DIR = os.path.join(STATE_DIR, "logs")
DEFAULT_RECORDINGS_DIR = os.path.join(STATE_DIR, "recordings")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

SAMPLE_RATE = 16000

def resolve_hotkey(name):
    """把 config 里的热键名解析成 pynput 按键。

    支持三种写法:
      - pynput 命名键: alt_r / cmd_r / ctrl_r / shift_r / f13 ... (keyboard.Key 的任意成员名)
      - 原始键码: "vk:105" (任意物理键, 用 --keytest 模式查自己想用的键的码)
      - 单字符: "§" 之类可打印键(不推荐, 打字会误触)
    """
    if name.startswith("vk:"):
        return keyboard.KeyCode.from_vk(int(name.split(":", 1)[1]))
    key = getattr(keyboard.Key, name, None)
    if key is not None:
        return key
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)
    return None


def key_vk(key):
    """取按键的原始键码, Key 枚举和 KeyCode 都兼容。"""
    if isinstance(key, keyboard.Key):
        return key.value.vk
    return getattr(key, "vk", None)


def key_matches(key, target):
    if key == target:
        return True
    kv, tv = key_vk(key), key_vk(target)
    return kv is not None and kv == tv

SOUND_START = "/System/Library/Sounds/Tink.aiff"
SOUND_DONE = "/System/Library/Sounds/Pop.aiff"
SOUND_ERROR = "/System/Library/Sounds/Basso.aiff"


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    log = logging.getLogger("voice2cursor")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "voice2cursor.log"),
        maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


log = setup_logging()


def acquire_single_instance_lock():
    """单实例锁，防止 launchd 和手动前台调试同时跑导致热键双触发。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    lock_file = open(os.path.join(STATE_DIR, "lock"), "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("已有另一个 voice2cursor 实例在运行（launchd？），本进程退出。")
        sys.exit(1)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file  # 持有引用防 GC 释放锁


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def play_sound(path, enabled):
    if enabled:
        subprocess.Popen(["afplay", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def copy_to_clipboard(text):
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)


def save_wav(path, audio):
    """float32 [-1,1] 单声道 → 16kHz 16bit PCM WAV。标准库 wave，不加依赖。"""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


def save_transcript(wav_path, text):
    """转写文字存成和 WAV 同名的 .txt，方便按时间对照回听。"""
    txt_path = os.path.splitext(wav_path)[0] + ".txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return txt_path


def paste_frontmost():
    """在前台 app 的当前光标处 Cmd+V。需要给本 python 授「辅助功能」权限。"""
    time.sleep(0.1)  # 确保修饰键已全部松开，避免 Cmd+V 变 Cmd+Opt+V
    subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to keystroke "v" using command down'],
        check=True, capture_output=True)


def ollama_cleanup(text, cfg):
    """可选的 Ollama 后处理。失败时原样返回，绝不吞掉听写结果。"""
    import urllib.request
    prompt = (
        "你是语音听写文本的清理器。修正下面文本的标点符号和中英文混排格式"
        "（中英文之间加空格、英文专有名词大小写），不改变内容和语序，"
        "直接输出修正后的文本，不要任何解释：\n\n" + text
    )
    payload = json.dumps({
        "model": cfg["model"], "prompt": prompt, "stream": False,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            cfg["url"], data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 15)) as resp:
            out = json.loads(resp.read())["response"].strip()
            return out if out else text
    except Exception as e:
        log.warning("Ollama 后处理失败，使用原始转写: %s", e)
        return text


class Recorder:
    def __init__(self):
        self.frames = []
        self.stream = None

    def _open_stream(self):
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            callback=lambda indata, *_: self.frames.append(indata.copy()))
        self.stream.start()

    def start(self):
        self.frames = []
        try:
            self._open_stream()
        except sd.PortAudioError:
            # PortAudio 只在进程启动时扫一次设备，常驻期间蓝牙耳机增减/
            # 睡眠唤醒后设备列表过期，开流报 -9986。重置后设备列表刷新，重试一次。
            log.warning("开流失败，重置 PortAudio 后重试（设备变化？）")
            sd._terminate()
            sd._initialize()
            self._open_stream()

    def stop(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        if not self.frames:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(self.frames)[:, 0]
        self.frames = []
        return audio


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.hotkey = resolve_hotkey(cfg["hotkey"])
        if self.hotkey is None:
            log.error("未知热键 %r。可用: keyboard.Key 成员名(alt_r/cmd_r/shift_r/f13...)、"
                      "\"vk:键码\"(用 --keytest 查)、或单字符", cfg["hotkey"])
            sys.exit(1)
        self.recorder = Recorder()
        self.recording = False
        self.q = queue.Queue()
        self.mlx_whisper = None
        self.rec_dir = None
        if cfg.get("save_recordings", True):
            self.rec_dir = os.path.expanduser(
                cfg.get("recordings_dir") or DEFAULT_RECORDINGS_DIR)
            os.makedirs(self.rec_dir, exist_ok=True)

    def save_recording(self, audio):
        """录音留档。失败只记日志，绝不影响后面的转写/粘贴。返回 WAV 路径或 None。"""
        if self.rec_dir is None:
            return None
        stem = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.rec_dir, stem + ".wav")
        n = 1
        while os.path.exists(path):  # 同一秒内两段录音，加序号防覆盖
            n += 1
            path = os.path.join(self.rec_dir, f"{stem}_{n}.wav")
        try:
            save_wav(path, audio)
            return path
        except Exception:
            log.exception("录音存盘失败: %s", path)
            return None

    # ---- whisper ----
    def load_model(self):
        import mlx_whisper
        self.mlx_whisper = mlx_whisper
        if self.cfg.get("preload_model", True):
            log.info("预热模型 %s ...", self.cfg["model"])
            t0 = time.time()
            mlx_whisper.transcribe(
                np.zeros(SAMPLE_RATE, dtype=np.float32),
                path_or_hf_repo=self.cfg["model"])
            log.info("模型就绪 (%.1fs)", time.time() - t0)

    def transcribe(self, audio):
        opts = {}
        if self.cfg.get("language"):
            opts["language"] = self.cfg["language"]
        if self.cfg.get("initial_prompt"):
            opts["initial_prompt"] = self.cfg["initial_prompt"]
        opts["condition_on_previous_text"] = self.cfg.get(
            "condition_on_previous_text", False)
        result = self.mlx_whisper.transcribe(
            audio, path_or_hf_repo=self.cfg["model"], **opts)
        return result["text"].strip()

    # ---- 转写 worker（串行，避免连说两段时重叠粘贴） ----
    def worker(self):
        while True:
            audio, wav_path = self.q.get()
            try:
                t0 = time.time()
                text = self.transcribe(audio)
                if not text:
                    log.info("空转写结果，忽略")
                    play_sound(SOUND_ERROR, self.cfg.get("sounds", True))
                    continue
                if self.cfg.get("ollama", {}).get("enabled"):
                    text = ollama_cleanup(text, self.cfg["ollama"])
                if wav_path:
                    try:
                        save_transcript(wav_path, text)
                    except Exception:
                        log.exception("转写文字存盘失败: %s", wav_path)
                copy_to_clipboard(text)
                if self.cfg.get("auto_paste", True):
                    paste_frontmost()
                play_sound(SOUND_DONE, self.cfg.get("sounds", True))
                log.info("[%.1fs音频 %.1fs转写] %s",
                         len(audio) / SAMPLE_RATE, time.time() - t0, text)
                if wav_path:
                    log.info("已留档: %s (+.txt)", wav_path)
            except Exception:
                log.exception("转写/粘贴失败")
                play_sound(SOUND_ERROR, self.cfg.get("sounds", True))

    # ---- 热键 ----
    def on_press(self, key):
        if key_matches(key, self.hotkey) and not self.recording:
            self.recording = True
            try:
                self.recorder.start()
                play_sound(SOUND_START, self.cfg.get("sounds", True))
            except Exception:
                self.recording = False
                log.exception("录音启动失败（麦克风权限？）")
                play_sound(SOUND_ERROR, self.cfg.get("sounds", True))

    def on_release(self, key):
        if key_matches(key, self.hotkey) and self.recording:
            self.recording = False
            audio = self.recorder.stop()
            dur = len(audio) / SAMPLE_RATE
            if dur < self.cfg.get("min_seconds", 0.35):
                log.info("录音过短 (%.2fs)，忽略（误触？）", dur)
                return
            wav_path = self.save_recording(audio)  # 存完整音频，截断只影响转写
            max_s = self.cfg.get("max_seconds", 300)
            if dur > max_s:
                log.warning("录音 %.0fs 超上限，截取前 %ds", dur, max_s)
                audio = audio[: max_s * SAMPLE_RATE]
            self.q.put((audio, wav_path))

    def run(self):
        self.load_model()
        threading.Thread(target=self.worker, daemon=True).start()
        log.info("voice2cursor 就绪：按住 %s 说话，松开出字。", self.cfg["hotkey"])
        if self.rec_dir:
            log.info("录音留档目录: %s", self.rec_dir)
        with keyboard.Listener(
                on_press=self.on_press, on_release=self.on_release) as listener:
            listener.join()


def keytest():
    """按键探测: 按任意键, 打印它在 config.json 里应该写的名字。Ctrl+C 退出。"""
    print("按你想当热键的键(修饰键、F 键、旋钮键都行), Ctrl+C 退出。")
    print("注意: 从终端跑时, 终端 app 需要有「输入监听」权限, 否则按了没反应。\n")

    def show(key):
        if isinstance(key, keyboard.Key):
            vk = key.value.vk
            extra = f'  (等价写法 "vk:{vk}")' if vk is not None else ""
            print(f'识别到 {key!s:<16} → config.json 写 "{key.name}"{extra}')
        else:
            print(f'识别到 char={key.char!r} vk={key.vk} → config.json 写 "vk:{key.vk}"')

    with keyboard.Listener(on_press=show) as listener:
        listener.join()


def main():
    if "--keytest" in sys.argv:
        keytest()
        return
    lock = acquire_single_instance_lock()  # noqa: F841 持有到进程结束
    cfg = load_config()
    App(cfg).run()


if __name__ == "__main__":
    main()
