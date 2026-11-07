"""ops/render/pull_backup.py, which the Render backup workflow runs: its pull of the service's backup and its page.

A local HTTP server over a folder stands in for the service, serving a backup under state/ and the page beside it, and
answering /ok, as ops/render/serve.py does. The databases have the run's own tables (duo/db.py's and
duo/systematic.py's SCHEMA), and the manifests and the seed marker inside them are laid out as the supervisor and
ctl.py seed write theirs. Every refusal must exit 1 with a GitHub ::error:: line and write nothing, so nothing reaches
paper-state.

Then the workflow's backup job runs, its steps' shell in order as the runner runs them, against a bare repository that
stands in for GitHub, as in tests/test_actions_scripts.py. A backup must land in data/render/ on paper-state, one commit
a UTC day, an older one must never land, and the database the cutover took off paper-state must stay off it.

No network: the only server is on 127.0.0.1, and the only remote a folder.
"""
import contextlib
import functools
import hashlib
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from duo import db as duo_db
from duo import systematic

from .test_actions_scripts import BOT, git, git_env, make_project, publish_repo, run
from .test_workflow import BACKUP, job, script, step

ROOT = Path(__file__).resolve().parent.parent
PULL = ROOT / "ops" / "render" / "pull_backup.py"

_spec = importlib.util.spec_from_file_location("pull_backup", PULL)
pull_backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pull_backup)

HOUR = 3600
SEED_SHA = "0123456789abcdef" * 2 + "01234567"
START = json.dumps({"strategy_id": "F1__ENS__BTC_ETH__vol40", "quote": "USD", "started_at": 1793836800.0})
# GitHub's last check before the move, and the hourly one before it, as Paper run ran them; then Render's loop, every
# 5 minutes from the seed.
LAST_CRON_AT = datetime(2026, 11, 8, 11, 35, 20, tzinfo=timezone.utc).timestamp()
GITHUB_CHECKS = [LAST_CRON_AT - HOUR, LAST_CRON_AT]
LOOP_CHECKS = [LAST_CRON_AT + 1800 + 300 * n for n in range(4)]
HEARTBEAT_FILE = b'{"status": "ok", "schedule": "loop"}\n'


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_db(path: Path, checks, start: str = START) -> bytes:
    """A database with the run's tables, its start record and a check (heartbeat) at each time in checks. Returns its
    bytes."""
    connection = sqlite3.connect(path)
    connection.executescript(duo_db.SCHEMA + systematic.SCHEMA)
    connection.executemany("INSERT INTO state (key, value) VALUES (?, ?)",
                           [("sys_start", start), ("sys_tick_at", str(LAST_CRON_AT))])
    connection.executemany("INSERT INTO sys_heartbeats (ts, status, halts, controls) VALUES (?, 'ok', '[]', '{}')",
                           [(at,) for at in checks])
    connection.commit()
    connection.close()
    return path.read_bytes()


def marker(seed_db: bytes, **changes) -> dict:
    """The seed marker, render_seed.json, as ctl.py seed writes it."""
    seed = {"schema": 1, "sha": SEED_SHA, "db_sha256": sha256(seed_db), "seeded_at": LAST_CRON_AT + 1500,
            "seeded_at_utc": "2026-11-08 12:00:20 UTC", "sys_tick_at": LAST_CRON_AT, "last_cron_at": LAST_CRON_AT,
            "cron_runs": len(GITHUB_CHECKS), "start_sha256": sha256(START.encode())}
    seed.update(changes)
    return seed


