"""TST-G: worktree provisioning, reuse, cleanup and snapshots (GIT-02..06, GIT-11, CHK-03), against real git."""

import fcntl
import threading
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import gitops
from kanban_claude.errors import Error
from kanban_claude.paths import dirs

from .conftest import git, push_commit

TID = "t_0123abcd"


def head(path: Path) -> str:
    return git("rev-parse", "HEAD", cwd=path)


def test_g1_a_new_branch_is_cut_from_origin_while_the_primary_checkout_is_behind(
    origin: Path, repo: Path, env: Path
) -> None:
    stale = head(repo)
    fresh = push_commit(env, "b.txt")
    path = gitops.provision(repo, TID, "feat/z", "main")
    assert path == repo / ".worktrees" / TID
    assert head(path) == fresh and head(repo) == stale
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "feat/z"


def test_g1_a_branch_that_already_exists_on_origin_is_tracked(
    origin: Path, repo: Path, env: Path
) -> None:
    other = env / "other"
    git("clone", str(origin), str(other), cwd=env)
    git("checkout", "-b", "feat/z", cwd=other)
    (other / "z.txt").write_text("z")
    git("add", "z.txt", cwd=other)
    git("commit", "-m", "z", cwd=other)
    git("push", "-u", "origin", "feat/z", cwd=other)
    path = gitops.provision(repo, TID, "feat/z", "main")
    assert head(path) == head(other)
    assert git("rev-parse", "--abbrev-ref", "@{u}", cwd=path) == "origin/feat/z"


def test_g2_the_same_card_reuses_its_worktree_and_a_clean_branch_fast_forwards(
    origin: Path, repo: Path, env: Path
) -> None:
    first = gitops.provision(repo, TID, "feat/z", "main")
    new = push_commit(env, "b.txt")
    again = gitops.provision(repo, TID, "feat/z", "main")
    assert again == first and head(again) == new


def test_g2_a_dirty_or_diverged_branch_is_left_alone(origin: Path, repo: Path, env: Path) -> None:
    path = gitops.provision(repo, TID, "feat/z", "main")
    (path / "a.txt").write_text("edited")
    before = head(path)
    push_commit(env, "b.txt")
    gitops.provision(repo, TID, "feat/z", "main")
    assert head(path) == before and (path / "a.txt").read_text() == "edited"
    git("checkout", "--", "a.txt", cwd=path)
    (path / "mine.txt").write_text("mine")
    git("add", "mine.txt", cwd=path)
    git("commit", "-m", "mine", cwd=path)
    mine = head(path)
    push_commit(env, "c.txt")
    gitops.provision(repo, TID, "feat/z", "main")
    assert head(path) == mine


def test_g2_a_worktree_on_another_branch_is_never_reused(origin: Path, repo: Path) -> None:
    path = gitops.provision(repo, TID, "feat/z", "main")
    git("checkout", "-b", "feat/other", cwd=path)
    with pytest.raises(gitops.ProvisionError, match=r"feat/other.*feat/z") as e:
        gitops.provision(repo, TID, "feat/z", "main")
    assert not e.value.infra


def test_g_a_base_that_is_not_on_origin_is_a_card_fault(origin: Path, repo: Path) -> None:
    with pytest.raises(gitops.ProvisionError, match="does not exist on origin") as e:
        gitops.provision(repo, TID, "feat/z", "nope")
    assert not e.value.infra


def test_g_an_unreachable_origin_is_an_infrastructure_fault(repo: Path) -> None:
    with pytest.raises(gitops.ProvisionError, match="fetch") as e:
        gitops.provision(repo, TID, "feat/z", "main")
    assert e.value.infra


@pytest.mark.parametrize("call", ["--show-toplevel", "get-url", "check-ignore", "add"])
def test_g_a_git_call_that_cannot_run_is_an_infrastructure_fault(
    origin: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    real = gitops.run

    def flaky(*args: str, **kw: Any) -> tuple[int, str]:
        if call in args:
            raise Error("git failed: timed out")
        return real(*args, **kw)

    monkeypatch.setattr(gitops, "run", flaky)
    with pytest.raises(gitops.ProvisionError, match="timed out") as e:
        gitops.provision(repo, TID, "feat/z", "main")
    assert e.value.infra


def test_g_provisioning_one_repository_is_serialised(origin: Path, repo: Path) -> None:
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo))
    done = threading.Event()
    with (common / gitops.LOCK).open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        worker = threading.Thread(
            target=lambda: (gitops.provision(repo, TID, "feat/z", "main"), done.set())
        )
        worker.start()
        assert not done.wait(1.0)
        fcntl.flock(held, fcntl.LOCK_UN)
    worker.join(30)
    assert done.is_set() and (repo / ".worktrees" / TID).is_dir()


