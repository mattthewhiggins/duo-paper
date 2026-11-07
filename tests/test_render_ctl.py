"""ops/render/ctl.py: the paper bot's controls in the Render service's Shell, and the seed that moves the run there.

Everything ctl.py reaches has a stand-in here. A localhost http.server answers as the supervisor, its state following
the HOLD file and the seeded files the way the real one's does, taking a backup when BACKUP_NOW appears, and starting a
bot that checks at once, as the loop does. Another answers as the bot's dashboard and records what was posted to it.
Downloads come from a folder laid out as raw.githubusercontent.com serves paper-state, and GitHub's branches and tags
from git's ref advertisement, built here as pkt-lines: by default, as they are once the fence is pushed. The clock is
faked, so a wait that runs out does so at once, and is_mount is given, so a temporary folder can be the disk.
Databases are made with the runner's own schema (duo.db and duo.systematic), as paper-state holds them; ctl.py itself
never imports duo.

No network: the only servers are on 127.0.0.1.
"""
import base64
import contextlib
import errno
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from duo.db import DB, SCHEMA as DB_SCHEMA
from duo.systematic import SCHEMA

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("render_ctl", ROOT / "ops" / "render" / "ctl.py")
ctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctl)

STRATEGY = "F1__sma_50__BTC__vol20"
SHA = "c80804de37832d7b686844ad7f881da11faa1a35"  # paper-state's tip on 6 November 2026. Any 40 hex digits would do.
FENCE = "f" * 40  # The commit the fence pushes on top of SHA: the note in place of the database (RUNBOOK 13.3 step 5).
MAIN = "e" * 40
REFS = "https://github.com/mattthewhiggins/duo-paper.git/info/refs?service=git-upload-pack"  # What git ls-remote reads.
NOTE = "MOVED_TO_RENDER.md"
T0 = 1793886825.8507414  # The run's first tick.
GITHUB = [T0 + i * 900 for i in range(4)]  # The heartbeats of four of GitHub's runs, 15 minutes apart.
NOW = GITHUB[-1] + 1200  # The seed, 20 minutes after the last of them.


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def exited(restarts=1, code=1, lines=("Not starting the systematic runner: the start record is for another run",)):
    """/supervisor.json once the bot has exited by itself: backoff, with its exit code and last lines."""
    return {"schema": 1, "state": "backoff", "reason": f"the bot exited with code {code}; it's started again in 30 s",
            "child": {"pid": None, "started_at": None, "restarts": restarts, "tail": [],
                      "last_exit": {"code": code, "at": NOW, "tail": ["Duo paper run, starting.", *lines]}}}


def make_db(path: Path, strategy=STRATEGY, quote="USD", beats=GITHUB, tick_at=GITHUB[-1], state=None, resting=0,
            working=0, start=True) -> None:
    """A paper run's database as paper-state holds it, made with the runner's own schema: its start record, a heartbeat
    at each of beats, sys_tick_at, any other state keys, and children resting on the book or orders working. Every
    commit is in the file itself, as save_state.sh leaves it."""
    db = DB(path)
    db.conn.executescript(SCHEMA)
    if start:
        db.set("sys_start", json.dumps({"strategy_id": strategy, "quote": quote, "family": "F1", "started_at": T0,
                                        "starting_capital": "26862.97", "starting_capital_gbp": "20000"},
                                       sort_keys=True))
    if tick_at is not None:
        db.set("sys_tick_at", tick_at)  # As python -m duo tick stores it.
    for key, value in (state or {}).items():
        db.set(key, value)
    for ts in beats:
        db.execute("INSERT INTO sys_heartbeats(ts, status, halts, controls) VALUES(?, 'ok', '[]', '[]')", (ts,))
    for child in [True] * resting + [False] * working:
        order = db.execute("INSERT INTO sys_orders(source, asset, side, notional_wanted, created_at, status) "
                           "VALUES('decision', 'BTC', 'buy', '500', ?, 'working')", (T0,)).lastrowid
        if child:
            db.execute("INSERT INTO sys_children(order_id, kind, asset, side, qty, limit_price, placed_at, status) "
                       "VALUES(?, 'maker', 'BTC', 'buy', '0.005', '100000', ?, 'open')", (order, T0))
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.conn.close()


def break_index(path: Path) -> None:
    """Makes path fail PRAGMA integrity_check while every table still reads: idx_sys_heartbeats_ts is said to be on
    another column, so its entries no longer match the table's rows."""
    connection = sqlite3.connect(path)
    if hasattr(connection, "setconfig"):  # Python 3.12 and later: SQLite's defensive mode would refuse the edit.
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, False)
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute("UPDATE sqlite_master SET sql = 'CREATE INDEX idx_sys_heartbeats_ts ON sys_heartbeats(status)' "
                       "WHERE name = 'idx_sys_heartbeats_ts'")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()


def start_record(path: Path) -> str:
    """sys_start's value as stored, read without changing anything."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        return connection.execute("SELECT value FROM state WHERE key = 'sys_start'").fetchone()[0]
    finally:
        connection.close()


def disk(folder: Path) -> dict:
    """Everything under folder: each file's bytes and modification time, and each folder."""
    return {path.relative_to(folder).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns) if path.is_file()
            else "folder" for path in folder.rglob("*")}


def nobody() -> str:
    """A URL on 127.0.0.1 where nothing listens."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def refusing(database, *args, **kwargs):
    """sqlite3.connect as a SQLite build that refuses a plain read-only open of a WAL database with no -wal or -shm
    beside it, as Apple's does, whichever SQLite runs the tests."""
    if str(database).endswith("?mode=ro"):
        raise sqlite3.OperationalError("unable to open database file")
    return sqlite3.connect(database, *args, **kwargs)


def accepting(database, *args, **kwargs):
    """sqlite3.connect as a SQLite build that makes that open, as upstream's does, whichever SQLite runs the tests: the
    open is made immutable too, which every build makes."""
    if str(database).endswith("?mode=ro"):
        database = f"{database}&immutable=1"
    return sqlite3.connect(database, *args, **kwargs)


def lone_wal_opens(folder: Path) -> bool:
    """Whether this SQLite makes the supervisor's plain read-only open of the run's database, placed as the seed places
    it: a WAL database, every commit in the file, and no -wal or -shm beside it."""
    database = folder / ctl.DB_NAME
    make_db(database)
    for suffix in ("-wal", "-shm"):  # Some builds keep them after the last close; the seed never places them.
        Path(f"{database}{suffix}").unlink(missing_ok=True)
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            connection.execute("SELECT COUNT(*) FROM sys_heartbeats").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return True


def pkt_lines(*lines: bytes) -> bytes:
    """lines as git's pkt-lines: each after its length in 4 hex digits, which count themselves. b"" is a flush."""
    return b"".join(b"%04x" % (len(line) + 4) + line if line else b"0000" for line in lines)


def advertisement(refs: dict) -> bytes:
    """refs, {name: commit}, as GitHub's info/refs advertises them to git ls-remote: the service's line and a flush,
    then a line for each ref, the first with the server's capabilities after a NUL, then a flush."""
    lines = [f"{commit} {name}".encode() for name, commit in refs.items()]
    if lines:
        lines[0] += (b"\0multi_ack thin-pack side-band side-band-64k ofs-delta shallow no-progress include-tag "
                     b"symref=HEAD:refs/heads/main object-format=sha1 agent=git/github-0123456789ab-Linux")
    return pkt_lines(b"# service=git-upload-pack\n", b"", *(line + b"\n" for line in lines), b"")


class Remote:
    """GitHub as the seed and check reach it: raw.githubusercontent.com from a folder holding <commit>/<path>, and the
    repository's branches and tags, refs, advertised at REFS (bytes are served as they are, and None is a 404).
    Anything missing is a 404, and a URL in failures fails that many times first. Every URL asked for is in urls."""

    def __init__(self, folder: Path):
        self.folder, self.failures, self.urls = folder, {}, []
        # As they are once the fence is pushed: paper-state at the fence, and render-seed marking SHA.
        self.refs = {"HEAD": MAIN, "refs/heads/main": MAIN, "refs/heads/paper-state": FENCE,
                     "refs/tags/render-seed": SHA}

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if self.failures.get(url):
            self.failures[url] -= 1
            raise urllib.error.URLError("connection reset by peer")
        if url == REFS and self.refs is not None:
            return self.refs if isinstance(self.refs, bytes) else advertisement(self.refs)
        path = self.folder / url.removeprefix(ctl.RAW + "/")
        if not url.startswith(ctl.RAW + "/") or not path.is_file():
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"404: Not Found"))
        return path.read_bytes()


