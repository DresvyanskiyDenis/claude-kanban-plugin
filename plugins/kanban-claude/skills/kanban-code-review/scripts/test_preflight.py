"""Pre-flight tests: pure fact functions, plus one run against fake `kanban` and `gh`."""

import json
import os
import subprocess
import sys
from pathlib import Path

import preflight as p
import pytest

SHA = "abc1234def5678abc1234def5678abc1234def56"
SCRIPT = Path(p.__file__).resolve()


def levels(facts: list[str]) -> list[str]:
    return [f.split(" ", 1)[0] for f in facts]


def test_gate_line_must_name_the_head_sha():
    good = "Summary\nGate: `uv run pytest` -> exit 0 at abc1234\n"
    assert levels(p.gate_fact(good, SHA)) == ["ok"]
    assert levels(p.gate_fact("Gate: `make test` exit 0 at 9999999", SHA)) == ["CHECK"]
    assert levels(p.gate_fact("no gate here", SHA)) == ["CHECK"]


GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


class Repo:
    """A throwaway repo: `main` with a 10-line a.txt, `origin/main` pinned to main, branch `feat`."""

    def __init__(self, path: Path):
        self.path = path
        subprocess.run([*GIT, "init", "-q", "-b", "main", str(path)], check=True)
        self.write("a.txt", "".join(f"{i}\n" for i in range(1, 11)))
        self.commit("chore: base")
        self.git("checkout", "-q", "-b", "feat")
        self.edit("a.txt", 2, "feat line")
        self.commit("feat: change line 2")

    def git(self, *args: str, check: bool = True) -> str:
        r = subprocess.run(
            [*GIT, *args], cwd=self.path, capture_output=True, text=True, check=check
        )
        return r.stdout.strip()

    def write(self, name: str, text: str) -> None:
        (self.path / name).write_text(text)

    def edit(self, name: str, line: int, text: str) -> None:
        lines = (self.path / name).read_text().splitlines(keepends=True)
        lines[line - 1] = text + "\n"
        self.write(name, "".join(lines))

    def commit(self, msg: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def advance_main(self, line: int, text: str) -> None:
        self.git("checkout", "-q", "main")
        self.edit("a.txt", line, text)
        self.commit("chore: base moves")
        self.git("update-ref", "refs/remotes/origin/main", "main")
        self.git("checkout", "-q", "feat")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    r = Repo(tmp_path / "r")
    r.git("update-ref", "refs/remotes/origin/main", "main")
    monkeypatch.chdir(r.path)
    return r


def approved_runs(sha: str) -> list[dict[str, str]]:
    return [
        {"kind": "work", "outcome": "completed", "summary": "PR #7 opened"},
        {
            "kind": "review",
            "outcome": "completed",
            "summary": f"WARDEN: APPROVED PR #7 @ {sha[:7]}",
        },
    ]


def test_base_merge_is_not_a_nonconventional_commit(repo: Repo):
    approved = repo.git("rev-parse", "HEAD")
    repo.advance_main(9, "main line")
    repo.git(
        "merge", "-q", "--no-ff", "origin/main", "-m", "Merge remote-tracking branch 'origin/main'"
    )
    merge = repo.git("rev-parse", "HEAD")
    commits = [
        {"oid": approved, "messageHeadline": "feat: change line 2"},
        {"oid": merge, "messageHeadline": "Merge remote-tracking branch 'origin/main'"},
    ]
    assert p.own_headlines(commits) == ["feat: change line 2"]
    unknown = [{"oid": "0" * 40, "messageHeadline": "Merge x"}]
    assert p.own_headlines(unknown) == ["Merge x"]


def test_gate_sha_may_be_an_ancestor_when_only_merges_followed(repo: Repo):
    gated = repo.git("rev-parse", "HEAD")
    repo.advance_main(9, "main line")
    repo.git("merge", "-q", "--no-ff", "origin/main", "-m", "Merge origin/main")
    head = repo.git("rev-parse", "HEAD")
    body = f"Gate: `pytest` exit 0 at {gated[:7]}"

    def only_merges(tok: str) -> bool:
        return p.merge_only_since(tok, head)

    fact = p.gate_fact(body, head, only_merges, ci_green=True)
    assert levels(fact) == ["ok"]
    assert gated[:7] in fact[0] and "merge" in fact[0]
    stale = p.gate_fact(body, head, only_merges)  # no green CI vouches for the merged tree
    assert levels(stale) == ["CHECK"]
    assert gated[:7] in stale[0] and head[:7] in stale[0]
    assert levels(p.gate_fact(body, head)) == ["CHECK"]
    named_head = f"Gate: `pytest` exit 0 at {gated[:7]}, head {head[:7]} adds a merge, no CI"
    assert "names the head commit" in p.gate_fact(named_head, head, only_merges)[0]
    repo.edit("a.txt", 3, "late fix")
    repo.commit("fix: late")
    late = repo.git("rev-parse", "HEAD")
    assert levels(p.gate_fact(body, late, lambda tok: p.merge_only_since(tok, late))) == ["CHECK"]


def test_merge_only_since_needs_a_first_parent_ancestor(repo: Repo):
    branch_tip = repo.git("rev-parse", "HEAD")
    repo.advance_main(9, "main line")
    main_tip = repo.git("rev-parse", "main")
    repo.git("merge", "-q", "--no-ff", "origin/main", "-m", "Merge origin/main")
    head = repo.git("rev-parse", "HEAD")
    assert p.merge_only_since(branch_tip, head)
    assert not p.merge_only_since(head, head)
    assert not p.merge_only_since(main_tip, head)  # came in with the merge, not before it
    assert not p.merge_only_since("not-a-sha", head)


def merge_only_runs(repo: Repo, *, conflict: str | None = None) -> tuple[str, list[dict[str, str]]]:
    """Approve the branch, move main, merge it in; `conflict` moves line 2 too and is the resolution."""
    approved = repo.git("rev-parse", "HEAD")
    repo.advance_main(*((2, "main line") if conflict else (9, "main line")))
    repo.git("merge", "-q", "--no-ff", "origin/main", "-m", "Merge origin/main", check=not conflict)
    if conflict:
        repo.write("a.txt", conflict)
        repo.commit("Merge origin/main")
    return repo.git("rev-parse", "HEAD"), approved_runs(approved)


def test_merge_only_since_approval_is_reported(repo: Repo):
    head, runs = merge_only_runs(repo)
    fact = p.merge_only_fact(runs, head, "main")
    assert len(fact) == 1 and fact[0].startswith("INFO merge-only since APPROVED @ ")
    assert "origin/main" in fact[0]


def test_no_merge_only_fact_without_an_approval_or_after_real_work(repo: Repo):
    head, runs = merge_only_runs(repo)
    assert p.merge_only_fact(runs[:1], head, "main") == []
    assert p.merge_only_fact([], head, "main") == []
    repo.edit("a.txt", 3, "late fix")
    repo.commit("fix: late")
    assert p.merge_only_fact(runs, repo.git("rev-parse", "HEAD"), "main") == []


def test_no_merge_only_fact_when_the_merge_changed_the_prs_own_diff(repo: Repo):
    taken_from_main = "".join(f"{i}\n" for i in range(1, 11)).replace("2\n", "main line\n", 1)
    head, runs = merge_only_runs(repo, conflict=taken_from_main)
    assert p.merge_only_fact(runs, head, "main") == []


def test_ci_checks_separate_failing_pending_and_green():
    assert levels(p.checks_fact([{"name": "a", "bucket": "pass"}])) == ["ok"]
    failing = p.checks_fact([{"name": "a", "bucket": "fail"}, {"name": "b", "bucket": "pending"}])
    assert levels(failing) == ["CHECK", "CHECK"]
    assert levels(p.checks_fact([])) == ["INFO"]


@pytest.mark.parametrize("bucket", ["skipping", "neutral", "surprise"])
def test_ci_is_not_green_while_any_check_did_not_pass(bucket: str):
    facts = p.checks_fact([{"name": "a", "bucket": "pass"}, {"name": "lint", "bucket": bucket}])
    assert levels(facts) == ["CHECK"]
    assert f"lint ({bucket})" in facts[0]


def test_commit_messages_are_checked_against_conventional_commits():
    assert levels(p.nonconventional(["feat: a", "fix(core): b", "docs!: c"])) == ["ok"]
    bad = p.nonconventional(["feat: a", "Update stuff", "wip"])
    assert levels(bad) == ["CHECK"]
    assert "Update stuff" in bad[0]


def test_tier_follows_sensitivity_and_size_of_non_test_changes():
    def f(path: str, n: int) -> dict[str, object]:
        return {"path": path, "additions": n, "deletions": 0}

    assert p.tier([f("src/a.py", 10), f("tests/test_a.py", 900)])[0].startswith("TIER light")
    assert p.tier([f("src/a.py", 200)])[0].startswith("TIER standard")
    assert p.tier([f("src/auth/login.py", 5)])[0].startswith("TIER deep")
    assert p.tier([f("src/a.py", 3000)])[1].startswith("SIZE oversized")


def test_fix_without_a_test_change_is_flagged():
    files = [{"path": "src/a.py", "additions": 1, "deletions": 1}]
    assert levels(p.fix_needs_test("fix: crash", files)) == ["CHECK"]
    assert levels(p.fix_needs_test("feat: new", files)) == []
    tested = [*files, {"path": "tests/test_a.py", "additions": 5, "deletions": 0}]
    assert levels(p.fix_needs_test("fix: crash", tested)) == []


def test_simplify_pr_must_shrink_code_and_leave_tests_alone():
    shrink = [{"path": "src/a.py", "additions": 2, "deletions": 20}]
    assert levels(p.simplify_rules("refactor: simplify store", shrink)) == ["ok"]
    grow = [{"path": "src/a.py", "additions": 20, "deletions": 2}]
    assert levels(p.simplify_rules("refactor: simplify store", grow)) == ["CHECK"]
    touched = [*shrink, {"path": "tests/test_a.py", "additions": 1, "deletions": 1}]
    assert levels(p.simplify_rules("refactor: simplify store", touched)) == ["CHECK"]
    assert p.simplify_rules("feat: other", grow) == []


def test_out_of_scope_tokens_are_matched_against_changed_paths():
    body = "## Goal\nx\n\n## Out of scope\n- `docs/`\n- `src/api.py`\n\n## Deliverable\n`other/`\n"
    hits = p.out_of_scope(body, ["docs/a.md", "src/apix.py", "other/b.py"])
    assert levels(hits) == ["CHECK"]
    assert "docs/a.md" in hits[0] and "apix" not in hits[0] and "other/" not in hits[0]
    assert p.out_of_scope("no sections", ["a.py"]) == []


DIFF = """diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -1 +1,2 @@
 requests==2.0
+evilpkg==1.0
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,4 @@
-URL = "https://old.example.org/x"
+URL = "https://new-host.io/x"
+KEY = "AKIAABCDEFGHIJKLMNOP"
+x = 1\u202e
"""


def test_scan_of_added_lines_finds_dependencies_secrets_unicode_and_hosts():
    out = "\n".join(p.scan_added(DIFF))
    assert "requirements.txt" in out and "evilpkg" in out
    assert "secret-like" in out
    assert "U+202E" in out
    assert "new-host.io" in out
    assert "old.example.org" not in out
    assert levels(p.scan_added("+++ b/a.py\n+x = 1\n")) == ["ok"]


def test_ledger_comments_set_the_round_and_the_previous_head():
    comments = [
        {"body": "[run] smith"},
        {"body": "REVIEW LEDGER @ 1111111 \nW1 major a.py:1 open: x"},
        {"body": "REVIEW LEDGER @ 2222222\nW1 major a.py:1 addressed"},
    ]
    n, prev = p.last_ledger(comments)
    assert (n, prev) == (3, "2222222")
    assert p.last_ledger([{"body": "hello"}]) == (1, None)


def test_pr_ref_accepts_number_url_and_the_cards_pr():
    assert p.pr_ref("42", {}) == ("42", None)
    assert p.pr_ref("https://git.example.com/o/r/pull/7", {}) == ("7", "git.example.com/o/r")
    assert p.pr_ref(None, {"pr_number": 9, "pr_url": "https://git.example.com/o/r/pull/9"}) == (
        "9",
        "git.example.com/o/r",
    )
    assert p.pr_ref(None, {}) is None


def write_exe(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)


def run_script(tmp_path: Path, card: dict, pr: dict, cwd: Path) -> tuple[str, list[str]]:
    """Run preflight in `cwd` against fake `kanban` and `gh`; return its stdout and the calls made."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    write_exe(
        bin_dir / "kanban",
        f"import sys\nopen({str(log)!r}, 'a').write('kanban ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        f"print({json.dumps(card)!r})\n",
    )
    write_exe(
        bin_dir / "gh",
        "import sys\n"
        f"open({str(log)!r}, 'a').write('gh ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "a = sys.argv[1:3]\n"
        f"if a == ['pr', 'view']: print({json.dumps(pr)!r})\n"
        'elif a == [\'pr\', \'checks\']: print(\'[{"name": "ci", "bucket": "pass"}]\')\n'
        "elif a == ['pr', 'diff']: print('+++ b/docs/a.md\\n+hello')\n"
        "else: sys.exit(2)\n",
    )
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "KANBAN_TASK": "t_1"}
    r = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=cwd, env=env
    )
    assert r.returncode == 0, r.stderr
    return r.stdout, log.read_text().splitlines()


def test_run_reads_the_card_through_kanban_show_json(tmp_path: Path):
    card = {
        "task": {
            "id": "t_1",
            "body": "## Out of scope\n- `docs/`\n",
            "pr_number": 7,
            "pr_url": "https://git.example.com/o/r/pull/7",
        },
        "comments": [{"body": "REVIEW LEDGER @ 1111111\nW1"}],
        "runs": [],
    }
    pr = {
        "number": 7,
        "title": "fix: crash",
        "body": "Gate: `pytest` exit 0 at abc1234",
        "headRefName": "fix/x",
        "baseRefName": "main",
        "headRefOid": SHA,
        "commits": [{"oid": SHA, "messageHeadline": "fix: crash"}],
        "files": [{"path": "docs/a.md", "additions": 1, "deletions": 0}],
    }
    subprocess.run(["git", "init", "-q", str(tmp_path / "w")], check=True)
    stdout, calls = run_script(tmp_path, card, pr, tmp_path / "w")
    assert "kanban show --json" in calls
    assert any(c.startswith("gh pr view 7 -R git.example.com/o/r") for c in calls)
    assert any(c.startswith("gh pr checks 7") and "--required" not in c for c in calls)
    prefixes = {line.split(" ", 1)[0] for line in stdout.splitlines()}
    assert prefixes <= {"ok", "CHECK", "TIER", "SIZE", "INFO"}
    assert "INFO PR #7 fix/x -> main @ abc1234, review round 2" in stdout
    assert any(line.startswith("CHECK") and "docs/a.md" in line for line in stdout.splitlines())
    assert any(line.startswith("CHECK") and "test" in line for line in stdout.splitlines())


def test_run_reports_a_base_merge_as_merge_only(repo: Repo, tmp_path: Path):
    gated = repo.git("rev-parse", "HEAD")
    repo.advance_main(9, "main line")
    repo.git(
        "merge", "-q", "--no-ff", "origin/main", "-m", "Merge remote-tracking branch 'origin/main'"
    )
    head = repo.git("rev-parse", "HEAD")
    card = {
        "task": {"id": "t_1", "body": "", "pr_number": 7},
        "comments": [],
        "runs": approved_runs(gated),
    }
    pr = {
        "number": 7,
        "title": "feat: x",
        "body": f"Gate: `pytest` exit 0 at {gated[:7]}",
        "headRefName": "feat",
        "baseRefName": "main",
        "headRefOid": head,
        "commits": [
            {"oid": gated, "messageHeadline": "feat: change line 2"},
            {"oid": head, "messageHeadline": "Merge remote-tracking branch 'origin/main'"},
        ],
        "files": [{"path": "a.txt", "additions": 1, "deletions": 1}],
    }
    stdout, _ = run_script(tmp_path, card, pr, repo.path)
    lines = stdout.splitlines()
    assert f"INFO merge-only since APPROVED @ {gated[:7]}" in stdout
    assert "ok commit messages are conventional" in lines
    assert any(
        ln.startswith("ok the Gate line names") and "only merge commits" in ln for ln in lines
    )


def test_a_repo_without_ci_reads_as_no_checks(monkeypatch):
    # real `gh pr checks` exits 1 instead of printing [] when the repo has no CI at all
    def gh(argv):
        return subprocess.CompletedProcess(
            argv, 1, "", "no checks reported on the 'fix/x' branch\n"
        )

    monkeypatch.setattr(p, "call", gh)
    assert p.pr_checks("7", []) == []


def test_pr_checks_reads_every_check_and_keeps_json_gh_prints_with_a_nonzero_exit(monkeypatch):
    asked = []

    def gh(argv):
        asked.append(argv)
        return subprocess.CompletedProcess(argv, 8, '[{"name": "x", "bucket": "pending"}]', "")

    monkeypatch.setattr(p, "call", gh)
    assert p.pr_checks("7", []) == [{"name": "x", "bucket": "pending"}]
    assert "--required" not in asked[0]
