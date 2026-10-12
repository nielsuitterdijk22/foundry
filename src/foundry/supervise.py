"""foundry supervise <suite> — keep a suite run going: restart it when it crashes, retry when it stops.

The suite run itself handles the model server, GitHub hiccups and stuck tasks. What it can't
handle is its own death (a bug, an OOM kill): this loop restarts it with a backoff, and after a
clean stop (nothing can start, everything planned out) tries again later, since new thoughts,
owner edits or a returning GitHub may unblock it. Every line is timestamped into
~/.foundry/logs/supervise-<suite>.log, so the run can be watched (and fixed) from elsewhere.
"""

import os
import signal
import subprocess
import sys
import time
from datetime import datetime

from . import config

CRASH_BACKOFF = [60, 120, 300, 900]  # seconds after the 1st, 2nd, 3rd, later consecutive crashes
IDLE_RETRY = 20 * 60                 # after a clean stop
HEALTHY_RUN = 30 * 60                # a run that lasted this long resets the crash count


def _held_by_other_run(cfg: dict, suite: str) -> int | None:
    """The pid of a live process holding one of the suite's repo locks, if any."""
    from . import scaffold
    for name in scaffold.suite_state(cfg, suite)["adopted"] or []:
        lock = config.state_dir(cfg, "locks") / f"{name.lower()}.lock"
        try:
            pid = int(lock.read_text())
            os.kill(pid, 0)
            return pid
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            continue
    return None


def main(cfg: dict, args: list[str]) -> int:
    if not args:
        sys.exit("usage: foundry supervise <suite>")
    suite = args[0]
    log_path = config.state_dir(cfg, "logs") / f"supervise-{suite.lower()}.log"
    crashes, child = 0, None

    def say(line: str):
        stamped = f"{datetime.now():%Y-%m-%d %H:%M:%S} {line.rstrip()}"
        print(stamped, flush=True)
        with open(log_path, "a") as f:
            f.write(stamped + "\n")

    def stop(*_):
        if child and child.poll() is None:
            child.send_signal(signal.SIGTERM)  # the run discards nothing: it keeps partial work
            child.wait()
        say("[supervise] stopped")
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    say(f"[supervise] watching suite {suite} (log: {log_path})")
    waiting = False
    while True:
        if holder := _held_by_other_run(cfg, suite):  # e.g. started by hand in a terminal: let it be
            if not waiting:
                say(f"[supervise] another run (pid {holder}) is working on the suite; taking over when it ends")
            waiting = True
            time.sleep(60)
            continue
        waiting = False
        started = time.time()
        child = subprocess.Popen([sys.executable, "-u", "-m", "foundry", "run", "--suite", suite],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=config.ROOT,
                                 env={**os.environ, "PYTHONPATH": str(config.ROOT / "src"), "PYTHONUNBUFFERED": "1"})
        for line in child.stdout:
            say(line)
        code = child.wait()
        lasted = time.time() - started
        if code == 0:
            crashes = 0
            say(f"[supervise] suite run stopped cleanly after {lasted / 60:.0f} min; retrying in {IDLE_RETRY // 60} min")
            time.sleep(IDLE_RETRY)
            continue
        crashes = 1 if lasted >= HEALTHY_RUN else crashes + 1
        wait = CRASH_BACKOFF[min(crashes, len(CRASH_BACKOFF)) - 1]
        say(f"[supervise] CRASH: suite run exited with {code} after {lasted / 60:.0f} min "
            f"(crash {crashes} in a row); restarting in {wait // 60} min")
        time.sleep(wait)