class StandIn:
    """An HTTP server on 127.0.0.1. It records each request as (method, path, headers with lower-case names) and
    answers with answer(method, path), a status and a JSON body."""

    def __init__(self, answer):
        requests = self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
                status, payload = answer(self.command, self.path)
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # A short poll, or each shutdown waits out serve_forever's default half second.
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Shell(unittest.TestCase):
    """A temporary disk, the stand-ins, and python3 ops/render/ctl.py run against them."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.data = self.tmp / "disk"
        self.data.mkdir()
        self.remote = Remote(self.tmp / "raw")
        # The fence's commit holds the note and no database, and main has .python-version, as on GitHub.
        (self.tmp / "raw" / FENCE / "data").mkdir(parents=True)
        (self.tmp / "raw" / FENCE / "data" / NOTE).write_text("# The paper run moved to Render\n")
        (self.tmp / "raw" / "main").mkdir()
        (self.tmp / "raw" / "main" / ".python-version").write_text("3.13\n")
        self.config = self.tmp / "config.toml"
        self.config.write_text(f'mode = "paper"\n\n[portfolio]\nquote = "USD"\n\n'
                               f'[systematic]\nenabled = true\nstrategy = "{STRATEGY}"\n')
        # The service's Python is this one, so the SQLite self-test runs in this process, on the stand-in for
        # sqlite3.connect: whichever SQLite this machine has, it passes unless a test says otherwise.
        (self.tmp / "venv" / "bin").mkdir(parents=True)
        (self.tmp / "venv" / "bin" / "python").symlink_to(sys.executable)
        self.environ = {"DUO_DATA_DIR": str(self.data), "VENV_ROOT": str(self.tmp / "venv")}
        self.now, self.slept, self.ran, self.typed = NOW, [], [], []
        self.pinned = []  # /supervisor.json answers to give in turn, the last for good, whatever is on the disk.
        self.started = None  # When the supervisor's stand-in last started the bot, while it runs.
        self.backup = {"at": None, "stamp": None, "sha256": None, "error": None}
        self.supervisor = StandIn(self.supervisor_json)
        self.addCleanup(self.supervisor.close)
        self.dashboard = StandIn(lambda method, path: (200, {"ok": True}))
        self.addCleanup(self.dashboard.close)

    def supervisor_json(self, method, path):
        """As ops/render/serve.py answers: held while HOLD is there, running once the database and the marker are, and
        waiting before that. BACKUP_NOW is a backup taken at once. The bot it starts checks at once, as the loop does,
        writing the heartbeat file."""
        if path != "/supervisor.json":
            return 404, {}
        if self.pinned:
            return 200, self.pinned.pop(0) if len(self.pinned) > 1 else self.pinned[0]
        if (self.data / ctl.BACKUP_NOW).exists():
            stamp = datetime.fromtimestamp(self.now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            (self.data / "backups" / stamp).mkdir(parents=True)
            (self.data / "backups" / stamp / "manifest.json").write_text(
                json.dumps({"schema": 1, "taken_at": self.now, "integrity": "ok"}))
            self.backup = {"at": self.now, "stamp": stamp, "sha256": "ab" * 32, "error": None}
            (self.data / ctl.BACKUP_NOW).unlink()
        if (self.data / ctl.HOLD).exists():
            state, reason = "held", "HOLD is there"
        elif (self.data / ctl.DB_NAME).exists() and (self.data / ctl.MARKER).exists():
            state, reason = "running", ""
        else:
            state, reason = "waiting", "no paper_systematic.db on the disk"
        if state != "running":
            self.started = None
        elif self.started is None:
            self.started = self.now
            (self.data / ctl.HEARTBEAT).write_text(json.dumps({"ts": self.now, "status": "ok", "schedule": "loop"}))
        child = {"pid": None if self.started is None else 4242, "started_at": self.started, "restarts": 0,
                 "last_exit": None, "tail": []}
        return 200, {"schema": 1, "now": self.now, "state": state, "reason": reason, "hold": state == "held",
                     "child": child, "backup": self.backup, "data_dir": {"path": str(self.data)}}

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def duo(self, command, cwd, env):
        self.ran.append((command, cwd, env))
        return 0

    def ask(self, prompt):
        if not self.typed:
            raise EOFError  # The end of input, as Ctrl-D gives.
        return self.typed.pop(0)

    def controls(self, **options) -> ctl.Controls:
        settings = dict(environ=self.environ, config=self.config, fetch=self.remote,
                        is_mount=lambda path: Path(path) == self.data, supervisor=self.supervisor.url,
                        dashboard=self.dashboard.url, clock=lambda: self.now, sleep=self.sleep, run=self.duo,
                        ask=self.ask, connect=accepting)
        settings.update(options)
        return ctl.Controls(**settings)

    def shell(self, *argv, **options) -> tuple[int, str]:
        """python3 ops/render/ctl.py ARGV: its exit code, and what it printed."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ctl.main(list(argv), self.controls(**options))
        return code, out.getvalue()

    def seeded(self):
        """The disk as the seed leaves it, as far as the supervisor's stand-in looks."""
        for name in (ctl.DB_NAME, ctl.MARKER):
            (self.data / name).write_text("")

    def paper_state(self, heartbeat=True, corrupt=False, **db) -> str:
        """paper-state at SHA on the stand-in for raw.githubusercontent.com: a database from make_db(**db), and the
        heartbeat file. Returns the database's sha256, as the owner works it out before seeding."""
        built = Path(tempfile.mkdtemp(dir=self.tmp)) / ctl.DB_NAME  # SQLite may leave a -wal and -shm beside it.
        make_db(built, **db)
        if corrupt:
            break_index(built)
        served = self.tmp / "raw" / SHA / "data"
        served.mkdir(parents=True, exist_ok=True)
        (served / ctl.DB_NAME).write_bytes(built.read_bytes())
        if heartbeat:
            (served / ctl.HEARTBEAT).write_text(json.dumps({"ts": GITHUB[-1], "status": "ok", "schedule": "cron"}))
        return hashlib.sha256(built.read_bytes()).hexdigest()


FENCED = [REFS] + [f"{ctl.RAW}/{FENCE}/data/{name}" for name in (ctl.DB_NAME, NOTE)]  # What the seed reads first.


