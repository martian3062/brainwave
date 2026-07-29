"""Terminal output for the CLI: the ERAYA palette, in ANSI, in pure Python.

There is no `rich`, no `click`, no `typer` and no `colorama` in this project's
dependency list, and this module is why the CLI does not need them. Everything
below is stdlib.

Colour rules, so the output is readable everywhere it might be run:

* Truecolor escapes, disabled automatically when stdout is not a TTY, when
  `NO_COLOR` is set (the informal standard), when `TERM=dumb`, or when the user
  passes `--no-color`. A redirected run therefore produces clean text a judge can
  paste into an issue.
* Windows consoles need ENABLE_VIRTUAL_TERMINAL_PROCESSING switched on before
  they interpret escapes. The `ctypes` call below does that once and falls back
  to plain text if it fails, which is what happens on an old conhost.
* Colour is never the only carrier of meaning -- every status line also has a
  word (`ok`, `FAIL`, `skip`). Reading a payment trace should not depend on
  seeing red.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, TextIO

__all__ = ["Printer", "Palette", "supports_color", "STATUS_OK", "STATUS_FAIL", "STATUS_SKIP"]

STATUS_OK = "ok"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "skip"
STATUS_WARN = "warn"


@dataclass(frozen=True)
class Palette:
    """ERAYA's colours, as truecolor escape sequences.

    The hex values are the project palette; NiceGUI gets the same ones as CSS
    strings in `app/dashboard.py`, so the terminal and the web UI agree.
    """

    fg: str = "\x1b[38;2;251;244;242m"  # #fbf4f2
    cream: str = "\x1b[38;2;255;243;236m"  # #fff3ec
    accent: str = "\x1b[38;2;255;111;145m"  # #ff6f91
    deep: str = "\x1b[38;2;230;65;111m"  # #e6416f
    dim: str = "\x1b[38;2;150;125;140m"
    good: str = "\x1b[38;2;120;220;170m"
    bad: str = "\x1b[38;2;255;110;110m"
    warn: str = "\x1b[38;2;250;200;120m"
    bold: str = "\x1b[1m"
    reset: str = "\x1b[0m"


_NO_COLOR = Palette(*[""] * 10)


def supports_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if sys.platform == "win32":
        return _enable_windows_vt()
    return True


def _enable_windows_vt() -> bool:
    """Turn on VT escape interpretation for the current console.

    Windows Terminal has it on already; the legacy console does not, and without
    this every escape sequence is printed literally as `<-[38;2;...m`.
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class Printer:
    """Structured console output with an optional machine-readable mode.

    Every command takes `--json`. In that mode this object swallows all human
    output and the command prints exactly one JSON document, so the CLI can be
    piped into `jq` or asserted on in a test without parsing decorated text.
    """

    def __init__(
        self,
        *,
        color: bool | None = None,
        json_mode: bool = False,
        stream: TextIO | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        self.json_mode = json_mode
        enabled = supports_color(self.stream) if color is None else color
        self.c = Palette() if enabled else _NO_COLOR
        self._step = 0

    # -- primitives ---------------------------------------------------------

    def raw(self, text: str = "") -> None:
        if self.json_mode:
            return
        print(text, file=self.stream)

    def title(self, text: str, subtitle: str = "") -> None:
        c = self.c
        self.raw()
        self.raw(f"{c.bold}{c.cream}{text}{c.reset}")
        if subtitle:
            self.raw(f"{c.accent}{subtitle}{c.reset}")
        self.rule()

    def rule(self, width: int = 74) -> None:
        self.raw(f"{self.c.dim}{'-' * width}{self.c.reset}")

    def section(self, text: str) -> None:
        self.raw()
        self.raw(f"{self.c.accent}{text.upper()}{self.c.reset}")

    def kv(self, key: str, value: Any, *, width: int = 22, note: str = "") -> None:
        c = self.c
        tail = f"  {c.dim}{note}{c.reset}" if note else ""
        self.raw(f"  {c.dim}{key:<{width}}{c.reset}{c.fg}{value}{c.reset}{tail}")

    # -- protocol trace -----------------------------------------------------

    def step(self, label: str, detail: str = "") -> None:
        """One numbered stage of the 402 flow. The numbering is the trace."""
        self._step += 1
        c = self.c
        self.raw()
        self.raw(f"{c.deep}[{self._step}]{c.reset} {c.bold}{c.cream}{label}{c.reset}")
        if detail:
            self.raw(f"    {c.dim}{detail}{c.reset}")

    def detail(self, text: str) -> None:
        self.raw(f"    {self.c.dim}{text}{self.c.reset}")

    def line(self, text: str) -> None:
        self.raw(f"    {self.c.fg}{text}{self.c.reset}")

    # -- status -------------------------------------------------------------

    def ok(self, text: str) -> None:
        self.raw(f"  {self.c.good}{STATUS_OK:<5}{self.c.reset}{self.c.fg}{text}{self.c.reset}")

    def fail(self, text: str) -> None:
        self.raw(f"  {self.c.bad}{STATUS_FAIL:<5}{self.c.reset}{self.c.fg}{text}{self.c.reset}")

    def skip(self, text: str) -> None:
        self.raw(f"  {self.c.dim}{STATUS_SKIP:<5}{text}{self.c.reset}")

    def warn(self, text: str) -> None:
        self.raw(f"  {self.c.warn}{STATUS_WARN:<5}{self.c.reset}{self.c.fg}{text}{self.c.reset}")

    def banner(self, text: str) -> None:
        """A box that is hard to crop out of a screenshot. Used for the demo-data
        warning and for anything that spends money."""
        c = self.c
        width = 74
        self.raw()
        self.raw(f"{c.deep}{'=' * width}{c.reset}")
        for chunk in _wrap(text, width - 4):
            body = f"{chunk:<{width - 4}}"
            self.raw(f"{c.deep}| {c.reset}{c.cream}{body}{c.reset}{c.deep} |{c.reset}")
        self.raw(f"{c.deep}{'=' * width}{c.reset}")

    # -- payloads -----------------------------------------------------------

    def payload(self, label: str, obj: Any, *, indent: int = 4, limit: int = 0) -> None:
        """Print a wire payload verbatim. This is the point of `simulate`: the
        bytes an agent would actually put on the wire, not a paraphrase."""
        if self.json_mode:
            return
        c = self.c
        self.raw(f"    {c.accent}{label}{c.reset}")
        text = json.dumps(obj, indent=2, sort_keys=False, default=str)
        lines = text.splitlines()
        if limit and len(lines) > limit:
            lines = lines[:limit] + [f"... ({len(text.splitlines()) - limit} more lines)"]
        pad = " " * indent
        for row in lines:
            self.raw(f"{pad}{c.dim}{row}{c.reset}")

    def emit_json(self, obj: Any) -> None:
        """The single JSON document a `--json` run produces."""
        print(json.dumps(obj, indent=2, default=str), file=self.stream)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]
