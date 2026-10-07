"""Terminal feedback: a live status line (TTY only) and line printing that doesn't clobber it."""

import sys
import threading
import time

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DIM, CYAN, RESET, CLEAR = "\033[2m", "\033[1;36m", "\033[0m", "\r\033[2K"
TTY = sys.stderr.isatty()


class Status:
    """A spinner line on stderr: `⠋ <label> · <detail> · 12s`. No-op when not a TTY."""

    def __init__(self, label: str):
        self.label, self.detail = label, ""
        self.start = time.time()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._shown = False
        if TTY:
            threading.Thread(target=self._spin, daemon=True).start()

    def _spin(self):
        i = 0
        while not self._stop.wait(0.1):
            with self._lock:
                self._draw(FRAMES[i % len(FRAMES)])
            i += 1

    def _draw(self, frame: str):
        secs = int(time.time() - self.start)
        elapsed = f"{secs // 60}:{secs % 60:02d}" if secs >= 60 else f"{secs}s"
        parts = [self.label, self.detail, elapsed]
        sys.stderr.write(f"{CLEAR}{DIM}{frame} {' · '.join(p for p in parts if p)}{RESET}")
        sys.stderr.flush()
        self._shown = True

    def update(self, detail: str):
        self.detail = detail

    def print(self, text: str):
        """Print a full line above the status line."""
        with self._lock:
            if self._shown:
                sys.stderr.write(CLEAR)
                sys.stderr.flush()
                self._shown = False
            print(text, flush=True)

    def stop(self):
        self._stop.set()
        with self._lock:
            if self._shown:
                sys.stderr.write(CLEAR)
                sys.stderr.flush()
                self._shown = False

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