def test_g_a_directory_that_is_not_a_repository_is_a_card_fault(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(gitops.ProvisionError, match="not a git repository"):
        gitops.provision(plain, TID, "feat/z", "main")


def test_g4_worktrees_are_excluded_locally_once_and_never_committed(
    origin: Path, repo: Path
) -> None:
    gitops.provision(repo, TID, "feat/z", "main")
    gitops.provision(repo, "t_ffff0000", "feat/y", "main")
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert exclude.count(".worktrees/") == 1
    assert git("status", "--porcelain", cwd=repo) == ""
    assert git("ls-files", cwd=repo) == "a.txt"


def test_g5_the_primary_checkout_is_untouched(origin: Path, repo: Path, env: Path) -> None:
    (repo / "wip.txt").write_text("work in progress")
    before = (
        head(repo),
        git("branch", "--show-current", cwd=repo),
        git("status", "--porcelain", cwd=repo),
    )
    push_commit(env, "b.txt")
    gitops.provision(repo, TID, "feat/z", "main")
    assert (
        head(repo),
        git("branch", "--show-current", cwd=repo),
        git("status", "--porcelain", cwd=repo),
    ) == before
    assert git("stash", "list", cwd=repo) == ""


def test_git11_repository_hooks_do_not_run(origin: Path, repo: Path, env: Path) -> None:
    marker = env / "hook-ran"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    gitops.provision(repo, TID, "feat/z", "main")
    assert not marker.exists()


def test_g3_clean_and_pushed_is_removed_and_the_branch_survives_unmerged(
    origin: Path, repo: Path
) -> None:
    path = gitops.provision(repo, TID, "feat/z", "main")
    git("push", "-u", "origin", "feat/z", cwd=path)
    assert gitops.cleanup(repo, TID, "feat/z", merged=False) == "removed"
    assert not path.exists() and git("branch", "--list", "feat/z", cwd=repo)


def test_g3_dirty_is_kept_even_when_merged(origin: Path, repo: Path) -> None:
    path = gitops.provision(repo, TID, "feat/z", "main")
    (path / "wip.txt").write_text("x")
    assert gitops.cleanup(repo, TID, "feat/z", merged=True) == "kept_dirty"
    assert path.is_dir()


def test_g3_unpushed_commits_are_kept_unless_the_pr_merged(origin: Path, repo: Path) -> None:
    path = gitops.provision(repo, TID, "feat/z", "main")
    (path / "b.txt").write_text("b")
    git("add", "b.txt", cwd=path)
    git("commit", "-m", "b", cwd=path)
    assert gitops.cleanup(repo, TID, "feat/z", merged=False) == "kept_unpushed"
    assert gitops.cleanup(repo, TID, "feat/z", merged=True) == "removed"
    assert not path.exists() and not git("branch", "--list", "feat/z", cwd=repo)


def test_g3_a_worktree_that_is_already_gone_is_removed_and_pruned(origin: Path, repo: Path) -> None:
    path = gitops.provision(repo, TID, "feat/z", "main")
    git("push", "-u", "origin", "feat/z", cwd=path)
    git("worktree", "remove", "--force", str(path), cwd=repo)
    assert gitops.cleanup(repo, TID, "feat/z", merged=False) == "removed"


def test_snapshot_is_a_detached_origin_checkout_that_follows_origin(
    origin: Path, repo: Path, env: Path
) -> None:
    path, warning = gitops.snapshot(repo)
    assert warning is None and path.parent == dirs().home / "snapshots"
    assert git("branch", "--show-current", cwd=path) == ""
    push_commit(env, "b.txt")
    again, _ = gitops.snapshot(repo)
    assert again == path and (path / "b.txt").exists()
    assert not (repo / "b.txt").exists()


def test_snapshot_warns_when_origin_cannot_be_fetched(repo: Path) -> None:
    path, warning = gitops.snapshot(repo)
    assert warning is not None and "git fetch failed" in warning
    assert path.parent == dirs().home / "snapshots"


def test_snapshot_of_a_plain_directory_uses_it_as_it_is(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    path, warning = gitops.snapshot(plain)
    assert path == plain.resolve() and warning is not None and "not a git repository" in warning


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://example.invalid/acme/proj.git", "example.invalid/acme/proj"),
        ("git@example.invalid:acme/proj.git", "example.invalid/acme/proj"),
        ("ssh://git@example.invalid:22/acme/proj", "example.invalid/acme/proj"),
        ("https://user@example.invalid/acme/sub/proj/", "example.invalid/acme/sub/proj"),
    ],
)
def test_repo_slug_reads_every_origin_form(url: str, slug: str) -> None:
    assert gitops.repo_slug(url) == slug


@pytest.mark.parametrize("name", ["", "-x", "a..b", "a b", "feat/", "x.lock"])
def test_invalid_branch_names_are_refused(name: str) -> None:
    assert not gitops.valid_branch(name)
    assert gitops.valid_branch("feat/ok-1")
