"""foundry bench — raw speed + one fixed agentic task, appended to bench/results.csv.

1. Prefill and decode tokens/s, measured directly against the server.
2. The agent (OpenCode in the sandbox) implements bench/task/TASK.md; then the hidden
   tests in bench/hidden/ are copied in and `go vet` + `go test -race` decide pass/fail.
"""

import csv
import random
import re
import shutil
import subprocess
import time
from datetime import datetime

from . import agent, config, git, llm, sandbox, serve

PROMPT = """Read TASK.md and implement it completely, including your own tests.
Run `go vet ./...` and `go test -race ./...` and fix everything until both pass.
Work alone; never ask questions."""


def speed(cfg) -> tuple[float, float]:
    words = " ".join(random.choice(["alpha", "beta", "func", "return", "struct", "int", "map"])
                     for _ in range(4000))
    t = time.time()
    r = llm.chat(cfg, [{"role": "user", "content": words + "\nReply with: ok"}], thinking=False, max_tokens=1)
    prefill = r["usage"]["prompt_tokens"] / (time.time() - t)
    t = time.time()
    r = llm.chat(cfg, [{"role": "user", "content": "Count from 1 to 400, comma separated."}],
                 thinking=False, max_tokens=256, temperature=0.0)
    decode = r["usage"]["completion_tokens"] / (time.time() - t)
    return prefill, decode


def main(cfg, args) -> int:
    if not serve.healthy(cfg):
        raise SystemExit("model server is not running — `foundry serve start`")
    print(f"model: {cfg['model']['id']} (thinking={'on' if cfg['model']['thinking'] else 'off'})")
    prefill, decode = speed(cfg)
    print(f"speed: prefill {prefill:.0f} tok/s, decode {decode:.1f} tok/s")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = config.state_dir(cfg, "bench") / f"run-{stamp}"
    shutil.copytree(config.ROOT / "bench" / "task", work)
    git.git(work, "init", "-q", "-b", "main")
    git.git(work, "add", "-A")
    git.git(work, "-c", "user.name=foundry", "-c", "user.email=foundry@localhost",
            "commit", "-q", "-m", "task")
    sandbox.ensure_proxy(cfg)
    agent.keep_awake()
    print(f"agent task running in {work} (cap {cfg['run']['task_timeout']} min)...", flush=True)
    res = agent.run(cfg, work, PROMPT, name="foundry-bench", log=work / "agent.jsonl",
                    timeout_min=cfg["run"]["task_timeout"], token_budget=cfg["run"]["task_token_budget"],
                    label="bench")
    if not serve.healthy(cfg):
        print(f"model server crashed during the task — see {config.state_dir(cfg) / 'server.log'}; "
              "restart with `foundry serve restart` and rerun")
        return 2
    for f in (config.ROOT / "bench" / "hidden").iterdir():
        (work / "lru").mkdir(exist_ok=True)
        shutil.copy(f, work / "lru" / f.name)
    argv = sandbox.run_cmd(cfg, work, ["bash", "-c", "go vet ./... && "
                                       "go test -race -count=1 -v -run Hidden ./lru/"],
                           name="foundry-bench-check")
    r = subprocess.run(argv, capture_output=True, text=True)
    out = r.stdout + r.stderr
    (work / "hidden-tests.log").write_text(out)
    total = len(re.findall(r"^func TestHidden", (config.ROOT / "bench" / "hidden" /
                                                    "lru_hidden_test.go").read_text(), re.M))
    hidden_passed = len(re.findall(r"--- PASS: TestHidden", out))
    passed = r.returncode == 0 and hidden_passed == total

    row = {
        "date": datetime.now().isoformat(timespec="minutes"), "model": cfg["model"]["id"],
        "thinking": cfg["model"]["thinking"], "prefill_tok_s": round(prefill),
        "decode_tok_s": round(decode, 1), "task": "pass" if passed else "fail",
        "hidden_tests": f"{hidden_passed}/{total}",
        "minutes": round(res.seconds / 60, 1), "steps": res.steps,
        "tokens_in": res.tokens_in, "tokens_out": res.tokens_out, "stopped": res.stopped or "-",
    }
    results = config.ROOT / "bench" / "results.csv"
    new = not results.exists()
    with open(results, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)
    print(f"task : {'PASS' if passed else 'FAIL'} ({hidden_passed}/{total} hidden tests) in {row['minutes']} min, {res.steps} steps, "
          f"{res.tokens_in:,} in / {res.tokens_out:,} out" + (f", stopped: {res.stopped}" if res.stopped else ""))
    if not passed:
        print("\n".join(out.splitlines()[-15:]))
    print(f"logged to {results}; workdir {work}")
    return 0 if passed else 1
