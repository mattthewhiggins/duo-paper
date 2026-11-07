"""The GitHub Actions paper run's own tools: restoring and saving the paper-state branch, and publishing the repo.

ops/actions/restore_state.sh and save_state.sh run against a bare repository in a temporary folder, which stands in for
GitHub. Each job is a fresh shallow clone, as actions/checkout makes. Dates come from DUO_STATE_TODAY and git's own
GIT_COMMITTER_DATE, so no clock is faked. git runs without the global or system config, so this machine's settings
(signing, hooks, the default branch) can't change what's tested. ops/publish_repo.py runs on small project trees made
here, and once on this project itself.

No network: the only remote is a folder.
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESTORE = ROOT / "ops" / "actions" / "restore_state.sh"
SAVE = ROOT / "ops" / "actions" / "save_state.sh"
BOT = "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"

_spec = importlib.util.spec_from_file_location("publish_repo", ROOT / "ops" / "publish_repo.py")
publish_repo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish_repo)


def git_env(**extra) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "GITHUB_", "DUO_"))}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", LC_ALL="C",
               PATH=os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")]))  # python3 is this one.
    env.update(extra)
    return env


def run(args, cwd, **extra) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, env=git_env(**extra), capture_output=True, text=True, timeout=60)


def git(*args, cwd) -> str:
    result = run(["git", *args], cwd)
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def write_state(state: Path, label: str) -> None:
    """What a tick leaves in data/: a row in the WAL-mode database, and the heartbeat file."""
    data = state / "data"
    data.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(data / "paper_systematic.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS ticks (label TEXT)")
    connection.execute("INSERT INTO ticks VALUES (?)", (label,))
    connection.commit()
    connection.close()
    (data / "heartbeat_systematic.json").write_text(json.dumps({"status": "ok", "label": label}))


def rows(db: Path) -> list[str]:
    connection = sqlite3.connect(db)
    try:
        return [row[0] for row in connection.execute("SELECT label FROM ticks ORDER BY rowid")]
    finally:
        connection.close()


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git and bash")
class StateBranch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.remote = self.tmp / "remote.git"
        self.jobs = 0
        git("init", "--quiet", "--bare", "-b", "main", str(self.remote), cwd=self.tmp)
        seed = self.tmp / "seed"
        git("init", "--quiet", "-b", "main", str(seed), cwd=self.tmp)
        (seed / "README.md").write_text("The code. The state lives on paper-state.\n")
        git("add", "README.md", cwd=seed)
        git("-c", "user.name=Seed", "-c", "user.email=seed@users.noreply.github.com", "commit", "--quiet", "-m", "Code",
            cwd=seed)
        git("push", "--quiet", self.url, "main", cwd=seed)
        self.main = self.tip("main")

    @property
    def url(self) -> str:
        return self.remote.as_uri()

    def tip(self, branch: str) -> str:
        return git("--git-dir", str(self.remote), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                   cwd=self.tmp) if self.has(branch) else ""

    def has(self, branch: str) -> bool:
        return run(["git", "--git-dir", str(self.remote), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                   self.tmp).returncode == 0

    def job(self, action: str) -> tuple[Path, dict, subprocess.CompletedProcess]:
        """A fresh shallow clone, as actions/checkout makes, and restore_state.sh run in it."""
        self.jobs += 1
        clone = self.tmp / f"job{self.jobs}"
        git("clone", "--quiet", "--depth", "1", self.url, str(clone), cwd=self.tmp)
        output = self.tmp / f"job{self.jobs}.out"
        output.write_text("")
        result = run(["bash", str(RESTORE), "state", action], clone, GITHUB_OUTPUT=str(output))
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
        return clone, outputs, result

    def save(self, clone: Path, at: str, **extra) -> subprocess.CompletedProcess:
        return run(["bash", str(SAVE), "state"], clone, DUO_STATE_TODAY=at[:10], GIT_COMMITTER_DATE=at,
                   GIT_AUTHOR_DATE=at, **extra)

    def started(self, at="2026-11-05T00:20:00Z", label="start") -> Path:
        clone, outputs, result = self.job("start")
        self.assertEqual(outputs, {"started": "true", "new": "true"}, result.stdout + result.stderr)
        write_state(clone / "state", label)
        saved = self.save(clone, at)
        self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
        return clone

    def fenced(self, at="2026-11-06T12:00:00Z") -> str:
        """The tree the cutover's fence leaves on paper-state (RUNBOOK 13.3 step 5): the database and the heartbeat
        gone from data/, and a note in their place. Returns paper-state's new tip."""
        clone, _, _ = self.job("tick")
        data = clone / "state" / "data"
        for name in ("paper_systematic.db", "heartbeat_systematic.json"):
            (data / name).unlink()
        (data / "MOVED_TO_RENDER.md").write_text("# The paper run moved to Render\n")
        saved = self.save(clone, at)
        self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
        self.assertEqual(self.saved_files(), [".gitattributes", "data/MOVED_TO_RENDER.md"])
        return self.tip("paper-state")

    def history(self) -> list[dict]:
        log = git("--git-dir", str(self.remote), "log", "--format=%H|%P|%cI|%an <%ae>|%s", "paper-state", cwd=self.tmp)
        keys = ("sha", "parents", "date", "author", "subject")
        commits = [dict(zip(keys, line.split("|"))) for line in log.splitlines()]
        for commit in commits:
            commit["date"] = commit["date"][:19]  # Without the zone, which git versions write as Z or +00:00.
        return commits

    def saved_files(self) -> list[str]:
        return git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "paper-state", cwd=self.tmp).split()

    def saved_rows(self) -> list[str]:
        """The rows in the saved database file alone, with no -wal beside it."""
        copy = self.tmp / f"saved{len(list(self.tmp.glob('saved*')))}.db"
        with copy.open("wb") as handle:
            subprocess.run(["git", "--git-dir", str(self.remote), "show", "paper-state:data/paper_systematic.db"],
                           env=git_env(), stdout=handle, check=True)
        return rows(copy)

    def test_a_scheduled_run_before_the_start_does_nothing(self):
        clone, outputs, result = self.job("tick")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs, {"started": "false", "new": "false"})
        self.assertIn("::notice title=Paper run not started::", result.stdout)
        self.assertFalse((clone / "state").exists())
        self.assertFalse(self.has("paper-state"))
        self.assertEqual(self.tip("main"), self.main)

    def test_the_first_start_makes_an_orphan_branch_holding_only_the_state(self):
        clone = self.started()
        self.assertEqual((clone / "state" / ".gitattributes").read_text().splitlines()[0], "*.db binary")
        [commit] = self.history()
        self.assertEqual(commit["parents"], "")  # An orphan: no history shared with main.
        self.assertEqual(commit["author"], BOT)
        self.assertEqual(commit["subject"], "Paper state, 2026-11-05 (UTC)")
        self.assertEqual(self.saved_files(),
                         [".gitattributes", "data/heartbeat_systematic.json", "data/paper_systematic.db"])
        self.assertEqual(self.saved_rows(), ["start"])
        self.assertEqual(self.tip("main"), self.main)
        refs = git("--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)", cwd=self.tmp).split()
        self.assertEqual(refs, ["refs/heads/main", "refs/heads/paper-state"])

    def test_a_later_run_restores_the_saved_state(self):
        self.started()
        clone, outputs, result = self.job("tick")
        self.assertEqual(outputs, {"started": "true", "new": "false"}, result.stderr)
        self.assertEqual(rows(clone / "state" / "data" / "paper_systematic.db"), ["start"])
        self.assertNotIn("::warning::", result.stdout)

    def test_an_unchanged_save_is_a_no_op(self):
        self.started()
        before = self.tip("paper-state")
        clone, _, _ = self.job("tick")
        result = self.save(clone, "2026-11-05T00:35:00Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hasn't changed", result.stdout)
        self.assertEqual(self.tip("paper-state"), before)

    def test_saves_on_one_utc_day_amend_one_commit_and_a_new_day_adds_one(self):
        self.started("2026-11-05T00:20:00Z", "nov5 00:20")
        for at in ("2026-11-05T10:35:00Z", "2026-11-05T23:47:00Z", "2026-11-06T00:05:00Z", "2026-11-06T01:05:00Z",
                   "2026-11-07T00:20:00Z"):
            clone, _, _ = self.job("tick")
            write_state(clone / "state", at)
            result = self.save(clone, at)
            self.assertEqual(result.returncode, 0, result.stderr)
        newest, middle, oldest = self.history()
        self.assertEqual([c["subject"] for c in (newest, middle, oldest)],
                         ["Paper state, 2026-11-07 (UTC)", "Paper state, 2026-11-06 (UTC)",
                          "Paper state, 2026-11-05 (UTC)"])
        # Each day's commit is the day's last save, and amending under a shallow clone kept every parent.
        self.assertEqual(oldest["date"], "2026-11-05T23:47:00")
        self.assertEqual(middle["date"], "2026-11-06T01:05:00")
        self.assertEqual((newest["parents"], middle["parents"], oldest["parents"]), (middle["sha"], oldest["sha"], ""))
        self.assertEqual(self.saved_rows(), ["nov5 00:20", "2026-11-05T10:35:00Z", "2026-11-05T23:47:00Z",
                                             "2026-11-06T00:05:00Z", "2026-11-06T01:05:00Z", "2026-11-07T00:20:00Z"])

    def test_the_wal_is_folded_into_the_saved_database(self):
        # As if a job were killed mid-run: the rows are in the -wal file, not yet in the database file.
        clone, _, _ = self.job("start")
        live = self.tmp / "live"
        live.mkdir()
        connection = sqlite3.connect(live / "paper_systematic.db")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE ticks (label TEXT)")
        connection.executemany("INSERT INTO ticks VALUES (?)", [("one",), ("two",)])
        connection.commit()
        data = clone / "state" / "data"
        for name in ("paper_systematic.db", "paper_systematic.db-wal"):
            shutil.copyfile(live / name, data / name)
        connection.close()
        self.assertGreater((data / "paper_systematic.db-wal").stat().st_size, 0)
        result = self.save(clone, "2026-11-05T00:20:00Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.saved_files(), [".gitattributes", "data/paper_systematic.db"])
        self.assertEqual(self.saved_rows(), ["one", "two"])

    def test_a_branch_without_its_database_is_restored_as_it_is(self):
        clone, _, _ = self.job("start")
        (clone / "state" / "data" / "heartbeat_systematic.json").write_text("{}")
        self.assertEqual(self.save(clone, "2026-11-05T00:20:00Z").returncode, 0)
        clone, outputs, result = self.job("tick")
        self.assertEqual(outputs, {"started": "true", "new": "false"})
        self.assertIn("::warning::paper-state has no data/paper_systematic.db", result.stdout)
        # Nothing is made in its place: the tick's own guard fails the run instead of starting a new paper run.
        self.assertFalse((clone / "state" / "data" / "paper_systematic.db").exists())

    def test_render_backup_expects_a_branch_without_its_database_and_says_nothing(self):
        # After the cutover's fence the database is on Render, so the warning and its advice to start a new run
        # would be wrong on every Render backup run. It's still restored as it is, with nothing made in its place.
        clone, _, _ = self.job("start")
        (clone / "state" / "data" / "heartbeat_systematic.json").write_text("{}")
        self.assertEqual(self.save(clone, "2026-11-05T00:20:00Z").returncode, 0)
        clone, outputs, result = self.job("backup")
        self.assertEqual(outputs, {"started": "true", "new": "false"})
        self.assertNotIn("::warning::", result.stdout)
        self.assertFalse((clone / "state" / "data" / "paper_systematic.db").exists())

    def test_a_paper_state_fenced_for_render_refuses_every_action_but_backup(self):
        # Since the cutover the run is on Render and Paper run is disabled. A Paper run that runs anyway, one GitHub
        # started late or one started by hand, must do nothing: a tick would find no database, and a start would begin
        # a second paper run on GitHub. So it stops before checking anything out, leaving nothing to save or publish.
        self.started()
        fence = self.fenced()
        for action in ("tick", "start", "resume", "flatten"):
            with self.subTest(action=action):
                clone, outputs, result = self.job(action)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("::error title=The paper run is on Render::", result.stdout)
                self.assertEqual(outputs, {})
                self.assertFalse((clone / "state").exists())
                self.assertEqual(self.tip("paper-state"), fence)
        # Render backup keeps the service's copies on it, in data/render/.
        clone, outputs, result = self.job("backup")
        self.assertEqual(outputs, {"started": "true", "new": "false"}, result.stdout + result.stderr)
        self.assertNotIn("::error", result.stdout)
        self.assertNotIn("::warning", result.stdout)
        # The note alone decides, so a database saved beside it doesn't make the branch Paper run's again.
        write_state(clone / "state", "beside the note")
        self.assertEqual(self.save(clone, "2026-11-06T15:23:00Z").returncode, 0)
        beside = self.tip("paper-state")
        for action in ("start", "tick"):
            with self.subTest(action=action, database="beside the note"):
                clone, outputs, result = self.job(action)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("::error title=The paper run is on Render::", result.stdout)
                self.assertEqual(outputs, {})
                self.assertFalse((clone / "state").exists())
                self.assertEqual(self.tip("paper-state"), beside)
        # Going back (RUNBOOK 13.9) takes the note away in the commit that puts the database back, and Paper run then
        # carries on with it.
        clone, _, _ = self.job("backup")
        (clone / "state" / "data" / "MOVED_TO_RENDER.md").unlink()
        self.assertEqual(self.save(clone, "2026-11-06T16:00:00Z").returncode, 0)
        clone, outputs, result = self.job("tick")
        self.assertEqual(outputs, {"started": "true", "new": "false"}, result.stdout + result.stderr)
        self.assertEqual(rows(clone / "state" / "data" / "paper_systematic.db"), ["beside the note"])
        self.assertNotIn("::", result.stdout)

    def test_render_backup_with_no_paper_state_says_nothing_about_start(self):
        # Its Pull step says what's wrong then. Paper run's notice, to start the run with action start, isn't for it.
        clone, outputs, result = self.job("backup")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outputs, {"started": "false", "new": "false"})
        self.assertNotIn("::notice", result.stdout)
        self.assertNotIn("action start", result.stdout)
        self.assertFalse((clone / "state").exists())
        self.assertFalse(self.has("paper-state"))

    def test_a_start_on_a_branch_that_lost_its_database_is_a_new_paper_run(self):
        # RUNBOOK 12.5: start begins a new run there. As on a new branch, a start that fails is then not kept.
        clone, _, _ = self.job("start")
        (clone / "state" / "data" / "heartbeat_systematic.json").write_text("{}")
        self.assertEqual(self.save(clone, "2026-11-05T00:20:00Z").returncode, 0)
        clone, outputs, result = self.job("start")
        self.assertEqual(outputs, {"started": "true", "new": "true"}, result.stdout + result.stderr)
        self.assertIn("so this starts a new paper run", result.stdout)
        self.assertNotIn("::warning::", result.stdout)
        write_state(clone / "state", "new start")
        self.assertEqual(self.save(clone, "2026-11-06T00:20:00Z").returncode, 0)
        self.assertEqual(self.saved_rows(), ["new start"])
        # Once it holds a paper run, a start carries on with it, so it isn't new.
        _, outputs, _ = self.job("start")
        self.assertEqual(outputs, {"started": "true", "new": "false"})

    def test_a_database_another_process_still_has_open_is_not_saved(self):
        # As a tick still running would: open, but holding no lock, so the checkpoint isn't busy. /proc, which a
        # folder of links stands in for here, shows it.
        self.started()
        clone, _, _ = self.job("tick")
        write_state(clone / "state", "half done")
        db = (clone / "state" / "data" / "paper_systematic.db").resolve()
        tick = sqlite3.connect(db)
        self.addCleanup(tick.close)
        tick.execute("SELECT COUNT(*) FROM ticks").fetchall()
        proc = self.tmp / "proc"
        for pid, fd, name in (("4242", "3", db), ("4242", "4", f"{db}-wal"), ("77", "0", os.devnull)):
            (proc / pid / "fd").mkdir(parents=True, exist_ok=True)
            (proc / pid / "fd" / fd).symlink_to(name)
        before = self.tip("paper-state")
        result = self.save(clone, "2026-11-05T00:35:00Z", DUO_STATE_PROC=str(proc))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is still open in process 4242, so it may be half-written. Not saving it.", result.stderr)
        self.assertEqual(self.tip("paper-state"), before)
        self.assertTrue(Path(f"{db}-wal").exists(), "the -wal it still has open is left alone")
        # Once that process has closed it, it's saved.
        tick.close()
        shutil.rmtree(proc / "4242")
        result = self.save(clone, "2026-11-05T00:36:00Z", DUO_STATE_PROC=str(proc))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.saved_rows(), ["start", "half done"])

    def test_a_save_never_writes_over_a_save_it_has_not_seen(self):
        self.started()
        slow, _, _ = self.job("tick")
        fast, _, _ = self.job("tick")
        write_state(fast / "state", "fast")
        self.assertEqual(self.save(fast, "2026-11-05T00:35:00Z").returncode, 0)
        after_fast = self.tip("paper-state")
        write_state(slow / "state", "slow")
        result = self.save(slow, "2026-11-05T00:36:00Z")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("moved while this job ran", result.stdout)
        self.assertEqual(self.tip("paper-state"), after_fast)
        self.assertEqual(self.saved_rows(), ["start", "fast"])

    def test_a_failed_lookup_is_not_taken_for_a_run_that_has_not_started(self):
        self.started()
        clone = self.tmp / "offline"
        git("clone", "--quiet", "--depth", "1", self.url, str(clone), cwd=self.tmp)
        git("remote", "set-url", "origin", (self.tmp / "missing.git").as_uri(), cwd=clone)
        output = self.tmp / "offline.out"
        output.write_text("")
        result = run(["bash", str(RESTORE), "state", "tick"], clone, GITHUB_OUTPUT=str(output))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Couldn't look for the paper-state branch", result.stdout)
        self.assertEqual(output.read_text(), "")

    def test_restore_wants_a_new_folder_and_save_only_the_state_branch(self):
        self.started()
        clone, _, _ = self.job("tick")
        again = run(["bash", str(RESTORE), "state", "tick"], clone, GITHUB_OUTPUT=os.devnull)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already exists", again.stdout)
        (clone / "stray.txt").write_text("not state")
        wrong = run(["bash", str(SAVE), "."], clone, DUO_STATE_TODAY="2026-11-05")
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("isn't a checkout of the paper-state branch", wrong.stdout)
        self.assertEqual(self.tip("main"), self.main)


