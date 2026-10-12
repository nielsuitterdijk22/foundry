"""Terminal feedback: a live status line (TTY only) and line printing that doesn't clobber it."""

import sys
import threading
import time

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DIM, CYAN, RESET, CLEAR = "\033[2m", "\033[1;36m", "\033[0m", "\r\033[2K"
TTY = sys.stderr.isatty()


class Status:
    """A spinner line on stderr: `⠋ <label> · <detail> · 12s`, optionally with a pinned panel of
    lines above it (panel: a callable returning them). Everything printed through print() scrolls
    above the panel. No-op when stderr is not a TTY."""

    PANEL_EVERY = 1.0  # seconds between panel refreshes; the spinner line redraws every tick

    def __init__(self, label: str, panel=None):
        self.label, self.detail = label, ""
        self.panel = panel
        self.start = time.time()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._n = 0              # lines currently on screen
        self._panel_lines: list[str] = []
        self._panel_at = 0.0
        if TTY:
            threading.Thread(target=self._spin, daemon=True).start()

    @property
    def _shown(self) -> bool:
        return self._n > 0

    def _spin(self):
        i = 0
        while not self._stop.wait(0.1):
            with self._lock:
                self._draw(FRAMES[i % len(FRAMES)])
            i += 1

    def _clear_seq(self) -> str:
        return ("\r\033[2K" + "\033[1A\033[2K" * (self._n - 1)) if self._n else ""

    def _spinner(self, frame: str) -> str:
        secs = int(time.time() - self.start)
        elapsed = f"{secs // 60}:{secs % 60:02d}" if secs >= 60 else f"{secs}s"
        parts = [self.label, self.detail, elapsed]
        return f"{DIM}{frame} {' · '.join(p for p in parts if p)}{RESET}"

    def _draw(self, frame: str):
        from .view import fit
        now = time.time()
        if self.panel and (now - self._panel_at >= self.PANEL_EVERY or not self._n):
            try:
                self._panel_lines = [fit(ln) for ln in self.panel()]
            except Exception:  # noqa: BLE001 — a broken panel must not break the work
                self._panel_lines = []
            self._panel_at = now
            lines = self._panel_lines + [fit(self._spinner(frame))]
            sys.stderr.write(self._clear_seq() + "\n".join(lines))
            self._n = len(lines)
        else:  # only the spinner line (the cursor sits at its end)
            sys.stderr.write("\r\033[2K" + fit(self._spinner(frame)))
            self._n = max(self._n, 1)
        sys.stderr.flush()

    def update(self, detail: str):
        self.detail = detail

    def _erase(self):
        if self._n:
            sys.stderr.write(self._clear_seq())
            sys.stderr.flush()
            self._n = 0

    def print(self, text: str):
        """Print lines above the pinned panel / status line."""
        with self._lock:
            self._erase()
            print(text, flush=True)

    def stop(self):
        self._stop.set()
        with self._lock:
            self._erase()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


class StreamPrinter:
    """Callback for llm.stream: shows a status line until text arrives, then streams it.

    reasoning text streams dim; content streams in `style` (or is hidden when hide_content,
    e.g. JSON the caller will render nicely afterwards — then a token counter shows instead).
    """

    def __init__(self, label: str, style: str = "", hide_content: bool = False, dim_content: bool = False):
        self.status = Status(label)
        self.style = DIM if dim_content else style
        self.hide = hide_content
        self.mode = None  # None | "reasoning" | "content"
        self.tokens = 0
        self.t0 = time.time()

    def __call__(self, kind: str, text: str):
        self.tokens += 1
        rate = self.tokens / max(time.time() - self.t0, 0.1)
        self.status.update(f"{'thinking' if kind == 'reasoning' else 'writing'} {self.tokens} tok, {rate:.0f} tok/s")
        if kind == "content" and self.hide:
            return
        if self.mode != kind:
            self.status.stop()
            if self.mode is not None:
                sys.stdout.write(RESET + "\n")
            sys.stdout.write(DIM + "thinking: " if kind == "reasoning" else self.style)
            self.mode = kind
        sys.stdout.write(text)
        sys.stdout.flush()

    def done(self):
        self.status.stop()
        if self.mode is not None:
            sys.stdout.write(RESET + "\n")
            sys.stdout.flush()
