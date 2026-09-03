"""Tk window showing the raw transcript beside the Ollama-polished version.

Owned by the main thread. Other threads must go through `App` which marshals
calls with `root.after`, since Tk is not thread-safe.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

_FONT = ("Segoe UI", 10)
_MONO = ("Consolas", 10)


class ReviewWindow:
    def __init__(
        self,
        root: tk.Tk,
        on_polish: Callable[[str], None],
        on_paste: Callable[[str], None],
    ):
        self.root = root
        self._on_polish = on_polish
        self._on_paste = on_paste
        self.win: tk.Toplevel | None = None
        self.raw_box: tk.Text | None = None
        self.polished_box: tk.Text | None = None
        self.status: tk.StringVar | None = None

    # ---------- construction ----------

    def _build(self) -> None:
        self.win = tk.Toplevel(self.root)
        self.win.title("SonicType - transcript")
        self.win.geometry("1000x520")
        self.win.minsize(640, 320)
        self.win.protocol("WM_DELETE_WINDOW", self.hide)

        panes = ttk.Panedwindow(self.win, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self.raw_box = self._pane(panes, "Raw (Whisper)")
        self.polished_box = self._pane(panes, "Polished (Ollama)")

        bar = ttk.Frame(self.win)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        self.status = tk.StringVar(value="")

        ttk.Button(bar, text="Polish with Ollama", command=self._polish).pack(side="left")
        ttk.Button(bar, text="Copy raw",
                   command=lambda: self._copy(self.raw_box)).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Copy polished",
                   command=lambda: self._copy(self.polished_box)).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Paste polished at cursor",
                   command=self._paste).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Close", command=self.hide).pack(side="right")
        ttk.Label(bar, textvariable=self.status, font=_FONT,
                  foreground="#666").pack(side="right", padx=(0, 12))

    def _pane(self, parent: ttk.Panedwindow, title: str) -> tk.Text:
        frame = ttk.Frame(parent)
        ttk.Label(frame, text=title, font=(_FONT[0], 9, "bold")).pack(anchor="w", pady=(0, 3))
        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        box = tk.Text(wrap, wrap="word", font=_MONO, undo=True,
                      relief="solid", borderwidth=1, padx=6, pady=6)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        box.pack(side="left", fill="both", expand=True)
        parent.add(frame, weight=1)
        return box

    # ---------- main-thread API ----------

    def show(self, raw: str | None = None, polished: str | None = None,
             status: str | None = None) -> None:
        if self.win is None or not self.win.winfo_exists():
            self._build()
        if raw is not None:
            _replace(self.raw_box, raw)
        if polished is not None:
            _replace(self.polished_box, polished)
        if status is not None:
            self.status.set(status)
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def set_polished(self, text: str, status: str = "") -> None:
        if self.win is None or not self.win.winfo_exists():
            return
        _replace(self.polished_box, text)
        self.status.set(status)

    def set_status(self, text: str) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.status.set(text)

    def hide(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.withdraw()

    # ---------- callbacks ----------

    def _text(self, box: tk.Text | None) -> str:
        return box.get("1.0", "end-1c") if box is not None else ""

    def _polish(self) -> None:
        raw = self._text(self.raw_box).strip()
        if not raw:
            self.status.set("nothing to polish")
            return
        self.status.set("polishing...")
        self._on_polish(raw)

    def _paste(self) -> None:
        text = self._text(self.polished_box).strip() or self._text(self.raw_box).strip()
        if text:
            self.hide()  # so the paste lands in the window you were using
            self._on_paste(text)

    def _copy(self, box: tk.Text | None) -> None:
        text = self._text(box)
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.set("copied")


def _replace(box: tk.Text | None, text: str) -> None:
    if box is None:
        return
    box.delete("1.0", "end")
    box.insert("1.0", text)
