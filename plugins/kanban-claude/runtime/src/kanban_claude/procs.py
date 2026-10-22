"""Process facts (DSP-02, RUN-08): liveness by pid and start time, boot time, group signals."""

import contextlib
import os
import re
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import service
from .paths import ensure_dir

MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )
}  # not calendar.month_abbr: that follows LC_TIME


def parse_proc_stat(line: str) -> tuple[str, int]:
    """The state (field 3) and `starttime` in clock ticks since boot (field 22) of a /proc/PID/stat line.
    `comm` (field 2) may hold spaces and parentheses, so the fields are counted after the last `)`."""
    rest = line.rpartition(")")[2].split()
    if len(rest) < 20:
        raise ValueError(f"unreadable /proc stat line {line!r}")
    return rest[0], int(rest[19])


def proc_start_time(pid: int) -> int | None:
    """Linux: the process's `starttime` plus the boot epoch, with no subprocess and no locale."""
    try:
        line = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    state, ticks = parse_proc_stat(line)
    if state.startswith("Z"):
        return None
    boot = boot_time()
    if not boot:
        raise OSError("no btime in /proc/stat")
    return boot + ticks // os.sysconf("SC_CLK_TCK")


def ps_start_time(pid: int) -> int | None:
    """macOS: `ps` prints `lstart` in English under LC_ALL=C; it is read by hand because `strptime`
    would use this process's own LC_TIME."""
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
    return parse_lstart(fields[1])


def parse_lstart(text: str) -> int:
    """`Thu Oct  9 16:45:44 2026` in local time, whatever this process's LC_TIME."""
    try:
        _, month, day, clock, year = text.split()
        hour, minute, second = clock.split(":")
        started = datetime(int(year), MONTHS[month], int(day), int(hour), int(minute), int(second))
        return int(started.timestamp())
    except (KeyError, ValueError, OverflowError) as e:
        raise ValueError(f"unreadable ps start time {text!r}") from e


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
    return ps_start_time(pid) if service.darwin() else proc_start_time(pid)


def boot_time() -> int:
    if not service.darwin():
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return int(line.split()[1])
        return 0
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