class Seed(Shell):
    def assert_refused(self, digest: str, says: str, *flags: str, before_anything=False, **options):
        """Seeding SHA with digest is refused, saying `says`, both with no HOLD and with one already there, and leaves
        the disk exactly as it was: a HOLD the seed made is gone, and one that was there stays as it was. Refused
        before_anything, it made no HOLD and downloaded nothing at SHA."""
        for held in (False, True):
            with self.subTest(hold_before=held):
                if held:
                    (self.data / ctl.HOLD).write_text("Held by hand.\n")
                before = disk(self.data)
                self.remote.urls.clear()
                code, out = self.shell("seed", SHA, "--sha256", digest, *flags, **options)
                self.assertEqual(code, 1, out)
                self.assertIn(says, out)
                self.assertIn("Nothing was done." if before_anything else "Nothing was placed", out)
                self.assertEqual(disk(self.data), before)
                if before_anything:
                    self.assertNotIn("Made HOLD", out)
                    self.assertEqual([url for url in self.remote.urls if url.startswith(f"{ctl.RAW}/{SHA}/")], [])
                (self.data / ctl.HOLD).unlink(missing_ok=True)

    def test_a_good_seed_places_the_heartbeat_then_the_database_then_the_marker(self):
        digest = self.paper_state()
        moved, replace = {}, os.replace

        def recorded(source, destination):
            replace(source, destination)
            moved[Path(destination).name] = Path(destination).read_bytes()

        with mock.patch.object(ctl.os, "replace", recorded):
            code, out = self.shell("seed", SHA, "--sha256", digest)
        self.assertEqual(code, 0, out)
        self.assertEqual(list(moved), [ctl.HEARTBEAT, ctl.DB_NAME, ctl.MARKER])
        served = self.tmp / "raw" / SHA / "data"
        for name in (ctl.DB_NAME, ctl.HEARTBEAT):  # As placed: the bot has written its own heartbeat since.
            self.assertEqual(moved[name], (served / name).read_bytes(), name)
        # The HOLD it made is gone, and so are its staging folder and the marker's temporary file.
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), sorted([ctl.DB_NAME, ctl.HEARTBEAT, ctl.MARKER]))
        # It waited for the loop's first check, the heartbeat written once the bot started.
        self.assertIn(f"Seeded, and the bot is running on Render: the loop's first check was at {utc(NOW)}. Next "
                      f"(RUNBOOK 13.3): watch the page until it says Running normally, and /ok until it answers 200.",
                      out)
        self.assertNotIn("until the loop's first heartbeat", out)
        # The supervisor's first backup comes before the loop's first check, and GitHub won't keep that one. It takes
        # the next by itself once the loop has checked, and the workflow waits for that.
        self.assertIn("The supervisor backs up by itself within about 5 minutes of that first check (backup.at in "
                      "/supervisor.json changes); only then run the Render backup workflow once by hand.", out)
        self.assertIn("python3 ops/render/ctl.py backup forces one sooner", out)
        for key in ("sys_kill", "sys_waiting", "sys_degraded_since", "sys_tick_at", "newest heartbeat"):
            self.assertIn(key, out)
        self.assertIn("ok, 20 minutes ago", out)
        # GitHub's refs, then the fence's commit, and only then the files at SHA.
        self.assertEqual(self.remote.urls, FENCED + [f"{ctl.RAW}/{SHA}/data/{name}"
                                                     for name in (ctl.DB_NAME, f"{ctl.DB_NAME}-wal", ctl.HEARTBEAT)])
        self.assertIn(f"GitHub: the fence is on paper-state (its tip, {FENCE[:7]}, ", out)
        self.assertIn(f"the tag render-seed marks {SHA[:7]}", out)

    def test_the_marker_holds_where_githubs_runs_ended_exactly(self):
        # The last tick's own heartbeat is at sys_tick_at exactly. One after it, as a loop's would be, isn't GitHub's.
        digest = self.paper_state(beats=GITHUB + [GITHUB[-1] + 300], tick_at=GITHUB[-1])
        code, out = self.shell("seed", SHA, "--sha256", digest)
        self.assertEqual(code, 0, out)
        marker = json.loads((self.data / ctl.MARKER).read_text())
        self.assertEqual(marker, {
            "schema": 1, "sha": SHA, "db_sha256": digest, "seeded_at": NOW,
            "seeded_at_utc": datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "sys_tick_at": GITHUB[-1], "last_cron_at": GITHUB[-1], "cron_runs": 4,
            "start_sha256": hashlib.sha256(start_record(self.data / ctl.DB_NAME).encode("utf-8")).hexdigest(),
        })
        self.assertEqual(list(marker), ["schema", "sha", "db_sha256", "seeded_at", "seeded_at_utc", "sys_tick_at",
                                        "last_cron_at", "cron_runs", "start_sha256"])

    def test_a_database_github_never_checked_is_refused(self):
        # Its marker's last_cron_at would be null: pull_backup.py would refuse every backup for the rest of the run,
        # and the supervisor would never take the backup GitHub keeps.
        said = "the database has no check from GitHub at or before its last tick (sys_tick_at {}), so it isn't"
        self.assert_refused(self.paper_state(tick_at=None), said.format("not set"))
        early = GITHUB[0] - 60  # Before every heartbeat.
        self.assert_refused(self.paper_state(tick_at=early), said.format(utc(early)))

    def test_a_commit_without_a_heartbeat_file_is_seeded_without_one(self):
        digest, moved, replace = self.paper_state(heartbeat=False), [], os.replace

        def recorded(source, destination):
            moved.append(Path(destination).name)
            replace(source, destination)

        with mock.patch.object(ctl.os, "replace", recorded):
            code, out = self.shell("seed", SHA, "--sha256", digest)
        self.assertEqual(code, 0, out)
        self.assertIn(f"{ctl.HEARTBEAT}: none at that commit", out)
        self.assertEqual(moved, [ctl.DB_NAME, ctl.MARKER])
        self.assertIn(f"the loop's first check was at {utc(NOW)}", out)  # The bot's own, its first.

    def test_a_hold_that_was_there_before_stays_and_the_bot_stays_stopped(self):
        digest = self.paper_state()
        (self.data / ctl.HOLD).write_text("Held for the cutover.\n")
        before = disk(self.data)[ctl.HOLD]
        code, out = self.shell("seed", SHA, "--sha256", digest)
        self.assertEqual(code, 0, out)
        self.assertEqual(disk(self.data)[ctl.HOLD], before)
        self.assertIn("HOLD was there before the seed, so it stays", out)
        self.assertTrue((self.data / ctl.MARKER).exists())
        self.assertEqual((self.supervisor.requests, self.slept), ([], []))  # No waiting for a bot that can't start.

    def test_a_hold_while_the_seed_runs_takes_its_hold_over_and_keeps_the_bot_stopped(self):
        theirs, ours = "Made by python3 ops/render/ctl.py seed, ", "Made by python3 ops/render/ctl.py hold, "
        for name, digest in (("seeded", self.paper_state()), ("refused", "0" * 64)):
            with self.subTest(name):
                held = []

                def fetch(url):
                    if url == f"{ctl.RAW}/{SHA}/data/{ctl.DB_NAME}":  # Midway: the seed's HOLD is there.
                        self.assertTrue((self.data / ctl.HOLD).read_text().startswith(theirs))
                        held.append(self.shell("hold"))
                    return self.remote(url)

                self.supervisor.requests.clear()
                code, out = self.shell("seed", SHA, "--sha256", digest, fetch=fetch)
                (held_code, held_out), = held
                self.assertEqual(held_code, 0, held_out)
                self.assertIn("That HOLD was the seed's, which it takes away when it's done: it's this hold's now, so "
                              "the bot stays stopped after the seed until python3 ops/render/ctl.py unhold.", held_out)
                self.assertTrue((self.data / ctl.HOLD).read_text().startswith(ours), "the seed left hold's HOLD")
                self.assertEqual(len(self.supervisor.requests), 1, "only hold's own wait, which found it held")
                self.assertEqual((self.slept, sorted(p.name for p in self.data.glob(".*"))), ([], []))
                if name == "seeded":
                    self.assertEqual(code, 0, out)
                    self.assertIn("HOLD was replaced while the seed ran (python3 ops/render/ctl.py hold), so it stays, "
                                  "and the bot stays stopped until python3 ops/render/ctl.py unhold (RUNBOOK 13.6).",
                                  out)
                    self.assertTrue((self.data / ctl.MARKER).exists())
                    self.assertNotIn("Waiting for", out)
                    for path in self.data.iterdir():
                        path.unlink()
                else:
                    self.assertEqual(code, 1, out)
                    self.assertIn("Not seeded: the downloaded database's sha256 is", out)
                    self.assertEqual(sorted(p.name for p in self.data.iterdir()), [ctl.HOLD])
                    (self.data / ctl.HOLD).unlink()

    def test_a_hold_that_cant_take_the_seeds_over_leaves_it_and_tidies_up(self):
        (self.data / ctl.HOLD).write_text("Made by python3 ops/render/ctl.py seed, 2026-11-07 10:00:00 UTC.\n")
        before, replace = disk(self.data), os.replace

        def full(source, destination):
            if Path(destination).name == ctl.HOLD:
                raise OSError(errno.ENOSPC, "No space left on device")
            replace(source, destination)

        with mock.patch.object(ctl.os, "replace", full):
            code, out = self.shell("hold")
        self.assertEqual(code, 1, out)
        self.assertIn("HOLD is the seed's, and it couldn't be made this hold's (OSError: [Errno 28] No space left on "
                      "device)", out)
        self.assertEqual(disk(self.data), before)  # The seed's HOLD as it was, and no temporary file left.
        self.assertEqual(self.supervisor.requests, [])

    def test_a_seed_whose_bot_doesnt_start_says_what_the_supervisor_is_waiting_for(self):
        digest = self.paper_state()
        self.pinned = [{"schema": 1, "state": "waiting", "reason": "something already listens on 127.0.0.1:8787"}]
        code, out = self.shell("seed", SHA, "--sha256", digest)
        self.assertEqual(code, 1, out)
        self.assertIn("Seeded, but the loop hasn't checked after 4 minutes: waiting: something already listens on "
                      "127.0.0.1:8787. That's what the supervisor is waiting for", out)
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), sorted([ctl.DB_NAME, ctl.HEARTBEAT, ctl.MARKER]))
        self.assertEqual((sum(self.slept), set(self.slept)), (ctl.START_SECONDS, {ctl.POLL_SECONDS}))
        self.assertEqual((ctl.START_SECONDS, ctl.UNHOLD_SECONDS, ctl.POLL_SECONDS), (240, 240, 5))

    def test_a_seed_whose_bot_exits_at_once_says_so_with_its_last_lines(self):
        digest = self.paper_state()
        running = {"schema": 1, "state": "running", "reason": "",
                   "child": {"pid": 4242, "started_at": NOW, "restarts": 0, "last_exit": None, "tail": []}}
        lines = [f"line {i}" for i in range(6)] + ["Not starting the systematic runner: the start record is another's"]
        for name, answers in (("backoff", [running, exited(lines=lines)]),
                              ("started again already", [running, {**running, "child": {
                                  **exited(lines=lines)["child"], "pid": 4343, "started_at": NOW + 30}}])):
            with self.subTest(name):
                for path in self.data.iterdir():
                    path.unlink()
                self.slept.clear()
                self.pinned = list(answers)
                code, out = self.shell("seed", SHA, "--sha256", digest)
                self.assertEqual(code, 1, out)
                self.assertIn("Seeded, but the bot exited at once: ", out)
                self.assertIn("(RUNBOOK 13.3 step 6, and 13.4 for the reasons)", out)
                self.assertIn("It exited with code 1, and its last lines were:\n" +
                              "".join(f"    {line}\n" for line in lines[-5:]), out)
                self.assertNotIn("line 1\n", out)
                self.assertEqual(self.slept, [ctl.POLL_SECONDS], "it stopped waiting at the second answer")

    def test_a_seed_whose_loop_never_checks_says_so_after_four_minutes(self):
        digest = self.paper_state()
        self.pinned = [{"schema": 1, "state": "running", "reason": "",
                        "child": {"pid": 4242, "started_at": NOW, "restarts": 0, "last_exit": None, "tail": []}}]
        code, out = self.shell("seed", SHA, "--sha256", digest)
        self.assertEqual(code, 1, out)
        self.assertIn(f"Seeded, but the loop hasn't checked after 4 minutes: the supervisor says the bot is running, "
                      f"but {ctl.HEARTBEAT} has no check since {utc(NOW)} (its newest is from {utc(GITHUB[-1])}). "
                      f"The service's Logs on Render say more (RUNBOOK 13.3 step 6, and 13.4 for the reasons).", out)
        self.assertEqual(sum(self.slept), ctl.START_SECONDS)

    def test_a_seed_the_supervisor_doesnt_answer_goes_by_the_heartbeat(self):
        digest, nowhere = self.paper_state(), nobody()
        code, out = self.shell("seed", SHA, "--sha256", digest, supervisor=nowhere)
        self.assertEqual(code, 1, out)
        self.assertIn(f"Seeded, but the loop hasn't checked after 4 minutes: the supervisor doesn't answer from this "
                      f"Shell (URLError: ", out)
        self.assertIn("), so open the service's /supervisor.json in a browser to see what it's doing", out)
        for path in self.data.iterdir():
            path.unlink()

        def sleep(seconds):  # The bot starts, and checks, while the seed waits.
            self.sleep(seconds)
            (self.data / ctl.HEARTBEAT).write_text(json.dumps({"ts": self.now, "status": "ok"}))

        self.now, self.slept = NOW, []
        code, out = self.shell("seed", SHA, "--sha256", digest, supervisor=nowhere, sleep=sleep)
        self.assertEqual(code, 0, out)
        self.assertIn(f"Seeded, and the bot is running on Render: the loop's first check was at "
                      f"{utc(NOW + ctl.POLL_SECONDS)}.", out)

    def test_a_commit_in_capitals_is_the_same_commit(self):
        code, out = self.shell("seed", SHA.upper(), "--sha256", self.paper_state().upper())
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads((self.data / ctl.MARKER).read_text())["sha"], SHA)

    def test_a_commit_or_digest_that_isnt_hex_is_refused_before_anything_is_done(self):
        digest = self.paper_state()
        for sha, given in ((SHA[:39], digest), (SHA + "0", digest), ("g" * 40, digest), (SHA, digest[:63]),
                           (SHA, "z" * 64)):
            with self.subTest(sha=sha, sha256=given):
                code, out = self.shell("seed", sha, "--sha256", given)
                self.assertEqual(code, 2, out)
                self.assertIn("40 hex digits", out)
        self.assertEqual((disk(self.data), self.remote.urls), ({}, []))

    def test_it_never_writes_over_anything_on_the_disk_and_tells_the_owner_to_delete_nothing(self):
        digest, db, hb = self.paper_state(), ctl.DB_NAME, ctl.HEARTBEAT
        ending = "Delete nothing (RUNBOOK 13.10): have the assistant look (RUNBOOK 13.3 step 6). Nothing was changed."
        ours = {"schema": 1, "sha": SHA, "db_sha256": digest, "seeded_at": T0,
                "seeded_at_utc": "2026-11-05 13:53:45 UTC"}
        cut_off = "on the disk but render_seed.json isn't, so a seed was cut off after it placed the database."
        for name, files, says in (
                ("another commit", {db: "", ctl.MARKER: {**ours, "sha": "1" * 40}},
                 f"Not seeded: the disk was already seeded from {'1' * 40} at 2026-11-05 13:53:45 UTC "
                 f"(render_seed.json), and the seed never runs twice or writes over a run."),
                ("another digest", {db: "", ctl.MARKER: {**ours, "db_sha256": "2" * 64}},
                 f"Not seeded: the disk was already seeded from {SHA} at 2026-11-05 13:53:45 UTC (render_seed.json)"),
                ("the database alone", {db: ""}, f"Not seeded: {db} is {cut_off}"),
                ("its -wal alone", {f"{db}-wal": ""}, f"Not seeded: {db}-wal is {cut_off}"),
                ("its -shm alone", {f"{db}-shm": ""}, f"Not seeded: {db}-shm is {cut_off}"),
                ("the database and a heartbeat", {db: "", hb: "{}"}, f"Not seeded: {db}, {hb} are {cut_off}"),
                ("a heartbeat alone", {hb: "{}"},
                 f"Not seeded: {hb} is already in {self.data}, and the seed never writes over anything."),
                ("a marker alone", {ctl.MARKER: ours}, f"Not seeded: {ctl.MARKER} is already in {self.data}"),
                ("a marker that isn't JSON", {db: "", ctl.MARKER: "{half"},
                 f"Not seeded: {db}, {ctl.MARKER} are already in {self.data}"),
                ("a marker that isn't an object", {db: "", ctl.MARKER: [SHA]}, f"{db}, {ctl.MARKER} are already in")):
            with self.subTest(name):
                for path in self.data.iterdir():
                    path.unlink()
                for file, text in files.items():
                    (self.data / file).write_text(text if isinstance(text, str) else json.dumps(text))
                before = disk(self.data)
                code, out = self.shell("seed", SHA, "--sha256", digest)
                self.assertEqual(code, 1, out)
                self.assertIn(says, out)
                self.assertTrue(out.rstrip("\n").endswith(ending), out)
                self.assertEqual(disk(self.data), before)
        self.assertEqual((self.remote.urls, self.supervisor.requests), ([], []))

    def test_the_seed_run_again_on_its_own_run_says_so_and_changes_nothing(self):
        digest = self.paper_state()
        self.assertEqual(self.shell("seed", SHA, "--sha256", digest)[0], 0)
        marker = json.loads((self.data / ctl.MARKER).read_text())
        self.remote.urls.clear()
        for name, hold, says in (
                ("running", None, "The supervisor's state: running."),
                ("cut off before it took its HOLD away", "Made by python3 ops/render/ctl.py seed, 2026-11-07.\n",
                 "The seed was cut off before it took its HOLD away: python3 ops/render/ctl.py unhold starts the bot "
                 "(RUNBOOK 13.3 step 7)."),
                ("held by hand", "Held for a deploy.\n",
                 "HOLD is there, so the bot stays stopped until python3 ops/render/ctl.py unhold (RUNBOOK 13.6).")):
            with self.subTest(name):
                if hold:
                    (self.data / ctl.HOLD).write_text(hold)
                before = disk(self.data)
                code, out = self.shell("seed", SHA.upper(), "--sha256", digest)
                self.assertEqual(code, 0, out)
                self.assertTrue(out.startswith(f"Already seeded from {SHA} at {marker['seeded_at_utc']}: the run is on "
                                               f"the disk, and the seed never runs twice. Nothing was changed.\n"), out)
                self.assertIn(says, out)
                self.assertEqual(disk(self.data), before)
        self.assertEqual((self.remote.urls, self.slept), ([], []))

    def test_ctrl_c_while_the_seed_waits_for_the_bot_says_the_seed_is_done(self):
        digest = self.paper_state()
        self.pinned = [{"schema": 1, "state": "waiting", "reason": "starting"}]

        def interrupted(seconds):
            raise KeyboardInterrupt

        code, out = self.shell("seed", SHA, "--sha256", digest, sleep=interrupted)
        self.assertEqual(code, 130, out)
        self.assertIn("\nStopped waiting. The seed is done (render_seed.json is there), and the supervisor starts the "
                      "bot by itself: /supervisor.json and the page show how it goes (RUNBOOK 13.3 step 7).\n", out)
        self.assertNotIn("\nStopped.\n", out)
        self.assertTrue((self.data / ctl.MARKER).exists())
        self.assertFalse((self.data / ctl.HOLD).exists())

    def test_a_download_that_isnt_the_sha256_given_is_refused(self):
        self.paper_state()
        self.assert_refused("0" * 64, "the downloaded database's sha256 is")

    def test_a_database_that_fails_its_integrity_check_is_refused(self):
        self.assert_refused(self.paper_state(corrupt=True), "the database fails SQLite's integrity check")

    def test_a_run_of_another_strategy_or_in_another_quote_is_refused(self):
        for db, says in (({"strategy": "F1__ENS__BTC_ETH__vol40"}, "a run of F1__ENS__BTC_ETH__vol40 in USD"),
                         ({"quote": "GBP"}, f"a run of {STRATEGY} in GBP")):
            with self.subTest(**db):
                self.assert_refused(self.paper_state(**db), says)

    def test_a_config_toml_with_the_systematic_runner_off_is_refused_before_anything_is_done(self):
        # python -m duo run would start the agents' paper engine on paper.db, never this run. A missing enabled is
        # false to duo, and so it is here.
        digest, on, off = self.paper_state(), self.config.read_text(), "the systematic runner off"
        for name, text, says in (("false", on.replace("enabled = true", "enabled = false"), off),
                                 ("missing", on.replace("enabled = true\n", ""), off),
                                 ("not true or false", on.replace("enabled = true", "enabled = 1"),
                                  "isn't true or false")):
            with self.subTest(name):
                self.config.write_text(text)
                code, out = self.shell("seed", SHA, "--sha256", digest)
                self.assertEqual(code, 1, out)
                self.assertIn("Not seeded: config.toml", out)
                self.assertIn(says, out)
                self.assertIn("(RUNBOOK 13.10). Nothing was done.", out)
        self.assertEqual((disk(self.data), self.remote.urls), ({}, []), "no HOLD made and nothing downloaded")

    def test_a_database_without_a_start_record_is_refused(self):
        self.assert_refused(self.paper_state(start=False), "the database holds no paper run")

    def test_children_resting_on_the_book_or_orders_working_are_refused(self):
        self.assert_refused(self.paper_state(resting=1), "1 order(s) are resting on the book")
        self.assert_refused(self.paper_state(working=2), "2 order(s) are still working")

    def test_a_pending_sell_everything_is_refused_unless_allowed(self):
        digest = self.paper_state(state={"flatten_requested": "1", "flatten_requested_by": "the command line"})
        self.assert_refused(digest, "Seed with --allow-flatten if that's what you want")
        code, out = self.shell("seed", SHA, "--sha256", digest, "--allow-flatten")
        self.assertEqual(code, 0, out)
        self.assertIn("yes, allowed by --allow-flatten", out)

    def test_a_wal_beside_the_database_on_paper_state_is_refused(self):
        digest = self.paper_state()
        (self.tmp / "raw" / SHA / "data" / f"{ctl.DB_NAME}-wal").write_bytes(b"\0" * 32)
        self.assert_refused(digest, f"paper-state has data/{ctl.DB_NAME}-wal")

    def test_a_commit_without_a_database_is_refused(self):
        self.assert_refused("ab" * 32, f"there's no data/{ctl.DB_NAME} at {SHA}")

    def test_a_heartbeat_file_that_isnt_json_is_refused(self):
        digest = self.paper_state()
        (self.tmp / "raw" / SHA / "data" / ctl.HEARTBEAT).write_text("{half a heartbe")
        self.assert_refused(digest, f"data/{ctl.HEARTBEAT} at {SHA} isn't a JSON object")

    def test_a_download_is_tried_three_times_before_the_seed_gives_up(self):
        digest = self.paper_state()
        url = f"{ctl.RAW}/{SHA}/data/{ctl.DB_NAME}"
        self.remote.failures[url] = 3
        code, out = self.shell("seed", SHA, "--sha256", digest)
        self.assertEqual(code, 1, out)
        self.assertIn(f"{url} couldn't be downloaded in 3 tries", out)
        self.assertEqual((self.remote.urls, self.slept), (FENCED + [url] * 3, [ctl.FETCH_PAUSE] * 2))
        self.assertEqual(disk(self.data), {})
        self.remote.failures[url] = 2
        code, out = self.shell("seed", SHA, "--sha256", digest)
        self.assertEqual(code, 0, out)

    def test_a_staging_folder_left_by_another_seed_is_refused_and_left_alone(self):
        digest = self.paper_state()
        (self.data / ctl.INCOMING).mkdir()
        (self.data / ctl.INCOMING / ctl.DB_NAME).write_bytes(b"half a download")
        self.assert_refused(digest, "another seed is running, or one was cut off")

    def test_a_seed_that_fails_moving_its_files_keeps_hold_and_what_it_placed(self):
        digest, replace = self.paper_state(), os.replace
        for error, why in ((OSError(errno.ENOSPC, "No space left on device"), "OSError: [Errno 28] No space left on "
                                                                              "device"),
                           (KeyboardInterrupt(), "interrupted")):
            for held in (False, True):
                with self.subTest(error=why, hold_before=held):
                    shutil.rmtree(self.data)
                    self.data.mkdir()
                    if held:
                        (self.data / ctl.HOLD).write_text("Held by hand.\n")
                    before = disk(self.data).get(ctl.HOLD)

                    def failing(source, destination):
                        if Path(destination).name == ctl.DB_NAME:
                            raise error
                        replace(source, destination)

                    self.slept.clear()
                    self.supervisor.requests.clear()
                    with mock.patch.object(ctl.os, "replace", failing):
                        code, out = self.shell("seed", SHA, "--sha256", digest)
                    self.assertEqual(code, 1, out)
                    self.assertIn(f"The seed failed while moving its files into place ({why}). In {self.data} now: "
                                  f"{ctl.HEARTBEAT}. HOLD stays, so the bot can't start. Delete nothing (RUNBOOK "
                                  f"13.10): have the assistant look (RUNBOOK 13.3 step 6).", out)
                    self.assertTrue((self.data / ctl.HOLD).exists(), "HOLD stays, so the bot can't start")
                    if held:
                        self.assertEqual(disk(self.data)[ctl.HOLD], before)  # Byte for byte, and its time too.
                    else:
                        self.assertTrue((self.data / ctl.HOLD).read_text().startswith(ctl.SEED_HOLD))
                    self.assertTrue((self.data / ctl.INCOMING / ctl.DB_NAME).is_file(), "the download is kept")
                    self.assertFalse((self.data / ctl.MARKER).exists())
                    self.assertEqual((self.slept, self.supervisor.requests), ([], []), "nothing waited")

    def test_a_database_that_appears_while_the_seed_downloads_is_never_written_over(self):
        digest, planted = self.paper_state(), b"Another bot's database.\n"

        def fetch(url):
            if url == f"{ctl.RAW}/{SHA}/data/{ctl.DB_NAME}":
                (self.data / ctl.DB_NAME).write_bytes(planted)
            return self.remote(url)

        code, out = self.shell("seed", SHA, "--sha256", digest, fetch=fetch)
        self.assertEqual(code, 1, out)
        self.assertIn(f"Not seeded: {ctl.DB_NAME} appeared in {self.data} while the seed ran, and it never writes "
                      f"over anything. Nothing was placed, and everything the seed made is gone.", out)
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), [ctl.DB_NAME], "its HOLD and .incoming gone")
        self.assertEqual((self.data / ctl.DB_NAME).read_bytes(), planted)

    def test_a_seed_cut_off_by_ctrl_c_tidies_up_after_itself(self):
        def interrupted(url):
            if url.startswith(f"{ctl.RAW}/{SHA}/"):  # While it downloads, after HOLD.
                raise KeyboardInterrupt
            return self.remote(url)

        self.assert_refused(self.paper_state(), "Not seeded: interrupted", fetch=interrupted)

    # ---- The fence on GitHub, which the seed checks before it changes anything (RUNBOOK 13.3 step 5) ----

    def test_the_seed_refuses_when_githubs_branches_and_tags_cant_be_read(self):
        digest, step = self.paper_state(), "(RUNBOOK 13.3 step 5). Nothing was done."
        self.remote.failures[REFS] = 6  # Three tries in each of assert_refused's two seeds.
        self.assert_refused(digest, f"GitHub's branches and tags couldn't be read: {REFS} couldn't be downloaded in 3 "
                                    f"tries (URLError", before_anything=True)
        for name, answer, says in (
                ("a 404", None, f"{REFS} answered 404"),
                ("not git's refs", b"<!doctype html><title>Rate limited</title>", f"{REFS} isn't git's list of them"),
                ("cut short", advertisement(self.remote.refs)[:-30], f"{REFS} isn't git's list of them"),
                ("no service line", pkt_lines(f"{FENCE} refs/heads/paper-state\n".encode(), b""), "# service=")):
            with self.subTest(name):
                self.remote.refs = answer
                self.assert_refused(digest, f"GitHub's branches and tags couldn't be read: ", before_anything=True)
                self.assert_refused(digest, says, before_anything=True)
                self.assert_refused(digest, step, before_anything=True)

    def test_the_seed_refuses_until_the_fence_is_on_paper_state(self):
        digest, refs, tip = self.paper_state(), self.remote.refs, self.tmp / "raw" / FENCE / "data"
        step = "(RUNBOOK 13.3 step 5). Nothing was done."
        self.remote.refs = {name: commit for name, commit in refs.items() if name != "refs/heads/paper-state"}
        self.assert_refused(digest, "GitHub has no paper-state branch", before_anything=True)
        self.assert_refused(digest, step, before_anything=True)
        self.remote.refs = {**refs, "refs/heads/paper-state": SHA}  # The fence's push hasn't happened, or was refused.
        self.assert_refused(digest, f"paper-state's tip on GitHub is still {SHA}, so the fence isn't pushed yet",
                            before_anything=True)
        self.assert_refused(digest, step, before_anything=True)
        self.remote.refs = refs
        # A run saved after SHA: its tip still has the database, so SHA isn't the last state GitHub saved.
        shutil.copyfile(self.tmp / "raw" / SHA / "data" / ctl.DB_NAME, tip / ctl.DB_NAME)
        self.assert_refused(digest, f"paper-state's tip on GitHub, {FENCE}, still has data/{ctl.DB_NAME}, so it "
                                    f"isn't the fence", before_anything=True)
        self.assert_refused(digest, step, before_anything=True)
        (tip / ctl.DB_NAME).unlink()
        (tip / NOTE).unlink()
        self.assert_refused(digest, f"paper-state's tip on GitHub, {FENCE}, has no data/{NOTE}, so it isn't the "
                                    f"fence", before_anything=True)
        self.assert_refused(digest, step, before_anything=True)

    def test_the_seed_refuses_unless_the_tag_render_seed_marks_sha(self):
        digest, refs, other = self.paper_state(), self.remote.refs, "1234567" * 5 + "89abc"
        step = "(RUNBOOK 13.3 step 5). Nothing was done."
        self.remote.refs = {name: commit for name, commit in refs.items() if name != "refs/tags/render-seed"}
        self.assert_refused(digest, "GitHub has no tag render-seed", before_anything=True)
        self.assert_refused(digest, step, before_anything=True)
        self.remote.refs = {**refs, "refs/tags/render-seed": other}
        self.assert_refused(digest, f"the tag render-seed marks {other}, not {SHA}", before_anything=True)
        self.assert_refused(digest, f"git tag -f render-seed {SHA}, then git push -f origin refs/tags/render-seed",
                            before_anything=True)
        self.assert_refused(digest, step, before_anything=True)
        # An annotated tag's own line names the tag object; the line after it, with ^{}, names the commit.
        self.remote.refs = {**refs, "refs/tags/render-seed": SHA, "refs/tags/render-seed^{}": other}
        self.assert_refused(digest, f"the tag render-seed marks {other}, not {SHA}", before_anything=True)

    def test_an_annotated_render_seed_is_read_through_to_the_commit_it_marks(self):
        digest, tag = self.paper_state(), "7a6" * 13 + "0"
        self.remote.refs = {**self.remote.refs, "refs/tags/render-seed": tag, "refs/tags/render-seed^{}": SHA}
        code, out = self.shell("seed", SHA, "--sha256", digest)
        self.assertEqual(code, 0, out)
        self.assertIn(f"the tag render-seed marks {SHA[:7]}", out)
        self.assertTrue((self.data / ctl.MARKER).exists())

    def test_githubs_ref_advertisement_is_read_as_git_ls_remote_reads_it(self):
        self.assertEqual(ctl.REFS, REFS)
        refs = {"HEAD": MAIN, "refs/heads/main": MAIN, "refs/heads/paper-state": FENCE,
                "refs/tags/render-seed": "7a6" * 13 + "0", "refs/tags/render-seed^{}": SHA}
        self.assertEqual(ctl.advertised_refs(advertisement(refs)), refs)
        self.assertEqual(ctl.advertised_refs(pkt_lines(b"# service=git-upload-pack\n", b"", b"")), {})
        for body in (b"", b"0000", advertisement(refs)[:-1], advertisement(refs).replace(b"0044", b"00zz"),
                     pkt_lines(b"# service=git-upload-pack\n", b"", b"not a ref\n", b""),
                     pkt_lines(b"# service=git-upload-pack\n", b"", f"{SHA[:39]} refs/heads/main\n".encode(), b""),
                     b"0003" + advertisement(refs)):
            with self.subTest(body=body[:40]):
                with self.assertRaises(ValueError):
                    ctl.advertised_refs(body)


