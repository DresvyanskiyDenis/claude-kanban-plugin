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
# every flag the runner, `auth_probe`, setup's probes and the isolation flags pass; a typo fails loudly
VALUED = {
    "-p", "--agent", "--model", "--effort", "--permission-mode", "--permission-prompts", "--settings",
    "--mcp-config", "--output-format", "--name", "--max-budget-usd", "--max-turns", "--resume",
    "--session-id", "--tools", "--setting-sources", "--plugin-dir",
}  # fmt: skip
SWITCHES = {"--verbose", "--no-chrome", "--no-session-persistence", "--strict-mcp-config"}


def unknown_flag(argv: list[str]) -> str | None:
    """The first flag that is not in the allowed set, skipping the value of a valued flag."""
    skip = False
    for arg in argv:
        if skip:
            skip = False
        elif arg in VALUED:
            skip = True
        elif arg.startswith("-") and arg not in SWITCHES:
            return arg
    return None


def main() -> None:
    argv = sys.argv[1:]
    # a probe runs under a scrubbed environment, so knobs may also sit in a file beside the script
    conf = HERE / "claude.json"
    env = {**os.environ, **(json.loads(conf.read_text()) if conf.exists() else {})}
    if argv == ["--version"]:
        print(env.get("FAKE_CLAUDE_VERSION", "2.1.286 (Claude Code)"))
        return
    if bad := unknown_flag(argv):
        sys.exit(f"error: unknown option '{bad}'")
    if log := env.get("FAKE_CLAUDE_LOG"):
        with open(log, "a") as f:
            f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "env": dict(os.environ)}) + "\n")
    if out := env.get("FAKE_CLAUDE_OUT"):
        dump = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(env)}
        Path(out, f"{env.get('KANBAN_RUN_ID', 'x')}.json").write_text(json.dumps(dump))
    if pidfile := env.get("FAKE_CLAUDE_GRANDCHILD"):
        Path(pidfile).write_text(str(subprocess.Popen(["sleep", "300"]).pid))
    fixtures = Path(env.get("FAKE_CLAUDE_FIXTURES", HERE / "fixtures"))
    name = env.get("FAKE_CLAUDE_FIXTURE", "success")
    if "--agent" in argv:  # FAKE_CLAUDE_FIXTURE_AGENT answers only an `--agent` call
        name = env.get("FAKE_CLAUDE_FIXTURE_AGENT", name)
        if name == "no_agent":  # the real CLI refuses an unknown agent before any event, on stderr
            agent = argv[argv.index("--agent") + 1]
            sys.exit(f"--agent '{agent}' not found. Available agents: general-purpose")
    if "--model" in argv:  # FAKE_CLAUDE_FIXTURE_OPUS answers only `--model opus`
        name = env.get(f"FAKE_CLAUDE_FIXTURE_{argv[argv.index('--model') + 1].upper()}", name)
    text = (fixtures / f"{name}.jsonl").read_text()
    lines = text.replace("__COST__", env.get("FAKE_CLAUDE_COST", "0.12")).splitlines()
    if argv[-2:] == ["--output-format", "json"]:  # one result object, as `-p` prints it
        lines = lines[-1:]
    for line in lines:
        print(line, flush=True)
    if verb := env.get("FAKE_CLAUDE_TERMINAL"):
        subprocess.run([sys.executable, "-m", "kanban_claude", *json.loads(verb)], check=True)
    time.sleep(float(env.get("FAKE_CLAUDE_SLEEP", "0")))
    sys.exit(int(env.get("FAKE_CLAUDE_EXIT", "0")))


if __name__ == "__main__":
    main()
