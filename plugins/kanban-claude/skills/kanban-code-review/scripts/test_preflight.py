"""Pre-flight tests: pure fact functions, plus one run against fake `kanban` and `gh`."""

import json
import os
import subprocess
import sys
from pathlib import Path

import preflight as p

SHA = "abc1234def5678abc1234def5678abc1234def56"
SCRIPT = Path(p.__file__).resolve()


def levels(facts: list[str]) -> list[str]:
    return [f.split(" ", 1)[0] for f in facts]


def test_gate_line_must_name_the_head_sha():
    good = "Summary\nGate: `uv run pytest` -> exit 0 at abc1234\n"
    assert levels(p.gate_fact(good, SHA)) == ["ok"]
    assert levels(p.gate_fact("Gate: `make test` exit 0 at 9999999", SHA)) == ["CHECK"]
    assert levels(p.gate_fact("no gate here", SHA)) == ["CHECK"]


def test_ci_checks_separate_failing_pending_and_green():
    assert levels(p.checks_fact([{"name": "a", "bucket": "pass"}])) == ["ok"]
    failing = p.checks_fact([{"name": "a", "bucket": "fail"}, {"name": "b", "bucket": "pending"}])
    assert levels(failing) == ["CHECK", "CHECK"]
    assert levels(p.checks_fact([])) == ["INFO"]


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


def test_run_reads_the_card_through_kanban_show_json(tmp_path: Path):
    card = {
        "task": {
            "id": "t_1",
            "body": "## Out of scope\n- `docs/`\n",
            "pr_number": 7,
            "pr_url": "https://git.example.com/o/r/pull/7",
        },
        "comments": [{"body": "REVIEW LEDGER @ 1111111\nW1"}],
    }
    pr = {
        "number": 7,
        "title": "fix: crash",
        "body": "Gate: `pytest` exit 0 at abc1234",
        "headRefName": "fix/x",
        "baseRefName": "main",
        "headRefOid": SHA,
        "commits": [{"messageHeadline": "fix: crash"}],
        "files": [{"path": "docs/a.md", "additions": 1, "deletions": 0}],
    }
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
        "elif a == ['pr', 'checks'] and '--required' in sys.argv: sys.exit(1)\n"
        'elif a == [\'pr\', \'checks\']: print(\'[{"name": "ci", "bucket": "pass"}]\')\n'
        "elif a == ['pr', 'diff']: print('+++ b/docs/a.md\\n+hello')\n"
        "else: sys.exit(2)\n",
    )
    subprocess.run(["git", "init", "-q", str(tmp_path / "w")], check=True)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "KANBAN_TASK": "t_1"}
    r = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=tmp_path / "w", env=env
    )
    assert r.returncode == 0, r.stderr
    calls = log.read_text().splitlines()
    assert "kanban show --json" in calls
    assert any(c.startswith("gh pr view 7 -R git.example.com/o/r") for c in calls)
    assert any("--required" in c for c in calls) and any(
        c.startswith("gh pr checks 7") and "--required" not in c for c in calls
    )
    prefixes = {line.split(" ", 1)[0] for line in r.stdout.splitlines()}
    assert prefixes <= {"ok", "CHECK", "TIER", "SIZE", "INFO"}
    assert "INFO PR #7 fix/x -> main @ abc1234, review round 2" in r.stdout
    assert any(line.startswith("CHECK") and "docs/a.md" in line for line in r.stdout.splitlines())
    assert any(line.startswith("CHECK") and "test" in line for line in r.stdout.splitlines())
