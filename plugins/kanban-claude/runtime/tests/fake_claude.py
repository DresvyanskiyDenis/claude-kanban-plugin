#!python
"""Stands in for `claude` in the tests: replays a stream-json fixture, then optionally calls the
real `kanban` CLI (the worker's terminal verb) and lingers. Everything is driven by FAKE_CLAUDE_*."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    env = os.environ
    if out := env.get("FAKE_CLAUDE_OUT"):
        dump = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(env)}
        Path(out, f"{env.get('KANBAN_RUN_ID', 'x')}.json").write_text(json.dumps(dump))
    if pidfile := env.get("FAKE_CLAUDE_GRANDCHILD"):
        Path(pidfile).write_text(str(subprocess.Popen(["sleep", "300"]).pid))
    fixtures = Path(env.get("FAKE_CLAUDE_FIXTURES", HERE / "fixtures"))
    text = (fixtures / f"{env.get('FAKE_CLAUDE_FIXTURE', 'success')}.jsonl").read_text()
    for line in text.replace("__COST__", env.get("FAKE_CLAUDE_COST", "0.12")).splitlines():
        print(line, flush=True)
    if verb := env.get("FAKE_CLAUDE_TERMINAL"):
        subprocess.run([sys.executable, "-m", "kanban_claude", *json.loads(verb)], check=True)
    time.sleep(float(env.get("FAKE_CLAUDE_SLEEP", "0")))
    sys.exit(int(env.get("FAKE_CLAUDE_EXIT", "0")))


if __name__ == "__main__":
    main()