def make_project(root: Path) -> None:
    """A small project with a file for every whitelist entry, and the things that must never be published."""
    files = {
        "duo/__init__.py": "", "duo/systematic.py": "RUNNER = True\n", "duo/research_breaks.json": "{}\n",
        "duo/static/index.html": "<html></html>\n", "research/__init__.py": "", "research/harness/engine.py": "",
        "research/harness/deeper/extra.py": "", "research/strategies/f1_trend.py": "", "research/tools/__init__.py": "",
        "research/tools/fetch_binance.py": "", "research/artifacts/cost_model.json": "{}\n",
        "research/artifacts/cost_model.sha256": "abc\n", "tests/__init__.py": "", "tests/helpers.py": "",
        "tests/sys_helpers.py": "", "tests/test_core.py": "", "config.toml": "mode = \"paper\"\n",
        "requirements.txt": "", "requirements-research.txt": "", "requirements-paper.txt": "",
        "README.md": "Put your key in .env as `ANTHROPIC_API_KEY=...`.\n", "RUNBOOK.md": "", "start.sh": "#!/bin/zsh\n",
        ".python-version": "3.13\n", "ops/actions/save_state.sh": "#!/usr/bin/env bash\n", "ops/publish_repo.py": "",
        "ops/render/serve.py": "", "ops/render/pull_backup.py": "", ".github/workflows/paper.yml": "name: Paper run\n",
        ".github/workflows/render-backup.yml": "name: Render backup\n",
        # Never published:
        ".env": "ANTHROPIC_API_KEY=" + "x" * 8 + "\n", "data/paper_systematic.db": "state", "RESULTS.md": "",
        "AUDIT.md": "", "duo/AUDIT_STATUS.md": "", "research/PREREGISTRATION.md": "",
        "research/artifacts/variants.jsonl": "", "research/llm/calls.py": "", "research/tests/test_data.py": "",
        "tests/demo_server.py": "", ".claude/launch.json": "{}", ".venv/pyvenv.cfg": "", "duo/__pycache__/x.pyc": "",
        "ops/com.duo.bot.plist": "", "ops/render/__pycache__/serve.cpython-313.pyc": "",
        ".github/workflows/other.yml": "",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    for name in ("start.sh", "ops/actions/save_state.sh"):
        (root / name).chmod(0o755)


def tree(folder: Path) -> set[str]:
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file() and ".git" not in p.parts}