class Service:
    """The service on 127.0.0.1: the files in folder at their URLs, and /ok answering ok, or not, as the supervisor
    does. answers holds what isn't the service's own: {path: (code, headers, body)}, sent as they are. asked lists the
    paths asked for, in order."""

    def __init__(self, folder: Path):
        self.folder = folder
        (folder / "state").mkdir(parents=True)
        self.ok = (200, "ok")
        self.answers, self.asked = {}, []
        service = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                service.asked.append(self.path)
                if self.path in service.answers:
                    code, headers, body = service.answers[self.path]
                    self.send_response(code)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path != "/ok":
                    return super().do_GET()
                code, body = service.ok
                self.send_response(code)
                self.send_header("Content-Length", str(len(body) + 1))
                self.end_headers()
                self.wfile.write(f"{body}\n".encode())

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      functools.partial(Handler, directory=str(folder)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def back_up(self, data: bytes, seed: dict | None, *, heartbeat: bytes | None = HEARTBEAT_FILE, start: str = START,
                newest: float | None = None, **changes) -> dict:
        """Makes data the latest backup, beside heartbeat (or none) and the manifest the supervisor writes for them.
        changes replace the manifest's fields."""
        state = self.folder / "state"
        (state / "paper_systematic.db").write_bytes(data)
        (state / "heartbeat_systematic.json").unlink(missing_ok=True)
        if heartbeat is not None:
            (state / "heartbeat_systematic.json").write_bytes(heartbeat)
        taken_at = time.time() - 600
        manifest = {
            "schema": 1, "taken_at": taken_at, "taken_at_utc": pull_backup.utc(taken_at),
            "db": {"sha256": sha256(data), "size": len(data)},
            "heartbeat": None if heartbeat is None else {"sha256": sha256(heartbeat), "size": len(heartbeat)},
            "start_sha256": sha256(start.encode()), "newest_heartbeat_at": newest, "integrity": "ok", "seed": seed,
            "commit": None,
        }
        manifest.update(changes)
        (state / "manifest.json").write_text(json.dumps(manifest))
        return manifest


def main(*argv) -> tuple[int, str]:
    """pull_backup.py with argv, as typed: its exit code and what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            pull_backup.main(list(argv))
            code = 0
        except SystemExit as stop:
            code = stop.code
    return code, out.getvalue()


class Pull(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.seed_db = self.tmp / "seed.db"
        self.seed = marker(make_db(self.seed_db, GITHUB_CHECKS))
        self.service = Service(self.tmp / "service")
        self.addCleanup(self.service.close)
        self.out = self.tmp / "out"
        self.checks = GITHUB_CHECKS + LOOP_CHECKS
        self.back_up(self.checks)

    def back_up(self, checks, *, start: str = START, **changes) -> dict:
        """A backup of a database holding checks, as the service would take it."""
        data = make_db(self.tmp / f"backup{len(list(self.tmp.glob('backup*')))}.db", checks, start)
        return self.service.back_up(data, changes.pop("seed", self.seed), start=start, newest=max(checks), **changes)

    def pull(self, *args, seed_sha: str = SEED_SHA) -> tuple[int, str]:
        return main("pull", self.service.url, str(self.out), "--seed-db", str(self.seed_db), "--seed-sha", seed_sha,
                    *args)

    def refused(self, *says, args=(), seed_sha: str = SEED_SHA) -> str:
        code, printed = self.pull(*args, seed_sha=seed_sha)
        self.assertEqual(code, 1, printed)
        self.assertTrue(printed.startswith("::error title=Render backup not saved::"), printed)
        self.assertEqual(printed.count("\n"), 1, "one line, so GitHub shows all of it")
        for part in says:
            self.assertIn(part, printed)
        self.assertFalse(self.out.exists(), "nothing is written")
        return printed

    def test_a_backup_that_checks_out_is_written_as_the_service_served_it(self):
        code, printed = self.pull()
        self.assertEqual(code, 0, printed)
        self.assertIn(f"holds the run seeded from {SEED_SHA[:7]}, and its newest check is at "
                      f"{pull_backup.utc(LOOP_CHECKS[-1])}", printed)
        served = self.service.folder / "state"
        self.assertEqual({p.name: p.read_bytes() for p in self.out.iterdir()},
                         {p.name: p.read_bytes() for p in served.iterdir()})
        self.assertEqual(len(list(self.out.iterdir())), 3)

    def test_a_backup_without_a_heartbeat_file_leaves_none_beside_it(self):
        self.service.back_up(make_db(self.tmp / "no_beat.db", self.checks), self.seed, heartbeat=None,
                             newest=max(self.checks))
        self.out.mkdir()
        (self.out / "heartbeat_systematic.json").write_text("{}")  # An older backup's: it isn't this one's.
        code, printed = self.pull()
        self.assertEqual(code, 0, printed)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["manifest.json", "paper_systematic.db"])

    def test_a_file_that_is_not_the_one_its_manifest_describes_is_refused(self):
        for name in ("paper_systematic.db", "heartbeat_systematic.json"):
            with self.subTest(name=name):
                self.back_up(self.checks)
                served = self.service.folder / "state" / name
                served.write_bytes(served.read_bytes() + b"\n")  # As a newer backup's file, say.
                self.refused(f"The {name} downloaded ({served.stat().st_size} bytes) isn't the one the manifest "
                             f"describes", "run it again")

    def test_a_database_that_fails_its_integrity_check_is_refused(self):
        good = make_db(self.tmp / "good.db", self.checks)
        # One byte changed in a row, so its index no longer matches, which the check reports; and bytes that aren't a
        # database at all, which SQLite reports as an error instead.
        for data, says in ((good.replace(b"sys_tick_at", b"sys_tick_au", 1), "missing from index"),
                           (b"not a database " * 300, "file is not a database")):
            with self.subTest(says=says):
                self.service.back_up(data, self.seed, newest=max(self.checks))
                self.refused("The backup's database fails SQLite's integrity check", says)
        # A sound database that holds something else isn't saved either.
        connection = sqlite3.connect(self.tmp / "other.db")
        connection.execute("CREATE TABLE notes (text TEXT)")
        connection.commit()
        connection.close()
        self.service.back_up((self.tmp / "other.db").read_bytes(), self.seed, newest=max(self.checks))
        self.refused("The backup's database isn't a paper run's (no such table: sys_heartbeats)")

    def test_a_start_record_that_is_not_the_one_its_manifest_names_is_refused(self):
        self.back_up(self.checks, start_sha256="f" * 64)
        self.refused(f"The backup's start record (sha256 {sha256(START.encode())}) isn't the one its manifest names")

    def test_a_different_paper_run_from_the_one_render_was_seeded_with_is_refused(self):
        # The database and its manifest agree with each other: it's the run itself that isn't the seed's.
        other = START.replace("USD", "GBP")
        self.back_up(self.checks, start=other)
        self.refused("The backup holds a different paper run from the one Render was seeded with")

    def test_a_service_seeded_from_another_commit_is_refused(self):
        self.refused(f"The service was seeded from {SEED_SHA}, not from {'f' * 40}", seed_sha="f" * 40)
        self.back_up(self.checks, seed=None)
        self.refused("The service was seeded from nothing")

    def test_a_backup_with_no_check_since_the_move_is_refused(self):
        # The seed as it came, before the loop on Render has checked anything.
        self.back_up(GITHUB_CHECKS)
        # The supervisor's own first backup is always this one, so the error says when the next comes, and how to take
        # one sooner.
        self.refused(f"The backup's newest check ({pull_backup.utc(LAST_CRON_AT)}) isn't later than GitHub's last",
                     "the loop on Render hasn't checked anything since the move",
                     "The supervisor backs up by itself within about 5 minutes of the loop's first check",
                     "python3 ops/render/ctl.py backup")
        self.back_up(self.checks, seed=marker(self.seed_db.read_bytes(), last_cron_at=None))
        self.refused("doesn't say when GitHub's last check was")

    def test_a_backup_older_than_the_copy_already_saved_is_refused(self):
        previous = self.tmp / "previous.json"
        previous.write_text(json.dumps({"schema": 1, "newest_heartbeat_at": max(self.checks) + 300}))
        self.refused("is older than the copy already saved", "saving it would go backwards",
                     args=("--previous", str(previous)))
        # The same backup again, as while the bot is held, isn't older.
        previous.write_text(json.dumps({"schema": 1, "newest_heartbeat_at": max(self.checks)}))
        code, printed = self.pull("--previous", str(previous))
        self.assertEqual(code, 0, printed)

    def test_a_backup_over_26_hours_old_is_refused(self):
        self.back_up(self.checks, taken_at=time.time() - 27 * HOUR)
        self.refused("27 hours ago, so its backups have stopped")
        self.back_up(self.checks, taken_at=time.time() - 25 * HOUR)
        code, printed = self.pull()
        self.assertEqual(code, 0, printed)

    def test_a_service_with_no_backup_yet_is_refused(self):
        (self.service.folder / "state" / "manifest.json").unlink()
        self.refused(f"Couldn't download the service's backup: {self.service.url}/state/manifest.json answered 404")

    def test_an_answer_that_sends_it_elsewhere_is_refused(self):
        # The service never redirects, so what does isn't the service, and nothing checked where it points: here, a
        # second server with the same backup.
        elsewhere = Service(self.tmp / "elsewhere")
        self.addCleanup(elsewhere.close)
        shutil.copytree(self.service.folder / "state", elsewhere.folder / "state", dirs_exist_ok=True)
        self.service.answers["/state/manifest.json"] = (302, {"Location": f"{elsewhere.url}/state/manifest.json",
                                                              "Content-Length": "0"}, b"")
        self.refused(f"{self.service.url}/state/manifest.json answered 302")
        self.assertEqual(elsewhere.asked, [])

    def test_an_answer_bigger_than_the_file_could_be_is_refused(self):
        # Whatever answers could send without end: no more is read than the service's own file could be.
        size = (self.service.folder / "state" / "manifest.json").stat().st_size
        with mock.patch.dict(pull_backup.MAX_BYTES, {"manifest.json": size - 1}):
            self.refused(f"{self.service.url}/state/manifest.json answered with more than {size - 1:,} bytes")
        with mock.patch.dict(pull_backup.MAX_BYTES, {"manifest.json": size}):
            code, printed = self.pull()
        self.assertEqual(code, 0, printed)

    def test_an_answer_cut_short_is_tried_again(self):
        # Fewer bytes than its Content-Length said is a blip on the way, not the file, so it's tried again.
        self.service.answers["/state/manifest.json"] = (200, {"Content-Length": "1000"}, b'{"schema": 1')
        with mock.patch.object(pull_backup, "PAUSE_SECONDS", 0):
            self.refused(f"{self.service.url}/state/manifest.json didn't answer (IncompleteRead(12 bytes read")
        self.assertEqual(self.service.asked, ["/state/manifest.json"] * pull_backup.TRIES)

    def test_it_runs_on_its_own_without_duo(self):
        # As the runner runs it: a python3 with nothing of this project importable (-I leaves out the script's folder
        # and the working one).
        self.assertNotRegex(PULL.read_text(), r"(?m)^\s*(?:from|import) (?:duo|numpy|research)\b")
        args = [sys.executable, "-I", str(PULL), "pull", self.service.url, str(self.out), "--seed-db",
                str(self.seed_db), "--seed-sha", SEED_SHA]
        result = subprocess.run(args, cwd=self.tmp, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run(args[:-1] + ["f" * 40], cwd=self.tmp, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith("::error title=Render backup not saved::"), result.stdout)


class Page(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.service = Service(self.tmp / "service")
        self.addCleanup(self.service.close)
        self.out = self.tmp / "_site"
        # status.json's files object names the files the service's build made, as duo/site.py writes it.
        self.status = {"host": {"schedule": "loop", "held": True}, "compare": {"ok": True, "error": None},
                       "files": {"page": "index.html", "status": "status.json", "compare": "compare.md",
                                 "decisions": "decisions.jsonl", "decisions_error": None}}
        self.files = {"index.html": b"<!doctype html>\n<title>Duo paper run</title>\n",
                      "status.json": json.dumps(self.status, indent=1).encode() + b"\n",
                      "compare.md": b"# Paper against the research\n", "decisions.jsonl": b'{"bar_utc": "2026"}\n'}
        for name, data in self.files.items():
            (self.service.folder / name).write_bytes(data)

    def serve_status(self, status: dict) -> bytes:
        """status as the service's status.json. Returns its bytes."""
        data = json.dumps(status, indent=1).encode() + b"\n"
        (self.service.folder / "status.json").write_bytes(data)
        return data

    def final(self) -> tuple[int, str]:
        return main("page", str(self.out), self.service.url, "--final")

    def test_the_moved_page_takes_readers_to_the_service_and_loads_nothing_from_elsewhere(self):
        url = "https://duo-paper.onrender.com"
        code, printed = main("page", str(self.out), url)
        self.assertEqual(code, 0, printed)
        self.assertEqual([p.name for p in self.out.iterdir()], ["index.html"])
        text = (self.out / "index.html").read_text()
        self.assertIn(f'<meta http-equiv="refresh" content="10; url={url}/">', text)
        for path in ("/", "/compare.md", "/decisions.jsonl", "/state/manifest.json"):
            self.assertIn(f'<a href="{url}{path}">', text)
        # Nothing for a browser to fetch: no scripts, styles, fonts or images, and no address but the service's.
        self.assertNotRegex(text, r"(?i)<script|<link|<img|<iframe|\bsrc=|@import|url\(")
        self.assertEqual(set(re.findall(r"https?://[^/\"'<>\s]+", text)), {url})

    def test_final_copies_the_services_page_as_it_is(self):
        code, printed = main("page", str(self.out), self.service.url, "--final")
        self.assertEqual(code, 0, printed)
        self.assertEqual({p.name: p.read_bytes() for p in self.out.iterdir()}, self.files)

    def test_final_writes_nothing_unless_it_has_every_file_the_page_names(self):
        (self.service.folder / "status.json").write_text("<html>Not the page</html>\n")
        code, printed = main("page", str(self.out), self.service.url, "--final")
        self.assertEqual(code, 1, printed)
        self.assertIn("isn't JSON", printed)
        (self.service.folder / "status.json").write_bytes(self.files["status.json"])
        (self.service.folder / "decisions.jsonl").unlink()
        code, printed = main("page", str(self.out), self.service.url, "--final")
        self.assertEqual(code, 1, printed)
        self.assertTrue(printed.startswith("::error title=Page not written::Couldn't copy the service's page: "
                                           f"{self.service.url}/decisions.jsonl answered 404"), printed)
        self.assertFalse(self.out.exists())

    def test_final_leaves_out_a_file_the_services_build_could_not_make_and_says_why(self):
        # status.json then has null for it, with the reason, and the page says why instead of linking it. So the copy
        # has none either, rather than fail for want of it, and the rest of the end of the test is kept.
        for left_out in (["compare.md"], ["decisions.jsonl"], ["compare.md", "decisions.jsonl"]):
            with self.subTest(left_out=left_out):
                shutil.rmtree(self.out, ignore_errors=True)
                status = json.loads(self.files["status.json"])
                if "compare.md" in left_out:
                    status["compare"] = {"ok": False, "error": "ValueError: no 100% day to compare"}
                    status["files"]["compare"] = None
                if "decisions.jsonl" in left_out:
                    status["files"].update(decisions=None, decisions_error="RuntimeError: the export broke")
                for name, data in self.files.items():  # The service's build, without what it couldn't make.
                    (self.service.folder / name).write_bytes(data)
                for name in left_out:
                    (self.service.folder / name).unlink()
                served = self.serve_status(status)
                code, printed = self.final()
                self.assertEqual(code, 0, printed)
                *warnings, wrote = printed.splitlines()
                self.assertEqual([w.split("::")[1] for w in warnings],
                                 [f"warning title=Page copied without {name}" for name in left_out])
                if "compare.md" in left_out:
                    self.assertIn("(ValueError: no 100%25 day to compare)", warnings[0])
                if "decisions.jsonl" in left_out:
                    self.assertIn("(RuntimeError: the export broke)", warnings[-1])
                self.assertTrue(wrote.startswith(f"Wrote the service's page into {self.out}: "), wrote)
                kept = {name: data for name, data in self.files.items() if name not in left_out}
                self.assertEqual({p.name: p.read_bytes() for p in self.out.iterdir()}, {**kept, "status.json": served})

    def test_final_refuses_a_status_json_that_names_some_other_file(self):
        # The service's names only its own files, or null. Nothing named in the JSON is ever fetched or written.
        status = json.loads(self.files["status.json"])
        status["files"]["decisions"] = "elsewhere.txt"
        self.serve_status(status)
        (self.service.folder / "elsewhere.txt").write_text("Not the page's.\n")
        self.service.asked.clear()
        code, printed = self.final()
        self.assertEqual(code, 1, printed)
        self.assertTrue(printed.startswith("::error title=Page not written::"), printed)
        self.assertIn('"elsewhere.txt"', printed)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.service.asked, ["/status.json"])

    def test_final_copies_all_four_when_status_json_has_no_files_object(self):
        # As a status.json from before the files object: then each of the four must be there, as before.
        status = json.loads(self.files["status.json"])
        del status["files"]
        served = self.serve_status(status)
        code, printed = self.final()
        self.assertEqual(code, 0, printed)
        self.assertEqual({p.name: p.read_bytes() for p in self.out.iterdir()}, {**self.files, "status.json": served})
        shutil.rmtree(self.out)
        (self.service.folder / "compare.md").unlink()
        code, printed = self.final()
        self.assertEqual(code, 1, printed)
        self.assertIn(f"{self.service.url}/compare.md answered 404", printed)
        self.assertFalse(self.out.exists())

    def test_it_takes_only_a_web_address(self):
        # The moved page puts it in links.
        with contextlib.redirect_stderr(io.StringIO()):
            code, _ = main("page", str(self.out), "javascript:alert(1)")
        self.assertEqual(code, 2)
        self.assertFalse(self.out.exists())


