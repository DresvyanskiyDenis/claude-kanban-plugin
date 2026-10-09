"""Process facts (DSP-02, RUN-08): liveness by pid and start time, boot time, group signals."""

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from .paths import ensure_dir


def start_time(pid: int) -> int | None:
    """Epoch start time of a live, non-zombie process, or None when it is gone.

    A probe that itself fails (`ps` times out, errors or prints something unreadable) raises:
    the caller must treat that as "unknown", never as "dead".
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    env = {**os.environ, "LC_ALL": "C"}
    p = subprocess.run(
        ["ps", "-o", "stat=,lstart=", "-p", str(pid)],
        capture_output=True, text=True, env=env, timeout=10, check=False,
    )  # fmt: skip
    if p.returncode == 1 and not p.stdout.strip():
        return None
    if p.returncode != 0:
        raise OSError(f"ps failed ({p.returncode}): {p.stderr.strip()}")
    fields = p.stdout.strip().split(None, 1)
    if len(fields) != 2:
        raise ValueError(f"unreadable ps output {p.stdout!r}")
    if fields[0].startswith("Z"):
        return None
    return int(time.mktime(time.strptime(fields[1], "%a %b %d %H:%M:%S %Y")))


def boot_time() -> int:
    out = subprocess.run(
        ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=10, check=False
    ).stdout
    m = re.search(r"sec = (\d+)", out)
    return int(m[1]) if m else 0


def signal_group(pgid: int | None, sig: int) -> None:
    """Signal a whole process group; pgids 0 and 1 would hit our own or every group."""
    if pgid and pgid > 1:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, sig)


class Procs:
    """The process table as the dispatcher sees it; tests substitute a fake."""

    def alive(self, pid: int | None, started: int | None) -> bool:
        """A pid alone proves nothing (pid reuse); its start time must match within 1 s."""
        if not pid or pid < 2 or started is None:
            return False
        found = start_time(pid)
        return found is not None and abs(found - started) <= 1

    def boot_time(self) -> int:
        return boot_time()

    def killpg(self, pgid: int, sig: int = signal.SIGKILL) -> None:
        signal_group(pgid, sig)

    def spawn(self, run_id: int, log: Path) -> None:
        """DSP-08: a detached runner whose stdout and stderr land in the run log."""
        ensure_dir(log.parent)
        with log.open("ab") as out:
            subprocess.Popen(
                [sys.executable, "-P", "-m", "kanban_claude", "run", str(run_id)],
                start_new_session=True, stdin=subprocess.DEVNULL, stdout=out,
                stderr=subprocess.STDOUT, close_fds=True,
            )  # fmt: skip
