"""Process facts (DSP-02, RUN-08): liveness is pid plus start time; unknown is never dead."""

import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import procs
from kanban_claude.procs import Procs, start_time


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen[bytes]]:
    p = subprocess.Popen(["sleep", "60"])
    yield p
    p.kill()
    p.wait()


def canned(monkeypatch: pytest.MonkeyPatch, stdout: str = "", returncode: int = 0) -> None:
    def fake(argv: list[str], **_kw: Any) -> "subprocess.CompletedProcess[str]":
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(procs.subprocess, "run", fake)
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: None)


def test_the_start_time_of_a_live_process_is_its_start(sleeper: subprocess.Popen[bytes]) -> None:
    found = start_time(sleeper.pid)
    assert found is not None and abs(found - time.time()) < 5


def test_a_start_time_that_differs_by_more_than_a_second_is_another_process(
    sleeper: subprocess.Popen[bytes],
) -> None:
    started = start_time(sleeper.pid)
    assert started is not None
    p = Procs()
    assert [p.alive(sleeper.pid, started + d) for d in (-2, -1, 0, 1, 2)] == [
        False, True, True, True, False,
    ]  # fmt: skip


def test_a_zombie_is_not_alive() -> None:
    child = subprocess.Popen(["true"])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            ps = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child.pid)], capture_output=True, text=True
            )
            if ps.stdout.strip().startswith("Z"):
                break
            time.sleep(0.05)
        assert start_time(child.pid) is None
        assert not Procs().alive(child.pid, int(time.time()))
    finally:
        child.wait()


def test_a_missing_or_reserved_pid_is_not_alive() -> None:
    p = Procs()
    gone = subprocess.Popen(["true"])
    gone.wait()
    assert not p.alive(gone.pid, int(time.time()))
    assert not p.alive(None, 1) and not p.alive(0, 1) and not p.alive(1, 1)
    assert not p.alive(gone.pid, None)


def test_the_lstart_column_is_read_in_the_c_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    canned(monkeypatch, "S    Thu Oct  9 16:45:44 2026\n")
    expected = int(time.mktime(time.strptime("2026-10-09 16:45:44", "%Y-%m-%d %H:%M:%S")))
    assert start_time(4242) == expected


def test_ps_is_asked_in_the_c_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(argv: list[str], **kw: Any) -> "subprocess.CompletedProcess[str]":
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(procs.subprocess, "run", fake)
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: None)
    assert start_time(4242) is None and seen["env"]["LC_ALL"] == "C"


@pytest.mark.parametrize(
    "bad", ["S    Thursday the 9th\n", "S\n", "S    Thu Oct  9 99:99:99 2026\n"]
)
def test_unreadable_output_raises_so_the_caller_skips_instead_of_declaring_death(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    canned(monkeypatch, bad)
    with pytest.raises(ValueError):
        Procs().alive(4242, 1)


def test_a_ps_that_fails_for_another_reason_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    canned(monkeypatch, "", returncode=2)
    with pytest.raises(OSError):
        Procs().alive(4242, 1)


def test_a_ps_timeout_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(argv: list[str], **_kw: Any) -> None:
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(procs.subprocess, "run", hang)
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: None)
    with pytest.raises(subprocess.TimeoutExpired):
        Procs().alive(4242, 1)


def test_the_runner_is_spawned_without_the_daemons_directory_on_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def popen(argv: list[str], **_kw: Any) -> None:
        seen.append(argv)

    monkeypatch.setattr(procs.subprocess, "Popen", popen)
    Procs().spawn(7, tmp_path / "run.log")
    assert seen == [[sys.executable, "-P", "-m", "kanban_claude", "run", "7"]]
