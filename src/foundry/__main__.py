"""foundry — local autonomous dev agents. See README.md."""

import sys

from . import config

USAGE = """usage: foundry <command> [args]

  serve start|stop|restart|status   manage the local model server
  bench                             run the fixed benchmark task, report tokens/s + pass/fail
  scaffold <name>                   interview → new private GitHub repo with all foundry files
  adopt <repo>                      bring an existing repo under foundry (interview seeded from it)
  cto <suite> [repo ...]            one interview for a suite of products, then unattended: adopts
                                    the repos that exist, creates the rest, starts the suite run.
                                    Naming more repos later adds them. --review: approve each document;
                                    --no-run: stop after setup
  tell <suite> [--now|--high|--med|--low] "…"
                                    drop a thought in the CTO's inbox; it decides when and how to act,
                                    within the priority's deadline: now / 1 day / 3 days (default) / 2 weeks.
                                    `tell <suite>` alone shows the inbox. Issues on foundry-showcase count too
  purge <name> [-y]                 forget a paused scaffold/adopt session
  purge suite <suite> [-y]          forget a suite (repos are left alone)
  run <repo>                        work the backlog unattended (Ctrl-C stops cleanly)
  run --suite <suite>               one loop across the suite's repos, respecting cross-repo Needs
  status                            what needs you, product status, inbox, latest work, what runs now
  report <repo>                     write REPORT.md for the daily check-in
"""


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = args[0], args[1:]
    cfg = config.load()
    if cmd == "serve":
        from . import serve
        return serve.main(cfg, rest)
    if cmd == "bench":
        from . import bench
        return bench.main(cfg, rest)
    if cmd in ("scaffold", "adopt"):
        from . import scaffold
        return scaffold.main(cfg, cmd, rest)
    if cmd == "cto":
        from . import scaffold
        return scaffold.cto(cfg, rest)
    if cmd == "tell":
        from . import inbox
        return inbox.tell(cfg, rest)
    if cmd == "triage":  # internal: tell runs it in the background
        from . import inbox
        return inbox.triage(cfg, rest)
    if cmd == "purge":
        from . import scaffold
        return scaffold.purge(cfg, rest)
    if cmd == "run":
        from . import run
        return run.main(cfg, rest)
    if cmd == "status":
        from . import showcase
        return showcase.status(cfg, rest)
    if cmd == "report":
        from . import report
        return report.main(cfg, rest)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