class Published(unittest.TestCase):
    def test_the_public_repository_gets_what_render_and_the_workflow_need_and_no_other_workflow(self):
        for pattern in (".python-version", "ops/render/*.py", ".github/workflows/render-backup.yml",
                        ".github/workflows/paper.yml"):  # Kept for going back: off the list, it'd be deleted.
            self.assertIn(pattern, publish_repo.WHITELIST)
        with tempfile.TemporaryDirectory() as temporary:
            make_project(Path(temporary))
            files = publish_repo.whitelisted(Path(temporary))
        for name in (".python-version", "ops/render/serve.py", "ops/render/pull_backup.py",
                     ".github/workflows/render-backup.yml", ".github/workflows/paper.yml"):
            self.assertIn(name, files)
        self.assertNotIn(".github/workflows/other.yml", files)
        self.assertFalse([name for name in files if name.startswith("ops/render/") and not name.endswith(".py")])
        # Render's Python: 3.13, as the one hashed numpy wheel needs. A bare minor version takes its latest patch.
        self.assertEqual((ROOT / ".python-version").read_text(), "3.13\n")


@unittest.skipUnless(shutil.which("git") and shutil.which("bash") and shutil.which("curl"), "needs git, bash and curl")
class BackupJob(unittest.TestCase):
    """The workflow's backup job against GitHub's stand-in and the service's. gh is the one stand-in in the job: it says
    whether Paper run is disabled."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.text = BACKUP.read_text()
        self.service = Service(self.tmp / "service")
        self.addCleanup(self.service.close)
        self.jobs = 0
        self.remote = self.tmp / "remote.git"
        git("init", "--quiet", "--bare", "-b", "main", str(self.remote), cwd=self.tmp)
        # main holds the code the job runs.
        code = self.tmp / "code"
        git("init", "--quiet", "-b", "main", str(code), cwd=self.tmp)
        for name in ("ops/actions/restore_state.sh", "ops/actions/save_state.sh", "ops/render/pull_backup.py"):
            (code / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, code / name)
        self.commit(code, "Code", "2026-11-08T09:00:00Z")
        git("push", "--quiet", self.url, "main", cwd=code)
        self.main = self.tip("main")
        # paper-state holds the run as GitHub left it, tagged render-seed, and then the fence the cutover pushes: the
        # database leaves data/, so a Paper run that ran by mistake would find none (RUNBOOK 13.3).
        state = self.tmp / "paper-state"
        git("init", "--quiet", "-b", "paper-state", str(state), cwd=self.tmp)
        (state / "data").mkdir()
        (state / ".gitattributes").write_text("*.db binary\n*.db-wal binary\n*.db-shm binary\n")
        self.seed_db = make_db(state / "data" / "paper_systematic.db", GITHUB_CHECKS)
        (state / "data" / "heartbeat_systematic.json").write_text('{"status": "ok", "schedule": "cron"}\n')
        self.commit(state, "Paper state, 2026-11-08 (UTC)", "2026-11-08T11:36:00Z")
        git("tag", "render-seed", cwd=state)
        self.seed_sha = git("rev-parse", "HEAD", cwd=state)
        git("rm", "--quiet", "data/paper_systematic.db", "data/heartbeat_systematic.json", cwd=state)
        (state / "data").mkdir()  # git took the emptied folder away with its files.
        (state / "data" / "MOVED_TO_RENDER.md").write_text("The paper run moved to Render.\n")
        self.commit(state, "Moved to Render", "2026-11-08T12:00:00Z")
        git("push", "--quiet", self.url, "paper-state", "refs/tags/render-seed", cwd=state)

    @property
    def url(self) -> str:
        return self.remote.as_uri()

    def commit(self, folder: Path, message: str, at: str) -> None:
        git("add", "-A", cwd=folder)
        result = run(["git", "-c", "user.name=Owner", "-c", "user.email=owner@users.noreply.github.com", "commit",
                      "--quiet", "-m", message], folder, GIT_COMMITTER_DATE=at, GIT_AUTHOR_DATE=at)
        self.assertEqual(result.returncode, 0, result.stderr)

    def tip(self, ref: str) -> str:
        return git("--git-dir", str(self.remote), "rev-parse", ref, cwd=self.tmp)

    def back_up(self, checks) -> bytes:
        """The service's latest backup: the run with checks. Returns the database's bytes."""
        data = make_db(self.tmp / f"backup{len(list(self.tmp.glob('backup*')))}.db", checks)
        seed = marker(self.seed_db, sha=self.seed_sha)
        self.service.back_up(data, seed, newest=max(checks))
        return data

    def run_job(self, at: str, paper_run: str = "disabled_manually") -> dict:
        """The backup job on a fresh checkout at the UTC time at, as the runner runs it: each step's shell in order,
        and after one fails only those marked always(). Returns {step: result} for the steps that ran."""
        self.jobs += 1
        checkout, runner = self.tmp / f"job{self.jobs}", self.tmp / f"runner{self.jobs}"
        git("clone", "--quiet", "--depth", "1", self.url, str(checkout), cwd=self.tmp)
        (runner / "bin").mkdir(parents=True)
        (runner / "bin" / "gh").write_text('#!/bin/bash\necho "$PAPER_RUN"\n')
        (runner / "bin" / "gh").chmod(0o755)
        environment = {"PATH": os.pathsep.join([str(runner / "bin"), git_env()["PATH"]]), "RUNNER_TEMP": str(runner),
                       "RENDER_URL": self.service.url, "GITHUB_REPOSITORY": "someone/duo-paper", "PAPER_RUN": paper_run,
                       "DUO_STATE_TODAY": at[:10], "GIT_COMMITTER_DATE": at, "GIT_AUTHOR_DATE": at}
        outputs, results, failed = {}, {}, False
        for name in re.findall(r"(?m)^      - name: (.+)$", job(self.text, "backup")):
            text = step(self.text, name)
            condition = re.search(r"(?m)^        if: (.+)$", text)
            if condition and (outputs["guard"].get("ran") != "true" or failed and "always()" not in condition[1]):
                continue
            env = {}  # The step's env:, from the outputs of the steps before it, as GitHub fills it in.
            for key, value in re.findall(r"(?m)^          (\w+): \$\{\{ (.+) \}\}$", text):
                found = re.fullmatch(r"steps\.(\w+)\.outputs\.(\w+)", value)
                env[key] = outputs.get(found[1], {}).get(found[2], "") if found else "a token"
            output = runner / "output.txt"
            output.write_text("")
            (runner / "step.sh").write_text(script(text))
            results[name] = run(["bash", "-e", str(runner / "step.sh")], checkout, GITHUB_OUTPUT=str(output),
                                **environment, **env)
            step_id = re.search(r"(?m)^        id: (\S+)$", text)
            if step_id:
                outputs[step_id[1]] = dict(line.split("=", 1) for line in output.read_text().splitlines())
            failed = failed or results[name].returncode != 0
        return results

    def saved(self, path: str) -> bytes:
        return subprocess.run(["git", "--git-dir", str(self.remote), "show", f"paper-state:{path}"], env=git_env(),
                              capture_output=True, check=True).stdout

    def files(self) -> list[str]:
        return git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "paper-state", cwd=self.tmp).split()

    def test_backups_land_in_data_render_one_commit_a_day_and_never_go_backwards(self):
        first = self.back_up(GITHUB_CHECKS + LOOP_CHECKS[:2])
        results = self.run_job("2026-11-08T15:23:00Z")
        self.assertEqual({name: r.returncode for name, r in results.items()}, dict.fromkeys(results, 0),
                         "\n".join(f"{name}: {r.stdout}{r.stderr}" for name, r in results.items()))
        self.assertEqual(len(results), 7)
        self.assertIn("The bot is running normally", results["Check the bot"].stdout)
        self.assertEqual(self.files(), [".gitattributes", "data/MOVED_TO_RENDER.md",
                                        "data/render/heartbeat_systematic.json", "data/render/manifest.json",
                                        "data/render/paper_systematic.db"])
        self.assertEqual(self.saved("data/render/paper_systematic.db"), first)
        self.assertEqual(self.saved("data/render/heartbeat_systematic.json"), HEARTBEAT_FILE)
        # The fence's day, so its commit was amended: the seed is still its parent, and still tagged.
        log = git("--git-dir", str(self.remote), "log", "--format=%P|%cn <%ce>|%s", "paper-state", cwd=self.tmp)
        self.assertEqual(log.splitlines()[0], f"{self.seed_sha}|{BOT}|Paper state, 2026-11-08 (UTC)")
        self.assertEqual(self.tip("render-seed"), self.seed_sha)
        self.assertEqual(self.tip("main"), self.main)

        # The next day's backup is a commit of its own.
        second = self.back_up(GITHUB_CHECKS + LOOP_CHECKS)
        before = self.tip("paper-state")
        results = self.run_job("2026-11-09T03:23:00Z")
        self.assertTrue(all(r.returncode == 0 for r in results.values()), results)
        self.assertEqual(self.saved("data/render/paper_systematic.db"), second)
        self.assertEqual(git("--git-dir", str(self.remote), "log", "-1", "--format=%P", "paper-state", cwd=self.tmp),
                         before)

        # An older one is refused, nothing after the pull runs but the bot check, and paper-state stays as it was.
        self.back_up(GITHUB_CHECKS + LOOP_CHECKS[:3])
        before = self.tip("paper-state")
        results = self.run_job("2026-11-09T09:23:00Z")
        self.assertEqual(list(results), ["Check Paper run is disabled", "Fetch the seed", "Restore the paper state",
                                         "Pull the backup", "Check the bot"])
        self.assertEqual(results["Pull the backup"].returncode, 1)
        self.assertIn("is older than the copy already saved", results["Pull the backup"].stdout)
        self.assertEqual(results["Check the bot"].returncode, 0)
        self.assertEqual(self.tip("paper-state"), before)
        self.assertEqual(self.saved("data/render/paper_systematic.db"), second)

    def test_while_paper_run_runs_the_test_it_does_nothing(self):
        # Before the cutover, or after going back: Paper run writes paper-state, so this mustn't.
        self.back_up(GITHUB_CHECKS + LOOP_CHECKS)
        before = self.tip("paper-state")
        results = self.run_job("2026-11-08T15:23:00Z", paper_run="active")
        self.assertEqual(list(results), ["Check Paper run is disabled"])
        self.assertEqual(results["Check Paper run is disabled"].returncode, 0)
        self.assertIn("::notice title=Paper run still runs the test::Paper run is active",
                      results["Check Paper run is disabled"].stdout)
        self.assertEqual(self.tip("paper-state"), before)

    def test_a_held_bot_fails_the_job_after_its_copy_is_saved(self):
        first = self.back_up(GITHUB_CHECKS + LOOP_CHECKS)
        self.service.ok = (503, "held")
        results = self.run_job("2026-11-08T15:23:00Z")
        self.assertEqual(results["Save the paper state"].returncode, 0)
        self.assertEqual(self.saved("data/render/paper_systematic.db"), first)
        self.assertEqual(results["Check the bot"].returncode, 1)
        self.assertIn(f"::error title=The bot needs you::{self.service.url}/ok answered 503: held.",
                      results["Check the bot"].stdout)


if __name__ == "__main__":
    unittest.main()