class Commands(Shell):
    def test_every_command_but_check_refuses_a_folder_that_isnt_the_disk(self):
        digest = self.paper_state()
        commands = (["status"], ["probe"], ["resume"], ["halt"], ["flatten", "--yes"], ["hold"], ["unhold"],
                    ["backup"], ["events"], ["seed", SHA, "--sha256", digest])
        for case, options in (("not a mount", {"is_mount": lambda path: False}),
                              ("relative", {"environ": {"DUO_DATA_DIR": "disk"}})):
            for argv in commands:
                with self.subTest(case=case, command=argv[0]):
                    code, out = self.shell(*argv, **options)
                    self.assertEqual(code, 2, out)
                    self.assertIn("nothing was done", out)
        self.assertEqual(disk(self.data), {})
        self.assertEqual((self.ran, self.remote.urls, self.dashboard.requests, self.supervisor.requests),
                         ([], [], [], []))
        code, out = self.shell("check", is_mount=lambda path: False)
        self.assertEqual(code, 1, out)
        self.assertIn(f"no: {self.data} isn't a mount point", out)

    def test_check_changes_nothing_and_says_what_this_shell_sees(self):
        self.seeded()
        (self.data / ctl.MARKER).write_text(json.dumps({"schema": 1, "sha": SHA}))
        (self.data / ctl.HOLD).write_text("Held.\n")
        before = disk(self.data)
        code, out = self.shell("check")
        self.assertEqual(code, 0, out)
        self.assertEqual(disk(self.data), before)
        for text in (os.getcwd(), sys.executable, ctl.venv_python(self.environ), str(self.data), "Mount point", "yes",
                     "Write test", "ok: made a file there and deleted it", " MB of ",
                     f"{self.supervisor.url}/supervisor.json: held: HOLD is there",
                     f"{self.dashboard.url} answers (HTTP 200)", SHA, "there, since"):
            self.assertIn(text, out)
        self.assertEqual(self.dashboard.requests[0][:2], ("GET", "/"))  # Not /api/state, which writes.

    def test_check_says_which_run_config_toml_asks_for_and_when_the_systematic_runner_is_off(self):
        code, out = self.shell("check")
        self.assertEqual(code, 0, out)
        self.assertIn(f"the systematic runner: {STRATEGY} in USD", out)
        self.config.write_text(self.config.read_text().replace("enabled = true", "enabled = false"))
        code, out = self.shell("check")
        self.assertEqual(code, 1, out)
        self.assertIn("Not ready: config.toml has the systematic runner off", out)
        self.assertIn("RUNBOOK 13.10", out)
        self.assertEqual(disk(self.data), {})

    def test_check_says_when_neither_the_supervisor_nor_the_bot_answers(self):
        nowhere = nobody()
        code, out = self.shell("check", supervisor=nowhere, dashboard=nobody())
        self.assertEqual(code, 1, out)
        self.assertIn("the supervisor doesn't answer", out)
        self.assertIn("the bot isn't running here", out)
        self.assertIn("none: not seeded yet", out)
        self.assertIn(f"Not ready: this Shell can't reach the supervisor at {nowhere}/supervisor.json (URLError: ", out)
        self.assertIn("), so hold, unhold and backup can't confirm what they did, and halt and flatten can't reach the "
                      "bot: don't seed (RUNBOOK 13.2).", out)

    def test_check_needs_the_supervisor_and_its_data_folder_from_this_shell(self):
        url, closing = f"{self.supervisor.url}/supervisor.json", ("The disk is mounted and can be written to, so the "
                                                                  "other commands can run (RUNBOOK 13).\n")
        for name, answer, why in (("schema 2", {"schema": 2, "state": "running", "data_dir": {"path": str(self.data)}},
                                   "its answer has schema 2, not 1"),
                                  ("a list", [{"schema": 1}], "its answer isn't a JSON object")):
            with self.subTest(name):
                self.pinned = [answer]
                code, out = self.shell("check")
                self.assertEqual(code, 1, out)
                self.assertIn(f"Not ready: this Shell can't reach the supervisor at {url} ({why}), so hold, unhold and "
                              f"backup can't confirm what they did, and halt and flatten can't reach the bot: don't "
                              f"seed (RUNBOOK 13.2).", out)
                self.assertNotIn(closing, out)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.pinned = [{"schema": 1, "state": "waiting", "data_dir": {"path": str(elsewhere)}}]
        code, out = self.shell("check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"Not ready: the supervisor's data folder is {elsewhere}, but this Shell's is {self.data}: set "
                      f"DUO_DATA_DIR={elsewhere} in the Shell", out)
        self.assertIn("(RUNBOOK 13.2).", out)
        for name, given in (("null", {"data_dir": {"path": None}}), ("no path", {"data_dir": {}}), ("no data_dir", {})):
            with self.subTest(name):
                self.pinned = [{"schema": 1, "state": "waiting", **given}]
                code, out = self.shell("check")
                self.assertEqual(code, 1, out)
                self.assertIn(f"Not ready: the supervisor's data folder is not given (", out)
                self.assertIn(f"), but this Shell's is {self.data}:", out)
        # The same folder by another path is the same folder.
        (self.tmp / "link").symlink_to(self.data)
        self.pinned = [{"schema": 1, "state": "waiting", "data_dir": {"path": str(self.tmp / "link")}}]
        code, out = self.shell("check")
        self.assertEqual(code, 0, out)
        self.pinned = []
        code, out = self.shell("check")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.endswith(closing), out)

    def test_check_says_whether_github_answers_and_needs_it_until_the_seed(self):
        code, out = self.shell("check")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, rf"\nGitHub +answers from here \({re.escape(REFS)}, "
                              rf"{re.escape(ctl.RAW)}/main/\.python-version\)\n")
        self.assertEqual(self.remote.urls, [REFS, f"{ctl.RAW}/main/.python-version"])
        (self.tmp / "raw" / "main" / ".python-version").unlink()  # A 404 is GitHub answering.
        self.assertEqual(self.shell("check")[0], 0)

        def limited(url):
            if url == REFS:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b""))
            return self.remote(url)

        cut = f"{REFS}: URLError: <urlopen error connection reset by peer>"
        for name, fetch, problem in (("HTTP 429", limited, f"{REFS}: HTTP 429"), ("no connection", self.remote, cut)):
            with self.subTest(name):
                self.remote.failures[REFS] = 1
                code, out = self.shell("check", fetch=fetch)
                self.assertEqual(code, 1, out)
                self.assertIn(f"GitHub", out)
                self.assertIn(f"doesn't answer from here: {problem}\n", out)
                self.assertIn(f"Not ready: Render can't download from GitHub now ({problem}), and the seed needs it "
                              f"(RUNBOOK 13.2).", out)
                self.assertEqual(self.slept, [])  # One try each, with no pause.
                self.remote.failures.clear()
        # Once seeded, the seed doesn't need GitHub: the row still says so, and check is ready.
        self.seeded()
        code, out = self.shell("check", fetch=limited)
        self.assertEqual(code, 0, out)
        self.assertIn(f"doesn't answer from here: {REFS}: HTTP 429\n", out)
        self.assertNotIn("Not ready", out)

    def test_status_probe_and_resume_run_duo_with_the_services_python_on_the_disk(self):
        venv = self.tmp / "render-venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
        environ = {"DUO_DATA_DIR": str(self.data), "VENV_ROOT": str(venv), "PATH": "/usr/bin:/bin", "TZ": "UTC"}
        for command in ("status", "probe", "resume"):
            with self.subTest(command):
                self.ran.clear()
                code, _ = self.shell(command, environ=environ)
                self.assertEqual(code, 0)
                (args, cwd, env), = self.ran
                self.assertEqual(args, [str(venv / "bin" / "python"), "-m", "duo", command])
                self.assertEqual(cwd, ctl.ROOT)
                self.assertEqual(env, environ)
        # Without DUO_DATA_DIR in the Shell, duo is still told the disk's path, never left to fall back to data/.
        self.ran.clear()
        self.shell("status", environ={"PATH": "/usr/bin:/bin"}, is_mount=lambda path: path == ctl.DATA_DIR)
        self.assertEqual(self.ran[0][2]["DUO_DATA_DIR"], "/var/data")
        self.assertEqual(self.shell("resume", run=lambda command, cwd, env: 3)[0], 3)

    def test_the_services_python_is_venv_root_then_the_repositorys_venv_then_this_one(self):
        repo, venv = self.tmp / "repo", self.tmp / "render-venv"
        repo.mkdir()
        self.assertEqual(ctl.venv_python({"VENV_ROOT": str(venv)}, repo), sys.executable)
        (repo / ".venv" / "bin").mkdir(parents=True)
        (repo / ".venv" / "bin" / "python").write_text("")
        self.assertEqual(ctl.venv_python({"VENV_ROOT": str(venv)}, repo), str(repo / ".venv" / "bin" / "python"))
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
        self.assertEqual(ctl.venv_python({"VENV_ROOT": str(venv)}, repo), str(venv / "bin" / "python"))
        self.shell("status", root=repo, environ={"DUO_DATA_DIR": str(self.data)})
        (args, cwd, _), = self.ran
        self.assertEqual((args[0], cwd), (str(repo / ".venv" / "bin" / "python"), repo))

    def test_the_bot_and_the_supervisor_are_reached_on_loopback(self):
        defaults = ctl.Controls({})
        self.assertEqual((defaults.dashboard, defaults.supervisor), ("http://127.0.0.1:8787", "http://127.0.0.1:10000"))
        self.assertEqual((defaults.data, defaults.root, defaults.config),
                         (Path("/var/data"), ROOT, ROOT / "config.toml"))
        self.assertEqual(ctl.Controls({"PORT": "10123"}).supervisor, "http://127.0.0.1:10123")

    def test_halt_posts_to_the_bots_dashboard_with_the_header_its_actions_need(self):
        code, out = self.shell("halt")
        self.assertEqual(code, 0, out)
        (method, path, headers), = self.dashboard.requests
        self.assertEqual((method, path, headers.get("x-duo-action")), ("POST", "/api/halt", "1"))
        self.assertNotIn("authorization", headers)
        self.assertIn("Halted", out)

    def test_with_a_dashboard_token_in_the_shell_the_posts_carry_it(self):
        token = "pass" + "word"
        code, out = self.shell("halt", environ={"DUO_DATA_DIR": str(self.data), "DUO_DASHBOARD_TOKEN": token})
        self.assertEqual(code, 0, out)
        (_, _, headers), = self.dashboard.requests
        self.assertEqual(headers.get("authorization"), "Basic " + base64.b64encode(f"duo:{token}".encode()).decode())

    def test_flatten_posts_only_once_flatten_is_typed_or_yes_is_given(self):
        for typed in (["flatten"], ["yes"], ["FLATTEN now"], []):
            with self.subTest(typed=typed):
                self.typed = list(typed)
                code, out = self.shell("flatten")
                self.assertEqual(code, 1, out)
                self.assertIn("Nothing was sent.", out)
        self.assertEqual(self.dashboard.requests, [])
        self.typed = [" FLATTEN "]
        self.assertEqual(self.shell("flatten")[0], 0)

        def never(prompt):
            raise AssertionError("--yes asked anyway")

        code, out = self.shell("flatten", "--yes", ask=never)
        self.assertEqual(code, 0, out)
        self.assertIn("sells everything at its next loop", out)
        self.assertEqual([(method, path, headers.get("x-duo-action")) for method, path, headers in
                          self.dashboard.requests], [("POST", "/api/flatten", "1")] * 2)

    def test_halt_and_flatten_say_so_when_no_bot_answers_or_it_refuses(self):
        refusing = StandIn(lambda method, path: (401, {"error": "password"}))
        self.addCleanup(refusing.close)
        for argv in (["halt"], ["flatten", "--yes"]):
            with self.subTest(argv[0]):
                code, out = self.shell(*argv, dashboard=nobody())
                self.assertEqual(code, 1, out)
                self.assertIn("so the bot isn't running in this instance and nothing was sent", out)
                code, out = self.shell(*argv, dashboard=refusing.url)
                self.assertEqual(code, 1, out)
                self.assertIn("(HTTP 401), so nothing was done. It wants a password", out)

    def test_hold_makes_hold_and_waits_until_the_supervisor_says_held(self):
        code, out = self.shell("hold")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.data / ctl.HOLD).exists())
        self.assertIn("Held: the bot is stopped", out)
        before = disk(self.data)
        code, out = self.shell("hold")
        self.assertEqual(code, 0, out)
        self.assertIn("HOLD was already there", out)
        self.assertIn(". It says: Made by python3 ops/render/ctl.py hold, ", out)  # Who made it.
        self.assertEqual(disk(self.data), before)  # Left as it was, so the page's "held since" doesn't move.

    def test_hold_gives_up_after_four_minutes_and_leaves_hold_in_place(self):
        self.pinned = [{"schema": 1, "state": "stopping", "reason": "waiting for the bot to exit"}]
        code, out = self.shell("hold")
        self.assertEqual(code, 1, out)
        self.assertIn("after 4 minutes the bot isn't held: stopping: waiting for the bot to exit", out)
        self.assertTrue((self.data / ctl.HOLD).exists())
        self.assertEqual(sum(self.slept), ctl.HOLD_SECONDS)
        self.slept.clear()
        code, out = self.shell("hold", supervisor=nobody())
        self.assertEqual(code, 1, out)
        self.assertIn("the bot isn't held: the supervisor doesn't answer", out)
        self.assertEqual(sum(self.slept), ctl.HOLD_SECONDS)

    def test_unhold_removes_hold_and_waits_for_the_loops_first_check(self):
        self.seeded()
        (self.data / ctl.HOLD).write_text("Held.\n")
        code, out = self.shell("unhold")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.data / ctl.HOLD).exists())
        self.assertIn(f"Running: the loop checked at {utc(NOW)}. The page shows it within 5 minutes.", out)

    def test_unhold_gives_the_supervisors_reason_when_the_bot_doesnt_start(self):
        (self.data / ctl.HOLD).write_text("Held.\n")
        code, out = self.shell("unhold")
        self.assertEqual(code, 1, out)
        self.assertFalse((self.data / ctl.HOLD).exists())
        self.assertIn("Not running after 4 minutes: waiting: no paper_systematic.db on the disk", out)
        self.assertIn("(RUNBOOK 13.6)", out)
        self.assertEqual(sum(self.slept), ctl.UNHOLD_SECONDS)

    def test_unhold_says_when_the_bot_exits_at_once(self):
        self.seeded()
        (self.data / ctl.HOLD).write_text("Held.\n")
        held = {"schema": 1, "state": "held", "reason": "HOLD is there",
                "child": {"pid": None, "started_at": None, "restarts": 0, "last_exit": None, "tail": []}}
        self.pinned = [held, {**held, "state": "running", "reason": ""}, exited()]
        code, out = self.shell("unhold")
        self.assertEqual(code, 1, out)
        self.assertIn("Not running: the bot exited at once: backoff: the bot exited with code 1; it's started again in "
                      "30 s (RUNBOOK 13.6). It exited with code 1, and its last lines were:\n    Duo paper run, "
                      "starting.\n    Not starting the systematic runner: the start record is for another run\n", out)
        self.assertEqual(self.slept, [ctl.POLL_SECONDS] * 2)

    def test_unhold_with_no_hold_and_the_bot_running_says_so_at_once(self):
        self.seeded()
        self.started = NOW - 600  # The bot has run for 10 minutes.
        code, out = self.shell("unhold")
        self.assertEqual(code, 0, out)
        self.assertEqual(out, "There was no HOLD, and the bot is running.\n")
        self.assertEqual((self.slept, len(self.supervisor.requests)), ([], 1))
        # With no HOLD and no bot running, it waits as for any unhold.
        self.started = None
        (self.data / ctl.DB_NAME).unlink()
        code, out = self.shell("unhold")
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith("There was no HOLD.\n"), out)
        self.assertIn("Not running after 4 minutes: waiting: no paper_systematic.db on the disk", out)

    def test_backup_asks_the_supervisor_for_one_and_prints_its_manifest(self):
        self.seeded()
        code, out = self.shell("backup")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.data / ctl.BACKUP_NOW).exists())
        stamp = datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.assertIn(f"Backed up: {self.data / 'backups' / stamp / 'manifest.json'}", out)
        self.assertIn('"integrity": "ok"', out)

    def test_backup_gives_up_after_three_minutes_and_says_why(self):
        self.seeded()
        last = {"at": T0, "stamp": "20261105T135345Z", "sha256": "ab" * 32, "error": None}
        self.pinned = [{"schema": 1, "state": "running", "reason": "", "backup": last}]
        code, out = self.shell("backup")
        self.assertEqual(code, 1, out)
        self.assertIn("No new backup after 3 minutes", out)
        self.assertIn("BACKUP_NOW is still there, so the supervisor takes one when it can", out)
        self.assertEqual(sum(self.slept), ctl.BACKUP_SECONDS)
        # A failed backup is told as soon as the supervisor records it.
        self.slept.clear()
        self.pinned = [{"state": "running", "backup": last},
                       {"state": "running", "backup": {**last, "error": "integrity_check: disk I/O error"}}]
        code, out = self.shell("backup")
        self.assertEqual(code, 1, out)
        self.assertIn("The backup failed: integrity_check: disk I/O error", out)
        self.assertEqual(self.slept, [])
        # An error from an earlier backup that the supervisor still shows isn't this one's.
        stamp = "20261107T000500Z"
        (self.data / "backups" / stamp).mkdir(parents=True)
        (self.data / "backups" / stamp / "manifest.json").write_text(json.dumps({"schema": 1, "integrity": "ok"}))
        failed = {**last, "error": "integrity_check: disk I/O error"}
        self.pinned = [{"state": "running", "backup": failed},
                       {"state": "running", "backup": {**failed, "at": NOW, "stamp": stamp}}]
        code, out = self.shell("backup")
        self.assertEqual(code, 0, out)
        self.assertIn(f"Backed up: {self.data / 'backups' / stamp / 'manifest.json'}", out)

    def events_db(self, *events) -> Path:
        """The run's database with duo's own events table holding events, (ts, level, kind, text) each. Its journal
        mode is DELETE, so it opens read-only on its own whichever SQLite runs the tests."""
        connection = sqlite3.connect(self.data / ctl.DB_NAME)
        connection.executescript(DB_SCHEMA)
        connection.executemany("INSERT INTO events(ts, level, kind, text) VALUES(?, ?, ?, ?)", events)
        connection.commit()
        connection.close()
        return self.data / ctl.DB_NAME

    def test_events_prints_the_newest_events_oldest_first_and_only_reads(self):
        failed = ("The daily decision failed and will be retried at the next loop:\n"
                  "Traceback (most recent call last):\n"
                  "  File \"duo/systematic.py\", line 466, in loop\n"
                  "ValueError: no bar for 2026-11-05")
        database = self.events_db((NOW - 900, "info", "systematic", "Started the paper run."),
                                  (NOW - 600, "warn", "systematic", "No loop ran for 12 minutes."),
                                  (NOW - 300, "critical", "systematic", failed))
        before, opened = disk(self.data), []

        def recording(path, *args, **kwargs):
            opened.append((path, args, kwargs))
            return sqlite3.connect(path, *args, **kwargs)

        code, out = self.shell("events", "--limit", "2", connect=recording)
        self.assertEqual(code, 0, out)
        self.assertEqual(out, f"{utc(NOW - 600)}  warn  systematic  No loop ran for 12 minutes.\n"
                              f"{utc(NOW - 300)}  critical  systematic  The daily decision failed and will be retried "
                              f"at the next loop:\n"
                              f"    Traceback (most recent call last):\n"
                              f"      File \"duo/systematic.py\", line 466, in loop\n"
                              f"    ValueError: no bar for 2026-11-05\n")
        # A plain read-only open, as the supervisor's: never immutable, which would miss what the -wal holds.
        self.assertEqual(opened, [(f"{database.resolve().as_uri()}?mode=ro", (), {"uri": True, "timeout": 10})])
        self.assertEqual(disk(self.data), before)
        code, out = self.shell("events")  # The newest 20 by default.
        self.assertEqual((code, len(out.splitlines())), (0, 6), out)
        self.assertTrue(out.startswith(f"{utc(NOW - 900)}  info  systematic  Started the paper run.\n"), out)

    def test_events_shows_a_time_that_isnt_one_as_stored_and_says_when_there_are_none(self):
        self.events_db(("soon", "warn", "systematic", "A time that isn't one."))
        code, out = self.shell("events")
        self.assertEqual((code, out), (0, "soon  warn  systematic  A time that isn't one.\n"))
        (self.data / ctl.DB_NAME).unlink()
        self.events_db()
        code, out = self.shell("events")
        self.assertEqual(code, 0, out)
        self.assertIn("has no events yet", out)
        (self.data / ctl.DB_NAME).unlink()
        sqlite3.connect(self.data / ctl.DB_NAME).close()  # No events table at all.
        code, out = self.shell("events")
        self.assertEqual(code, 0, out)
        self.assertIn("has no events yet", out)

    def test_events_with_no_database_or_a_limit_under_one(self):
        code, out = self.shell("events")
        self.assertEqual(code, 1, out)
        self.assertEqual(out, f"No paper run on the disk yet: {self.data / ctl.DB_NAME} isn't there (RUNBOOK 13.3).\n")
        self.assertEqual(disk(self.data), {})
        for limit in ("0", "-3", "many"):
            with self.subTest(limit=limit), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as refused:
                    self.shell("events", "--limit", limit)
                self.assertEqual(refused.exception.code, 2)

    def test_backup_asked_for_already_waits_for_that_one(self):
        self.seeded()
        (self.data / ctl.BACKUP_NOW).write_text("Made by python3 ops/render/ctl.py backup, a minute ago.\n")
        last, stamp = {"at": T0, "stamp": "20261105T135345Z", "sha256": "ab" * 32, "error": None}, "20261107T000500Z"
        (self.data / "backups" / stamp).mkdir(parents=True)
        (self.data / "backups" / stamp / "manifest.json").write_text(json.dumps({"schema": 1, "taken_at": NOW + 5}))
        taken = {"schema": 1, "state": "running", "backup": {**last, "at": NOW + 5, "stamp": stamp}}
        self.pinned = [{"schema": 1, "state": "running", "backup": last}] * 2 + [taken]
        code, out = self.shell("backup")
        self.assertEqual(code, 0, out)
        self.assertIn("A backup was already asked for: BACKUP_NOW is there.", out)
        self.assertIn(f"Backed up: {self.data / 'backups' / stamp / 'manifest.json'}\n", out)
        self.assertIn(f'"taken_at": {NOW + 5}', out)
        self.assertEqual(self.slept, [ctl.POLL_SECONDS])

    def test_backup_refuses_before_the_seed(self):
        code, out = self.shell("backup")
        self.assertEqual(code, 1, out)
        self.assertIn("Nothing to back up yet", out)
        self.assertEqual((disk(self.data), self.supervisor.requests), ({}, []))


