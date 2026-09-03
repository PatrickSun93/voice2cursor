# SonicType

Offline push-to-talk dictation for Windows. Hold a hotkey, speak, release — the
text appears at your cursor. Transcription runs locally on your GPU via
[faster-whisper](https://github.com/SYSTRAN/faster-whisper); an optional pass
through a local [Ollama](https://ollama.com) model cleans up the transcript.

No web UI, no cloud, no audio leaves the machine.

```
  hold Ctrl+Shift+Space  ──▶  mic  ──▶  faster-whisper (CUDA)  ──▶  paste at cursor
                                                     │
                                             (optional) Ollama
                                              punctuation, filler removal
```

## Why it is built this way

**Ollama cannot do speech-to-text.** Its model library is text, vision, and
embedding models only — there is no ASR model in it, so you cannot hand it a
`.wav` and get a transcript. Whisper does the listening; Ollama only ever sees
text. That split is why there are two model settings in the tray menu.

**No ffmpeg dependency.** Audio is captured as raw PCM in-process with
`sounddevice` and handed to faster-whisper as a numpy array, so there is no
temp file and no ffmpeg install to go wrong.

**The microphone stream stays open.** Opening an `InputStream` on demand was
measured at 0.02 s on a USB mic array but **0.93 s on a Bluetooth headset** —
which silently ate the first word of every clip. So the stream is held open and
audio flows into a ring buffer; the hotkey just marks where to start reading.
That also buys a 0.25 s pre-roll, so speaking a fraction early still works.
While idle the buffer holds only the pre-roll, about 15 KB. The cost is that
Windows shows the microphone-in-use indicator whenever SonicType is running.

**The CUDA DLLs need help.** CTranslate2 links cuBLAS and cuDNN dynamically,
and the pip wheels put them somewhere Windows does not search. `cuda_setup.py`
registers those directories before the first `import faster_whisper`. Without
it you get `Library cudnn64_9.dll is not found` and a silent drop to CPU.

## Setup

Requires an existing conda (miniconda is fine) and, for the polish step,
Ollama. Everything else the setup script installs into an isolated env.

```powershell
cd C:\FlowDev\githubdevitems\sonictype
.\setup.ps1
```

This creates the `sonictype` conda env from `environment.yml`, then verifies
Python, CUDA visibility, and microphone enumeration. It is safe to re-run.

### If PowerShell is blocked

Some managed machines ban PowerShell by policy, which makes every `.ps1` here
unrunnable. `cscript`/`wscript` are not usually restricted, so there is a
parallel set of entry points that avoid PowerShell entirely:

| PowerShell | PowerShell-free equivalent |
| --- | --- |
| `.\setup.ps1` | double-click `setup.vbs`, or `cscript //nologo setup.vbs` |
| `.\install-autostart.ps1` | `cscript //nologo install-autostart.vbs` |
| `.\install-autostart.ps1 -Remove` | `cscript //nologo install-autostart.vbs /remove` |
| `.\run.ps1` | `%LOCALAPPDATA%\miniconda3\envs\sonictype\python.exe -m sonictype` |

`setup.vbs` is only a launcher for `setup.py`, which holds the real logic and
can be run directly with conda's **base** interpreter — not the `sonictype` env
(it may not exist yet) and never a bare `python`, which on some machines
resolves to a stale `C:\Python34`:

```
%LOCALAPPDATA%\miniconda3\python.exe setup.py
%LOCALAPPDATA%\miniconda3\python.exe setup.py --verify-only   # checks only, no conda solve
```

`--verify-only` re-runs the interpreter, CUDA, microphone, and Ollama checks
without touching the env — the fastest way to confirm a working install.

For the optional polish step:

```powershell
ollama pull llama3.2:3b
```

`llama3.2:3b` is a good default — about 2 GB, fast, and more than capable of
punctuating dictation. `qwen2.5:7b` is noticeably better at rewriting if you
would rather spend the VRAM.

## Running

```powershell
.\run.ps1              # with a console, so you can see logs and tracebacks
```

Double-click `run-silent.vbs` to start it with no console window. To have it
start at login:

```powershell
.\install-autostart.ps1            # add
.\install-autostart.ps1 -Remove    # remove
```

Or, without PowerShell — same shortcut, same target, and equally safe to re-run:

```
cscript //nologo install-autostart.vbs           # add
cscript //nologo install-autostart.vbs /remove   # remove
```

Both write `SonicType.lnk` into the per-user Startup folder pointing at
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
| `Ctrl+Shift+Space` | Hold to record, release to transcribe and paste |
| `Ctrl+Alt+P` | Polish the last transcript with Ollama and paste the result |
| `Ctrl+Alt+R` | Open the transcript window (raw beside polished) |

Prefer tapping to holding? Tray menu → **Hotkey mode** → *Tap to start, tap to
stop*. Clips shorter than 0.25 s are discarded, so a stray tap costs nothing.

The record key is `Ctrl+Shift+Space`, not the more obvious `Ctrl+Alt+Space`,
because the **Claude desktop app already owns that one**. To find a combination
nothing else has claimed:

```powershell
.\run.ps1 --scan-hotkeys
```

It checks your three configured hotkeys plus a list of candidates and reports
each as free or taken, asking Windows directly rather than guessing. One
caveat: it can only see hotkeys registered through the `RegisterHotKey` API, so
an app using a low-level keyboard hook stays invisible — "free" is a strong
hint, not a guarantee. Set `record_hotkey` in the config, then use **Reload
config file** in the tray menu.

## Tray menu

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

Settings live in `%APPDATA%\SonicType\config.json`, written whenever you change
something in the tray menu. A few options are only reachable by editing it:

| Key | Notes |
| --- | --- |
| `language` | `null` autodetects; set `"en"` to skip detection and save a little time |
| `initial_prompt` | Seed vocabulary — names, jargon, acronyms Whisper keeps getting wrong. Capped at 224 tokens, roughly 60 terms |
| `vocabulary` | Correct known jargon in the transcript after decoding. Default `true`. See below |
| `vocabulary_path` | Empty → `vocabulary.json` next to `config.json` |
| `beam_size` | Default `5`. Lower is faster, higher is marginally more accurate |
| `vad_filter` | Voice activity detection, trims silence. Default `true` |
| `max_no_speech` | Drop a segment above this silence probability. Default `0.6` — raise toward `1.0` if real speech is being dropped |
| `min_avg_logprob` | Drop a segment below this confidence. Default `-1.0` — lower toward `-2.0` to keep more |
| `compute_type` | `auto` picks `float16` on GPU, `int8` on CPU |
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
.\run.ps1 --doctor          # full diagnostic report
.\run.ps1 --scan-hotkeys    # which global hotkeys other apps already own
.\run.ps1 --list-devices    # available microphones
.\run.ps1 --config          # where the config file lives
.\run.ps1 --model tiny --device cpu   # one-off overrides, not saved
```

Without PowerShell, the same flags work off the env interpreter directly — the
`.ps1` only ever forwarded them:

```
%LOCALAPPDATA%\miniconda3\envs\sonictype\python.exe -m sonictype --doctor
```

`--doctor` checks platform, imports, CUDA, audio capture, model cache, hotkey
parsing, clipboard round-trip, and Ollama, and reports which layer failed.

**Nothing pastes.** Some windows run elevated (Task Manager, an admin console)
and refuse synthetic input from a non-elevated process. The text is still on
your clipboard. Or switch **Output** to *Copy to clipboard only*.

**Hotkey does nothing.** Another app probably owns it — run
`.\run.ps1 --scan-hotkeys` to find out, then set `record_hotkey` to something
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
SonicType stops using the GPU for the rest of the session instead of retrying.
Restart it; if the message returns, run `.\run.ps1 --device cpu` and reinstall
the NVIDIA wheels.

**A word appeared that I never said.** Whisper invents short stock phrases when
fed near-silence. Segments are dropped when the model is unsure — tune
`max_no_speech` and `min_avg_logprob` if it is either too eager or too strict.

**Polish does nothing.** Transcription is unaffected by Ollama being down — by
design, the raw text still gets pasted. Confirm `ollama serve` is running and
that you have pulled a model, then use **Refresh model list**.

## Layout

```
sonictype/
  __main__.py        CLI entry, registers CUDA DLLs before any import
  app.py             tray icon, hotkey wiring, pipeline orchestration
  config.py          dataclass + JSON persistence
  cuda_setup.py      puts the pip NVIDIA DLLs on the loader path
  audio.py           microphone capture, resample to 16 kHz mono
  transcriber.py     faster-whisper wrapper, hot model swapping
  ollama_client.py   polish step, degrades gracefully when Ollama is absent
  hotkeys.py         press+release global hotkeys (pynput cannot do release)
  output.py          paste / type / clipboard delivery
  review_window.py   Tk raw-vs-polished window
  icons.py           tray icons drawn at runtime
  hotkey_scan.py     probes Win32 for hotkeys other apps already own
  hotkey_scan.py     probes Win32 for hotkeys other apps already own
  doctor.py          diagnostics
```

Model weights live in `~/.cache/huggingface`, config in
`%APPDATA%\SonicType` — neither is in the repo.