PUBLISHED = {
    "duo/__init__.py", "duo/systematic.py", "duo/research_breaks.json", "duo/static/index.html",
    "research/__init__.py", "research/harness/engine.py", "research/harness/deeper/extra.py",
    "research/strategies/f1_trend.py", "research/tools/__init__.py", "research/tools/fetch_binance.py",
    "research/artifacts/cost_model.json", "research/artifacts/cost_model.sha256", "tests/__init__.py",
    "tests/helpers.py", "tests/sys_helpers.py", "tests/test_core.py", "config.toml", "requirements.txt",
    "requirements-research.txt", "requirements-paper.txt", "README.md", "RUNBOOK.md", "start.sh", ".python-version",
    "ops/actions/save_state.sh", "ops/publish_repo.py", "ops/render/serve.py", "ops/render/pull_backup.py",
    ".github/workflows/paper.yml", ".github/workflows/render-backup.yml", ".gitignore",
}


class PublishRepo(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.project = self.tmp / "project"
        make_project(self.project)
        self.target = self.tmp / "public"

    def refused(self, *problem_parts, target=None, patterns=publish_repo.WHITELIST) -> list[str]:
        with self.assertRaises(publish_repo.NotPublished) as caught:
            publish_repo.publish(self.project, target or self.target, patterns)
        joined = "\n".join(caught.exception.problems)
        for part in problem_parts:
            self.assertIn(part, joined)
        return caught.exception.problems

    def test_only_the_whitelist_is_copied(self):
        files, removed = publish_repo.publish(self.project, self.target)
        self.assertEqual(tree(self.target), PUBLISHED)
        self.assertEqual(set(files), PUBLISHED)
        self.assertEqual(removed, [])
        for name in ("start.sh", "ops/actions/save_state.sh"):  # The workflow runs the scripts as they are.
            self.assertTrue((self.target / name).stat().st_mode & stat.S_IXUSR, name)
        self.assertEqual((self.target / ".gitignore").read_text().split(),
                         ["data/", ".env", ".venv/", "__pycache__/", "*.pyc", "_site/", "state/"])

    def test_files_no_longer_published_are_deleted_but_git_is_left_alone(self):
        (self.target / ".git").mkdir(parents=True)
        (self.target / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        for name in ("old.py", "research/artifacts/variants.jsonl", "notes/today.md", "README.md"):
            (self.target / name).parent.mkdir(parents=True, exist_ok=True)
            (self.target / name).write_text("old")
        _, removed = publish_repo.publish(self.project, self.target)
        self.assertEqual(removed, ["notes/today.md", "old.py", "research/artifacts/variants.jsonl"])
        self.assertEqual(tree(self.target), PUBLISHED)
        self.assertFalse((self.target / "notes").exists())
        self.assertEqual((self.target / ".git" / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertEqual((self.target / "README.md").read_text(), (self.project / "README.md").read_text())

    def test_publishing_again_replaces_every_file_even_one_read_only_in_the_project(self):
        # The frozen cost model is read-only in the project, and RUNBOOK 12.2 publishes into the same clone again.
        model = self.project / "research" / "artifacts" / "cost_model.json"
        model.chmod(0o444)
        publish_repo.publish(self.project, self.target)
        (self.target / ".git").mkdir()  # Now a clone, as the owner's is.
        model.chmod(0o644)
        model.write_text('{"version": 2}\n')
        model.chmod(0o444)
        (self.project / "README.md").write_text("Changed.\n")
        publish_repo.publish(self.project, self.target)
        self.assertEqual((self.target / "research/artifacts/cost_model.json").read_text(), '{"version": 2}\n')
        self.assertEqual((self.target / "README.md").read_text(), "Changed.\n")
        self.assertEqual(stat.S_IMODE((self.target / "research/artifacts/cost_model.json").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((self.target / "start.sh").stat().st_mode), 0o755)

    def test_a_home_folder_path_stops_it_and_leaves_the_target_alone(self):
        (self.target / ".git").mkdir(parents=True)
        (self.target / "old.py").write_text("old")
        home = "/" + "Users/someone/project/data"  # Split, so this file doesn't trip the check itself.
        (self.project / "duo" / "systematic.py").write_text(f"RUNNER = True\nPATH = '{home}'\n")
        self.refused("duo/systematic.py:2: a path on this Mac")
        self.assertEqual(tree(self.target), {"old.py"})

    def test_a_home_folder_path_with_dashes_for_slashes_stops_it(self):
        # How Claude names its session folders: a note pasted from one would publish the Mac's login. Split, as above.
        session = "/private/tmp/claude-501/-" + "Users-someone-Library-Application-Support-Claude-x/scratchpad"
        (self.project / "RUNBOOK.md").write_text(f"Checked in {session}.\n")
        self.refused("RUNBOOK.md:1: a path on this Mac, written with dashes")
        self.assertFalse(self.target.exists())

    def test_a_file_that_is_not_utf8_stops_it(self):
        # Read as UTF-8 regardless, UTF-16 puts a NUL between the letters, and the address would pass.
        (self.project / "RUNBOOK.md").write_bytes(("Questions to " + "owner" + "@" + "duo-owner.uk\n").encode("utf-16"))
        self.refused("RUNBOOK.md: not UTF-8 text")
        self.assertFalse(self.target.exists())

    def test_a_link_on_the_list_stops_it(self):
        # A link is copied as what it points to, which can be a private file outside the project.
        outside = self.tmp / "private"
        (outside / "static").mkdir(parents=True)
        (outside / "notes.txt").write_text("private notes\n")
        (outside / "static" / "index.html").write_text("<html>private</html>\n")
        (self.project / "duo" / "notes.py").symlink_to(outside / "notes.txt")
        (self.project / "duo" / "alias.py").symlink_to("systematic.py")  # Even to a file in the project.
        shutil.rmtree(self.project / "duo" / "static")
        (self.project / "duo" / "static").symlink_to(outside / "static", target_is_directory=True)
        problems = self.refused("duo/notes.py is a link", "duo/alias.py is a link", "duo/static/index.html is a link")
        self.assertNotIn("nothing in project matches duo/static/*.html", problems)
        self.assertFalse(self.target.exists())

    def test_a_dotenv_file_stops_it(self):
        self.refused(".env: a .env file", patterns=publish_repo.WHITELIST + (".env",))
        self.assertFalse(self.target.exists())

    def test_an_email_address_stops_it_but_noreply_and_example_ones_do_not(self):
        (self.project / "README.md").write_text("Commits by 41898282+github-actions[bot]@users.noreply.github.com and "
                                                "someone@users.noreply.github.com are fine, and so are the reserved "
                                                "names: owner@example.com, https://user@a.test, x@y.invalid.\n")
        publish_repo.publish(self.project, self.target)
        (self.project / "RUNBOOK.md").write_text("Questions to " + "owner" + "@" + "duo-owner.uk\n")
        self.refused("RUNBOOK.md:1: an email address", target=self.tmp / "public2")

    def test_a_key_stops_it_but_a_placeholder_does_not(self):
        key = "sk-" + "ant-api03-" + "Q" * 40
        (self.project / "duo" / "systematic.py").write_text(f"KEY = '{key}'\n")
        problems = self.refused("duo/systematic.py:1: what looks like a key")
        self.assertNotIn(key, "\n".join(problems))  # Shown cut short, not repeated in full.
        (self.project / "duo" / "systematic.py").write_text("RUNNER = True\n")
        (self.project / "config.toml").write_text("KRAKEN_API_SECRET" + " = " + "'" + "aB3+/" * 10 + "'\n")
        self.refused("config.toml:1: what looks like a key")

    def test_it_refuses_a_target_that_could_be_some_other_folder(self):
        for target in (self.project, self.project / "public", self.tmp):
            self.refused("overlaps this project", target=target)
        other = self.tmp / "photos"
        other.mkdir()
        (other / "holiday.jpg").write_text("jpg")
        self.refused("isn't a git clone", target=other)
        self.assertEqual(tree(other), {"holiday.jpg"})

    def test_a_missing_file_on_the_list_stops_it(self):
        (self.project / "duo" / "research_breaks.json").unlink()
        self.refused("nothing in project matches duo/research_breaks.json")

    def test_this_project_publishes_cleanly(self):
        target = self.tmp / "duo-paper"
        files, _ = publish_repo.publish(ROOT, target)
        for name in ("duo/__main__.py", "research/artifacts/cost_model.json", ".github/workflows/paper.yml",
                     "ops/actions/restore_state.sh", "ops/actions/save_state.sh", "requirements-paper.txt",
                     "tests/test_actions_scripts.py", ".python-version", "ops/render/pull_backup.py",
                     "ops/render/serve.py", "ops/render/ctl.py", ".github/workflows/render-backup.yml"):
            self.assertIn(name, files)
        for name in ("RESULTS.md", "AUDIT.md", "duo/AUDIT_STATUS.md", "tests/demo_server.py", ".env"):
            self.assertNotIn(name, files)
        self.assertFalse(any(name.startswith(("data/", ".venv/", "research/llm/", "research/tests/")) or
                             name.endswith(".pyc") for name in files))
        # The runner records a hash of every research .py on each decision: the copy must give the same one.
        def research_code(root):
            return sorted((p.relative_to(root).as_posix(), p.read_bytes())
                          for d in ("research/harness", "research/strategies") for p in (root / d).rglob("*.py"))
        self.assertEqual(research_code(target), research_code(ROOT))


if __name__ == "__main__":
    unittest.main()