class SelfTest(Shell):
    """The SQLite self-test that check and the seed run: a WAL database with no -wal or -shm, as the seed places the
    run's, opened read-only as the supervisor opens it. Apple's SQLite refuses that open and upstream's makes it, so
    each outcome here comes from a stand-in for sqlite3.connect, and the real SQLite is only compared with itself."""

    REFUSED = "(OperationalError: unable to open database file)"

    def test_check_tries_it_on_the_disk_with_no_wal_or_shm_beside_it_and_leaves_nothing(self):
        folder, seen = self.data / f".selftest-{os.getpid()}", []

        class Keeping(sqlite3.Connection):
            def close(self):  # As Apple's SQLite does, whichever runs the tests: the -wal and -shm stay.
                super().close()
                for suffix in ("-wal", "-shm"):
                    (folder / f"selftest.db{suffix}").touch()

        def keeping(database, *args, **kwargs):
            if str(database).endswith("?mode=ro"):
                seen.append((database, sorted(os.listdir(folder)), (folder / "selftest.db").read_bytes()[18:20]))
                return accepting(database, *args, **kwargs)
            return sqlite3.connect(database, *args, factory=Keeping, **kwargs)

        code, out = self.shell("check", connect=keeping)
        self.assertEqual(code, 0, out)
        database = (folder / "selftest.db").resolve().as_uri()
        self.assertEqual(seen, [(f"{database}?mode=ro", ["selftest.db"], b"\x02\x02")],
                         "one plain read-only open, as the supervisor's, of a WAL database alone in its folder")
        self.assertIn(f"SQLite self-test  ok: SQLite {sqlite3.sqlite_version} ({sys.executable}) opened a WAL database "
                      f"with no -wal or -shm read-only, as the supervisor opens the run's\n", out)
        self.assertEqual(disk(self.data), {})

    def test_a_sqlite_that_refuses_that_open_stops_check_and_the_seed_before_anything_is_done(self):
        code, out = self.shell("check", connect=refusing)
        self.assertEqual(code, 1, out)
        self.assertIn(f"SQLite self-test  failed: SQLite {sqlite3.sqlite_version} ({sys.executable}) couldn't open a "
                      f"WAL database with no -wal or -shm read-only, as the supervisor opens the run's {self.REFUSED}",
                      out)
        self.assertIn("Not ready: the SQLite self-test failed, so the seed must not go ahead: the supervisor opens the "
                      "run's database the same way, and would never start the bot (RUNBOOK 13.2).", out)
        digest = self.paper_state()
        self.supervisor.requests.clear()
        self.remote.urls.clear()  # check asked GitHub whether it answers.
        code, out = self.shell("seed", SHA, "--sha256", digest, connect=refusing)
        self.assertEqual(code, 1, out)
        self.assertIn(f"Not seeded: the SQLite self-test failed: SQLite {sqlite3.sqlite_version}", out)
        self.assertIn(f"{self.REFUSED}. The supervisor opens the run's database the same way, so it would never start "
                      f"the bot: the seed mustn't go ahead (RUNBOOK 13.2). Nothing was done.", out)
        self.assertEqual((disk(self.data), self.remote.urls, self.supervisor.requests), ({}, [], []),
                         "no HOLD, no download, and nothing left of the test")

    def test_without_a_mount_it_can_write_to_it_tries_a_temporary_folder_and_says_so(self):
        system = self.tmp / "system-temp"
        system.mkdir()
        with mock.patch.object(ctl.tempfile, "tempdir", str(system)):
            code, out = self.shell("check", is_mount=lambda path: False)
            self.assertEqual(code, 1, out)  # Not the disk, which check says too.
            self.assertIn(f"read-only, as the supervisor opens the run's; tried in a temporary folder, as {self.data} "
                          f"isn't a mount that can be written to", out)
            code, out = self.shell("selftest", is_mount=lambda path: False)
        self.assertEqual(code, 0, out)  # The command alone never refuses: it's what check and the seed run elsewhere.
        self.assertRegex(out, rf"\Aok: SQLite {re.escape(sqlite3.sqlite_version)} .*tried in a temporary folder.*\n\Z")
        self.assertEqual((list(system.iterdir()), disk(self.data)), ([], {}), "and it removed what it made")

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_it_runs_in_the_services_python_when_that_isnt_this_one(self):
        # The supervisor's SQLite is the service's Python's (VENV_PY), and the Shell's python3 may be another build.
        venv, calls = self.tmp / "render-venv", self.tmp / "calls.txt"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text('#!/bin/bash\n'
                                             'if [ "$1" = --version ]; then echo "Python 3.13.9"; exit 0; fi\n'
                                             f'echo "$PWD|$DUO_DATA_DIR|$*" >> "{calls}"\n'
                                             'echo "$SAYS"\n'
                                             'exit "$EXIT"\n')
        (venv / "bin" / "python").chmod(0o755)
        environ = {"DUO_DATA_DIR": str(self.data), "VENV_ROOT": str(venv)}
        said = "SQLite 3.46.1 (the service's Python) opened a WAL database with no -wal or -shm read-only"
        code, out = self.shell("check", environ={**environ, "SAYS": f"ok: {said}", "EXIT": "0"}, connect=refusing)
        self.assertEqual(code, 0, out)
        self.assertIn(f"SQLite self-test  ok: {said}\n", out)
        self.assertEqual(calls.read_text().splitlines(),
                         [f"{ctl.ROOT}|{self.data}|-I {Path(ctl.__file__).resolve()} selftest"])
        said = f"SQLite 3.46.1 (the service's Python) couldn't open a WAL database ... {self.REFUSED}"
        failing = {**environ, "SAYS": f"failed: {said}", "EXIT": "1"}
        code, out = self.shell("check", environ=failing, connect=accepting)
        self.assertEqual(code, 1, out)
        self.assertIn(f"SQLite self-test  failed: {said}\n", out)
        self.remote.urls.clear()  # check asked GitHub whether it answers.
        code, out = self.shell("seed", SHA, "--sha256", self.paper_state(), environ=failing, connect=accepting)
        self.assertEqual(code, 1, out)
        self.assertIn(f"Not seeded: the SQLite self-test failed: {said}.", out)
        self.assertEqual((disk(self.data), self.remote.urls), ({}, []))

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_run_for_real_in_another_python_it_says_one_line_with_that_sqlite(self):
        venv = self.tmp / "render-venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
        (venv / "bin" / "python").chmod(0o755)
        # Not a mount to that Python, so it tries a temporary folder: this one's.
        system = self.tmp / "system-temp"
        system.mkdir()
        environ = {"DUO_DATA_DIR": str(self.data), "VENV_ROOT": str(venv), "TMPDIR": str(system)}
        passed, said = self.controls(environ=environ).selftest()
        self.assertRegex(said, rf"\ASQLite [\d.]+ \({re.escape(sys.executable)}\) "
                               rf"{'opened' if passed else 'couldn.t open'} a WAL database with no -wal or -shm .*"
                               rf"tried in a temporary folder")
        self.assertEqual((list(system.iterdir()), disk(self.data)), ([], {}))

    def test_what_it_finds_is_what_this_sqlite_does_to_the_run_s_own_database_placed_as_the_seed_places_it(self):
        passed, said = self.controls(connect=sqlite3.connect).selftest_here()
        self.assertEqual(passed, lone_wal_opens(Path(tempfile.mkdtemp(dir=self.tmp))), said)
        self.assertIn(f"SQLite {sqlite3.sqlite_version} ({sys.executable})", said)
        self.assertEqual(disk(self.data), {})


if __name__ == "__main__":
    unittest.main()
