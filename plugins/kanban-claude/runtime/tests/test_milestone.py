"""TST-M: the milestone-branches reference. Its replay block runs against real git; its gating claims run on the store."""

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import housekeeping, store, views

from .conftest import git
from .test_merge import CLEAN, PR, Prs, approved, merge_calls

REFERENCE = Path(__file__).resolve().parents[2] / "skills/kanban/references/milestone-branches.md"
SLUG = "x"


def replay_script(n: int, prev: str) -> str:
    """The reference's own fenced block that replays a milestone, with its placeholders filled in."""
    blocks = re.findall(r"```bash\n(.*?)```", REFERENCE.read_text(), re.S)
    (block,) = (b for b in blocks if "git cherry-pick" in b)
    return block.replace("<prev>", prev).replace("<n>", str(n)).replace("<slug>", SLUG)


def commit(clone: Path, name: str, text: str, msg: str) -> None:
    (clone / name).write_text(text)
    git("add", name, cwd=clone)
    git("commit", "-m", msg, cwd=clone)


def lines(**changed: str) -> str:
    return "".join(f"{changed.get(k, k)}\n" for k in ("l1", "l2", "l3", "l4", "l5"))


@pytest.fixture
def dev(env: Path) -> Path:
    """A clone of a bare origin whose `main` holds a five-line a.txt."""
    origin = env / "ms-origin.git"
    git("init", "--bare", "-b", "main", str(origin), cwd=env)
    clone = env / "ms-dev"
    git("clone", str(origin), str(clone), cwd=env)
    git("checkout", "-b", "main", cwd=clone)
    commit(clone, "a.txt", lines(), "init")
    git("push", "origin", "main", cwd=clone)
    return clone


def push_ref(dev: Path, src: str, dst: str) -> None:
    git("fetch", "origin", cwd=dev)
    git("push", "origin", f"origin/{src}:refs/heads/{dst}", cwd=dev)


def milestone_commits(dev: Path, branch: str, work: list[tuple[str, str]]) -> None:
    """Part PRs squash-merged into the milestone branch: one commit each."""
    git("fetch", "origin", cwd=dev)
    git("checkout", "-B", branch, f"origin/{branch}", cwd=dev)
    for i, (name, text) in enumerate(work):
        commit(dev, name, text, f"part {i}")
    git("push", "origin", branch, cwd=dev)


