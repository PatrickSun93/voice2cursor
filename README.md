# voice2cursor

Offline push-to-talk dictation for **macOS and Windows**. Hold a hotkey, speak,
release — the text appears at your cursor. Transcription runs locally on your
GPU; an optional pass through a local [Ollama](https://ollama.com) model cleans
up the transcript.

No web UI, no cloud, no audio leaves the machine.

```
  hold the hotkey  ──▶  mic  ──▶  Whisper (local GPU)  ──▶  paste at cursor
                                          │
                                  (optional) Ollama
                                   punctuation, filler removal
```

One package serves both hosts. Everything that differs is behind
`voice2cursor/backends/` and `voice2cursor/engines/`, so no other module asks
what platform it is on.

| | macOS | Windows |
|---|---|---|
| engine | [mlx-whisper](https://github.com/ml-explore/mlx-examples) (Apple Silicon GPU) | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CUDA) |
| backend | `backends/macos.py` | `backends/windows.py` |
| default hotkey | right Option (`alt_r`) | `ctrl+alt+space` |
| paste | Cmd+V | Ctrl+V |
| clipboard | `pbcopy` / `pbpaste` | pyperclip |
| runs as | menu-bar icon, from a launchd agent | tray icon |
| state | `~/.voice2cursor` | `%APPDATA%\voice2cursor` |
| autostart | LaunchAgent (`scripts/macos/install.sh`) | Startup shortcut (`scripts/windows/install-autostart.ps1`) |
| setup | `scripts/macos/install.sh` | `scripts/windows/setup.ps1` |

## Why it is built this way

**Ollama cannot do speech-to-text.** Its model library is text, vision, and
embedding models only — there is no ASR model in it, so you cannot hand it a
`.wav` and get a transcript. Whisper does the listening; Ollama only ever sees
text. That split is why there are two model settings in the tray menu.

**No ffmpeg dependency.** Audio is captured as raw PCM in-process with
`sounddevice` and handed to faster-whisper as a numpy array, so there is no
temp file and no ffmpeg install to go wrong.

**The microphone stream stays open on Windows.** Opening an `InputStream` on
demand was measured at 0.02 s on a USB mic array but **0.93 s on a Bluetooth
headset** — which silently ate the first word of every clip. So the stream is
held open and audio flows into a ring buffer; the hotkey just marks where to
start reading. That also buys a 0.25 s pre-roll, so speaking a fraction early
still works. While idle the buffer holds only the pre-roll, about 15 KB. The
cost is that Windows shows the microphone-in-use indicator whenever
voice2cursor is running.

**On macOS it opens per clip.** There an open Bluetooth microphone pins the
headset to its hands-free profile, so everything you listen to through it plays
at telephone quality for as long as voice2cursor runs, not just while you
dictate. The stream opens on the keypress and closes on release instead, with
no pre-roll. `keep_mic_open` in the config overrides the default on either
host — worth turning on for a wired or USB mic.

**Two engines, not one.** mlx-whisper runs on the Apple Silicon GPU and
faster-whisper on CUDA. Each one falls back to a slow CPU path on the other's
hardware, so picking a single engine would have made one of the two platforms
noticeably worse. `engines/` gives them one interface; `config.engine` is
`auto` by default and takes the host's preferred engine, falling back to the
other if it is not installed.

They disagree about more than speed. faster-whisper reports `no_speech_prob`
and `avg_logprob` per segment, which is what lets the transcript filter throw
away the stock phrases Whisper invents from near-silence ("Thank you.",
"Mahala"). An engine that reports neither returns `None`, which means *unknown*
and is treated as keep — discarding real speech is the worse failure.

**Tray on Windows, menu bar on macOS.** These are different program shapes,
not one with a flag: pystray wants its own loop and Tk wants the main thread,
and on a Mac pystray wants the main thread too, so the two cannot share a
process there. The macOS menu-bar item is drawn with AppKit directly instead
(`menubar.py`), wrapped around the same pipeline `--headless` runs. It needs no
`.app` bundle: a LaunchAgent runs inside the user's GUI session, so the launchd
agent can show one. `--tray` and `--headless` override the default on either
host; `--menubar` is macOS only.

**The CUDA DLLs need help.** CTranslate2 links cuBLAS and cuDNN dynamically,
and the pip wheels put them somewhere Windows does not search. `cuda_setup.py`
registers those directories before the first `import faster_whisper`. Without
it you get `Library cudnn64_9.dll is not found` and a silent drop to CPU.

## Setup

### macOS

Apple Silicon. Needs `python3` (the system one is fine) and, for the
transcript, about 1.6 GB of model download on first run.

```bash
git clone https://github.com/PatrickSun93/voice2cursor.git
cd voice2cursor
./scripts/macos/install.sh
```

That builds `.venv` with `--copies`, installs from `requirements.txt` (the
platform markers pick the mlx side), downloads the model, and writes a launchd
agent plus its wrapper into `~/.voice2cursor/`.

Then grant permissions — this is the step that actually catches people out.
macOS grants Accessibility and Input Monitoring **per executable**, and the
executable is the venv's python, not Terminal:

> System Settings → Privacy & Security → **Accessibility** *and* **Input
> Monitoring** → add `<repo>/.venv/bin/python3` to both.

Until both are granted the hotkey never fires and the paste silently does
nothing, with no error printed anywhere. `--doctor` checks it for you.

```bash
./scripts/macos/start.sh     # load the agent
./scripts/macos/stop.sh      # unload it
./scripts/macos/uninstall.sh # remove the agent; keeps recordings and config
```

The plist deliberately contains no `/Volumes` path: launchd kills a job with
`EX_CONFIG` if it finds one, so the plist points at `~/.voice2cursor/run.sh`
and the wrapper walks into the repo.

### Windows

NVIDIA GPU recommended; it runs on CPU without one, slower.

```powershell
git clone https://github.com/PatrickSun93/voice2cursor.git
cd voice2cursor
powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
```

This creates the `voice2cursor` conda env from `environment.yml`, then verifies
the three things that actually break on a fresh machine: the interpreter, CUDA
visibility through the pip-installed NVIDIA DLLs, and a usable input device.

#### If PowerShell is blocked

Double-click `scripts\windows\setup.vbs`, which finds conda's base
interpreter and runs `setup.py` — the same work, no PowerShell involved.

## Running

Naming no options picks this host's usual shape — a menu-bar icon on macOS, a
tray icon on Windows — and any of them can be forced:

```
python -m voice2cursor              # this host's default
python -m voice2cursor --menubar    # menu-bar icon (macOS)
python -m voice2cursor --tray       # tray icon (needs pystray + pillow)
python -m voice2cursor --headless   # no icon, logs to a file
python -m voice2cursor --doctor     # diagnostics, then exit
python -m voice2cursor --config     # print the config path
python -m voice2cursor --list-devices
```

A second copy refuses to start, because two listeners means every hotkey press
fires twice — which is exactly what happens when a debug run joins the
autostarted one. `--allow-multiple` overrides it.

### macOS

```bash
./scripts/macos/start.sh    # load the launchd agent
./scripts/macos/stop.sh     # unload it
tail -f ~/.voice2cursor/logs/voice2cursor.log
./.venv/bin/python3 -m voice2cursor   # or in the foreground
```

A microphone appears in the menu bar. Its shape is the status, and hovering
over it says the same in words:

| Icon | Meaning |
| --- | --- |
| Hourglass | Loading the model, for a few seconds after start |
| Microphone | Idle, ready |
| Red filled microphone | Recording |
| Waveform | Transcribing |
| Crossed-out microphone | Paused — the hotkey is ignored |
| Warning triangle | The last recording, transcription or paste failed; clears on the next success |

The menu shows the last transcript and up to ten recent ones (click one to copy
it again), pauses dictation, and switches sounds, clipboard restore, the
recording archive, Ollama polish, output mode, language and the record hotkey.
Changes apply at once and are saved to the config. **Quit** is a clean exit,
which the LaunchAgent does not relaunch — `start.sh` or the next login brings
it back.

### Windows

```powershell
.\scripts\windows\run.ps1          # with a console, so you can see logs
```

Double-click `scripts\windows\run-silent.vbs` to start it with no console
window. To have it start at login:

```powershell
.\scripts\windows\install-autostart.ps1            # add
.\scripts\windows\install-autostart.ps1 -Remove    # remove
```

Or, without PowerShell — same shortcut, same target, equally safe to re-run:

```
cscript //nologo scripts\windows\install-autostart.vbs           # add
cscript //nologo scripts\windows\install-autostart.vbs /remove   # remove
```

Both write `voice2cursor.lnk` into the per-user Startup folder pointing at
`wscript.exe "run-silent.vbs"`. No admin rights, no registry.

A microphone icon appears in the system tray. Its colour is the status:

| Colour | Meaning |
| --- | --- |
| Grey | Idle, ready |
| Red | Recording |
| Amber | Loading a model or transcribing |
| Purple | Error — hover the icon for the message |

## Hotkeys

| Hotkey | Action |
| --- | --- |
| right Option (macOS) / `Ctrl+Shift+Space` (Windows) | Hold to record, release to transcribe and paste |
| `Ctrl+Alt+P` | Polish the last transcript with Ollama and paste the result |
| `Ctrl+Alt+R` | Open the transcript window (raw beside polished) |

Prefer tapping to holding? Tray menu → **Hotkey mode** → *Tap to start, tap to
stop*. Clips shorter than `min_seconds` (0.35 s) are discarded, so a stray tap costs nothing.

On macOS the record key is the **right Option key** alone. A lone modifier is
the gesture push-to-talk actually wants, and the left Option key is untouched
so it can still type special characters — the two are told apart, which is why
`alt_r` in a config means right Option specifically while a bare `alt` means
either.

On Windows it is `Ctrl+Shift+Space`, not the more obvious `Ctrl+Alt+Space`,
because the **Claude desktop app already owns that one**. To find a combination
nothing else has claimed:

```powershell
.\scripts\windows\run.ps1 --scan-hotkeys
```

It checks your three configured hotkeys plus a list of candidates and reports
each as free or taken, asking Windows directly rather than guessing. One
caveat: it can only see hotkeys registered through the `RegisterHotKey` API, so
an app using a low-level keyboard hook stays invisible — "free" is a strong
hint, not a guarantee. Set `record_hotkey` in the config, then use **Reload
config file** in the tray menu.

## Tray menu (Windows)

- **Whisper model** — `tiny` · `base` · `small` · `medium` · `large-v3` ·
  `distil-large-v3`. Downloads on first use into `~/.cache/huggingface` and
  swaps without restarting. Larger is more accurate and slower; `small` is a
  good starting point and `distil-large-v3` gets close to `large-v3` accuracy
  at roughly half the cost.
- **Microphone** — any input device, or the system default.
- **Ollama polish** — pick the model, refresh the list after pulling a new one,
  or turn on *Auto-polish every transcript* to run it on every recording
  instead of on demand. With nothing chosen it uses the **smallest** installed
  model, not the first alphabetically: punctuating two sentences does not need
  a 26B model, and one that exceeds your VRAM spills to the CPU and turns a
  0.2 s polish into most of a minute.
- **Hotkey mode** — hold-to-talk or tap-to-toggle.
- **Output** — paste via `Ctrl+V`, type keystroke by keystroke (leaves the
  clipboard untouched), or copy to clipboard only.
- **Run diagnostics** — writes a full report and opens it in Notepad.

## Transcript window

Opens on `Ctrl+Alt+R`, or after every recording if you enable that in the menu.
Raw Whisper output on the left, the Ollama version on the right, both editable.
Buttons polish, copy either side, or paste the polished text at your cursor.

## Configuration

Settings live in the state directory — `~/.voice2cursor/config.json` on macOS,
`%APPDATA%\voice2cursor\config.json` on Windows — written whenever you change
something in the tray or menu-bar menu.

Settings from either pre-merge build are migrated the first time you run this
one: the Windows package's `%APPDATA%\SonicType\config.json` is copied across
with its vocabulary file, and the macOS script's `config.json` is translated
key by key (`hotkey` → `record_hotkey`, `auto_paste: false` → `output_mode:
"clipboard"`, the nested `ollama` block flattened). Nothing is deleted. The
macOS script never filtered segments, so a migrated macOS config keeps
`max_no_speech` and `min_avg_logprob` at `null` (off): replaying one user's
recordings through mlx-whisper, Mandarin that was spoken and transcribed
correctly scored an `avg_logprob` of -2.7 to -4.2 and would have been dropped.

A few options are only reachable by editing the file:

| Key | Notes |
| --- | --- |
| `language` | `null` autodetects; set `"en"` to skip detection and save a little time |
| `initial_prompt` | Seed vocabulary — names, jargon, acronyms Whisper keeps getting wrong. Capped at 224 tokens, roughly 60 terms |
| `vocabulary` | Correct known jargon in the transcript after decoding. Default `true`. See below |
| `vocabulary_path` | Empty → `vocabulary.json` next to `config.json` |
| `beam_size` | Default `5`. Lower is faster, higher is marginally more accurate |
| `vad_filter` | Voice activity detection, trims silence. Default `true` |
| `max_no_speech` | Drop a segment above this silence probability. Default `0.6` — raise toward `1.0` if real speech is being dropped, or `null` to turn it off |
| `min_avg_logprob` | Drop a segment below this confidence. Default `-1.0` — lower toward `-2.0` to keep more, or `null` to turn it off. Non-English speech can score far lower |
| `keep_mic_open` | Hold the microphone open between clips, for a 0.25 s pre-roll and no per-clip open delay. Default `true` on Windows, `false` on macOS, where it would keep a Bluetooth headset in its low-quality hands-free profile |
| `engine` | `auto` takes this host's preferred engine. Force with `"mlx_whisper"` or `"faster_whisper"` |
| `model_size` | A size token (`small`, `large-v3`) on either engine, or a Hugging Face repo id for mlx |
| `compute_type` | `auto` picks `float16` on GPU, `int8` on CPU. Ignored by mlx |
| `sounds` | Audible start / done / error cues. Default `true` |
| `save_recordings` | Keep every clip as a WAV with its transcript beside it. Default `false` — it grows without bound |
| `recordings_dir` | Empty → `recordings/` in the state directory |
| `min_seconds` / `max_seconds` | Shorter is treated as a mis-tap; longer is truncated, so a stuck key cannot fill the disk |
| `restore_clipboard` | Put your previous clipboard back after pasting. Default `true` |
| `polish_prompt` | The instruction sent to Ollama. Must contain `{text}` |
| `ollama_keep_alive` | How long Ollama holds the model in VRAM. Default `"30m"` — Ollama's own default of ~5 min means the first polish after a break pays a reload. Costs ~2.6 GB for a 3B model, so pair a big Whisper model with `"0"` |

Use **Reload config file** in the tray menu to pick up hand edits without
restarting.

## Spelling site jargon

Whisper has never heard your product names, so it guesses: FLIMS becomes
"films", Xifin becomes "zyphen", `dmn_autotransit` becomes "demon auto
transit". Two settings fix that, and they work at different layers.

`initial_prompt` biases the decoder itself, so the right word can win while
the audio is still being read. It is the better fix when it applies, but
Whisper caps it at **224 tokens** — about 60 terms — and anything past that is
silently dropped. It also only ever biases; it never guarantees.

`vocabulary.json` corrects the finished transcript instead, so it has no size
limit and a predictable result. Hundreds of panel names and daemon
identifiers go here.

```json
{
  "terms": ["FLIMS", "TempleCity", "dmn_autotransit", "json_actparams"],
  "aliases": {"zyphen": "Xifin", "activity phase": "activityphase"}
}
```

Every entry in `terms` is the **correct** spelling. Matching ignores case and
any spaces, hyphens or underscores between word parts, so the single entry
`TempleCity` also catches "temple city", "Temple-City" and "templecity" — you
never list the wrong spellings. `aliases` is only for phonetic misses, where
what Whisper heard shares no letters with the right word.

Corrections are applied before anything else sees the text, so the paste, the
transcript window, and the text handed to Ollama all agree. Edits to the file
take effect on the next dictation — no restart, no menu action. `--doctor`
reports how many spellings loaded.

A term that is also an ordinary English word will fire in ordinary sentences:
an alias mapping "films" to FLIMS is right in a support note and wrong in a
film review. Delete the line if it costs more than it saves.

## When something breaks

```powershell
.\scripts\windows\run.ps1 --doctor          # full diagnostic report
.\scripts\windows\run.ps1 --scan-hotkeys    # which global hotkeys other apps already own
.\scripts\windows\run.ps1 --list-devices    # available microphones
.\scripts\windows\run.ps1 --config          # where the config file lives
.\scripts\windows\run.ps1 --model tiny --device cpu   # one-off overrides, not saved
```

Without PowerShell, the same flags work off the env interpreter directly — the
`.ps1` only ever forwarded them:

```
%LOCALAPPDATA%\miniconda3\envs\voice2cursor\python.exe -m voice2cursor --doctor
```

`--doctor` checks platform, imports, CUDA, audio capture, model cache, hotkey
parsing, clipboard round-trip, and Ollama, and reports which layer failed.

**Nothing pastes.** Some windows run elevated (Task Manager, an admin console)
and refuse synthetic input from a non-elevated process. The text is still on
your clipboard. Or switch **Output** to *Copy to clipboard only*.

**Hotkey does nothing.** Another app probably owns it — run
`.\scripts\windows\run.ps1 --scan-hotkeys` to find out, then set `record_hotkey` to something
reported free. On this machine `Ctrl+Alt+Space` belongs to the Claude desktop
app, and `Ctrl+Win+Space` / `Win+Shift+Space` to Windows itself. Note too that
an elevated app will not see keystrokes from non-elevated windows, or the
reverse.

**Transcription is slow.** Check whether you are on CPU: hover the tray icon,
or run `--doctor`. CPU works but is several times slower; the usual cause is
missing NVIDIA wheels, which `setup.ps1` installs.

**"CUDA inference failed - restart with --device cpu".** A cuBLAS or cuDNN
library loaded but could not run. Once that happens the CUDA context is
unusable and reloading in the same process hangs rather than erroring, so
voice2cursor stops using the GPU for the rest of the session instead of retrying.
Restart it; if the message returns, run `.\scripts\windows\run.ps1 --device cpu` and reinstall
the NVIDIA wheels.

**A word appeared that I never said.** Whisper invents short stock phrases when
fed near-silence. Segments are dropped when the model is unsure — tune
`max_no_speech` and `min_avg_logprob` if it is either too eager or too strict.

**Words I did say went missing.** The same filter, too strict for your speech.
Lower `min_avg_logprob`, or set both thresholds to `null`. Mandarin in
particular scores well below the `-1.0` default.

**Polish does nothing.** Transcription is unaffected by Ollama being down — by
design, the raw text still gets pasted. Confirm `ollama serve` is running and
that you have pulled a model, then use **Refresh model list**.

## Layout

```
voice2cursor/
  __main__.py        CLI entry, registers CUDA DLLs before any import
  app.py             tray icon, hotkey wiring, pipeline orchestration (Windows default)
  headless.py        the pipeline with no UI of its own (--headless)
  menubar.py         AppKit menu-bar icon around that pipeline (macOS default)
  config.py          dataclass + JSON persistence + migration from both old builds
  audio.py           microphone capture, resample to 16 kHz mono
  transcriber.py     model lifecycle, confidence filter, vocabulary
  archive.py         WAV + transcript pairs, when save_recordings is on
  logging_setup.py   one rotating log file, plus stdout
  ollama_client.py   polish step, degrades gracefully when Ollama is absent
  hotkeys.py         press+release global hotkeys (pynput cannot do release)
  output.py          paste / type / clipboard delivery
  review_window.py   Tk raw-vs-polished window
  icons.py           tray icons drawn at runtime
  vocabulary.py      post-decode jargon correction
  doctor.py          diagnostics
  cuda_setup.py      puts the pip NVIDIA DLLs on the loader path (no-op off Windows)
  hotkey_scan.py     probes Win32 for hotkeys other apps already own
  backends/
    __init__.py      picks this host's backend; the only sys.platform check
    macos.py         Cmd+V, pbcopy, afplay, flock, TCC checks
    windows.py       Ctrl+V, pyperclip, MessageBeep, named mutex, hotkey checks
  engines/
    base.py          the contract: segments, optional confidence
    mlx_whisper_engine.py      Apple Silicon GPU
    faster_whisper_engine.py   CUDA, with the CPU fallback and wedge latch
scripts/
  macos/     install.sh  start.sh  stop.sh  uninstall.sh
  windows/   setup.ps1  setup.py  setup.vbs  run.ps1  run-silent.vbs
             install-autostart.ps1  install-autostart.vbs
```

Adding a platform means one module under `backends/`, one line in
`backends/__init__.py`, and a script pair. Adding an engine means one module
under `engines/` and one line in its `__init__.py`.

Model weights live in `~/.cache/huggingface`; config and logs live in the state
directory. Neither is in the repo.