def finalize(dev: Path, n: int, prev: str) -> Path:
    """A finalize card's worktree (cut from origin/main), with the reference's replay run in it."""
    git("fetch", "origin", cwd=dev)
    wt = dev.parent / f"final-{n}-{prev.replace('/', '_')}"
    git("worktree", "add", "-b", f"milestone/{n}-{SLUG}-final", str(wt), "origin/main", cwd=dev)
    # a fixed committer date: copies cut in the same second as the originals would get identical hashes
    env = {**os.environ, "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}
    subprocess.run(["bash", "-ec", replay_script(n, prev)], cwd=wt, check=True, env=env)
    return wt


def owner_merges(dev: Path, final: str, method: str) -> None:
    git("checkout", "-B", "main", "origin/main", cwd=dev)
    if method == "squash":
        git("merge", "--squash", f"origin/{final}", cwd=dev)
        git("commit", "-m", "squash", cwd=dev)
    elif method == "merge":
        git("merge", "--no-ff", "-m", "merge", f"origin/{final}", cwd=dev)
    else:
        git("checkout", "-B", "rebased", f"origin/{final}", cwd=dev)
        git("rebase", "origin/main", cwd=dev)
        git("checkout", "main", cwd=dev)
        git("merge", "--ff-only", "rebased", cwd=dev)
    git("push", "origin", "main", cwd=dev)


@pytest.mark.parametrize("method", ["squash", "merge", "rebase"])
def test_m1_the_replay_gives_the_next_milestone_a_clean_pr_whichever_way_the_owner_merged(
    dev: Path, method: str
) -> None:
    push_ref(dev, "main", f"milestone/1-{SLUG}")
    milestone_commits(dev, f"milestone/1-{SLUG}", [("a.txt", lines(l2="m1")), ("m1.txt", "m1\n")])
    push_ref(dev, f"milestone/1-{SLUG}", f"milestone/2-{SLUG}")
    # milestone 2 edits the very line milestone 1 edited, so a squash leaves them in conflict
    milestone_commits(
        dev,
        f"milestone/2-{SLUG}",
        [("a.txt", lines(l2="m2")), ("m2.txt", "m2\n"), ("a.txt", lines(l2="m2", l4="m2"))],
    )

    final1 = finalize(dev, 1, "main")
    git("push", "origin", f"milestone/1-{SLUG}-final", cwd=final1)
    git("checkout", "main", cwd=dev)
    commit(
        dev, "other.txt", "other\n", "unrelated work on main"
    )  # main moves after final 1 was cut
    git("push", "origin", "main", cwd=dev)
    owner_merges(dev, f"milestone/1-{SLUG}-final", method)

    git("fetch", "origin", cwd=dev)
    probe = dev.parent / "probe"
    git("worktree", "add", "--detach", str(probe), f"origin/milestone/2-{SLUG}", cwd=dev)
    naive = subprocess.run(["git", "merge", "origin/main"], cwd=probe, capture_output=True)
    assert (
        naive.returncode != 0
    )  # even `--merge` does not help: final 1 holds copies of milestone 1's commits

    final2 = finalize(dev, 2, f"milestone/1-{SLUG}")
    assert git("rev-list", "--count", "origin/main..HEAD", cwd=final2) == "3"
    assert git("diff", "--name-only", "origin/main", "HEAD", cwd=final2).splitlines() == [
        "a.txt", "m2.txt",
    ]  # fmt: skip
    assert (final2 / "a.txt").read_text() == lines(l2="m2", l4="m2")
    git("push", "origin", f"milestone/2-{SLUG}-final", cwd=final2)  # a new ref: no force-push
    if (
        method == "squash"
    ):  # the range that looks right, from the default branch, would replay milestone 1 too
        wrong = git(
            "rev-list", "--no-merges", "--count", f"origin/main..origin/milestone/2-{SLUG}", cwd=dev
        )
        assert wrong == "5"


def test_m2_a_part_pr_into_a_milestone_branch_is_merged_by_the_daemon(
    conn: Any, repo: Path, set_prs: Prs, env: Path
) -> None:
    set_prs({"7": {**PR, "baseRefName": f"milestone/1-{SLUG}"}})
    tid = approved(conn, repo, base=f"milestone/1-{SLUG}")
    assert store.get_task(conn, tid)["merge_state"] == "pending"
    housekeeping.run_due(conn, store.now() + 100000)
    assert store.get_task(conn, tid)["merge_state"] == "merged"
    assert "--squash" in merge_calls(env)[0]


def test_m3_a_child_of_a_finalize_card_starts_at_approval_not_at_the_owners_merge(
    conn: Any, repo: Path, set_prs: Prs
) -> None:
    set_prs({"7": {**PR, "baseRefName": "main"}})
    finalize_card = approved(conn, repo, CLEAN, base=None)
    child = str(
        store.create(
            conn, "main", "Finalize 2", body="b", assignee="smith", parents=[finalize_card]
        )["id"]
    )
    assert store.get_task(conn, finalize_card)["merge_state"] is None
    assert [c["id"] for c in views.awaiting_owner_merge(conn)] == [finalize_card]
    # released while the PR is unmerged: the gate cannot wait for the owner
    assert store.get_task(conn, child)["status"] == "ready"

    kinds = lambda: [r[0] for r in conn.execute("SELECT kind FROM task_events ORDER BY id")]  # noqa: E731
    before = kinds()
    set_prs({"7": {**PR, "baseRefName": "main", "state": "MERGED"}})
    housekeeping.settle_owner_merges(conn)
    assert (
        store.get_task(conn, finalize_card)["merged_at"] and kinds() == before
    )  # no event wakes a watcher


@pytest.mark.parametrize("parts", [1, 2])
def test_m4_the_finalize_card_the_reference_prescribes_is_based_on_the_default_branch(
    conn: Any, repo: Path, parts: int
) -> None:
    """With one part the card would inherit the part's base and its PR would go to the milestone branch."""
    store.board_create(conn, "main", default_workdir=str(repo))
    milestone = f"milestone/1-{SLUG}"
    ws = f"worktree:{repo}"
    ids = [
        str(
            store.create(
                conn, "main", f"Part {i}", body="b", assignee="smith", workspace=ws,
                branch=f"{milestone}-p{i}", base=milestone,
            )["id"]
        )
        for i in range(parts)
    ]  # fmt: skip
    (block,) = (
        b for b in re.findall(r"```bash\n(.*?)```", REFERENCE.read_text(), re.S) if "-final --" in b
    )
    base = re.search(r"--base (\S+)", block)
    assert base, "the finalize card must pass --base"
    final = store.create(
        conn, "main", "Finalize", body="b", assignee="smith", workspace=ws,
        branch=f"{milestone}-final", base=base[1].replace("<default>", "main"), parents=ids,
    )  # fmt: skip
    assert store.get_task(conn, str(final["id"]))["base_branch"] == "main"
