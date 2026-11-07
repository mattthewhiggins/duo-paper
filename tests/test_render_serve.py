"""The supervisor Render runs, ops/render/serve.py: the gate before the bot, the bot's lifecycle, the page, /ok,
/supervisor.json, the files it serves, and backups.

It runs as it is, with stand-ins only where it reaches outside its process. Whether the data folder is a mount, and
whether anything listens on the bot's port, are flags here. Its four commands run fake_duo.py, written to a temporary
folder: it stands in for python -m duo run (a bot that beats, keeps quiet, exits or ignores SIGTERM, as a file beside it
says), python -m duo.site, python -m duo watchdog and python -m duo probe, and notes each thing it does. Databases are
made with the runner's own schema. Timings are shortened, and a clock that can be moved on stands in where minutes or
hours would pass. Shutting down is tested in a process of its own, sent a real SIGTERM with a slow build under way.

No network: every server is on 127.0.0.1.
"""
import calendar
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
import platform
import re
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest import mock

from duo import config as config_module
from duo.db import DB
from duo.systematic import SCHEMA as SYSTEMATIC_SCHEMA, prepare

from .sys_helpers import FakeSysMarket, make_sys_config

ROOT = Path(__file__).resolve().parent.parent
SERVE = ROOT / "ops" / "render" / "serve.py"

_spec = importlib.util.spec_from_file_location("render_serve", SERVE)
serve = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = serve  # Its dataclasses look their module up there.
_spec.loader.exec_module(serve)

STRATEGY = "F1__sma_50__BTC__vol20"
CONFIG = f'mode = "paper"\n\n[portfolio]\nquote = "USD"\n\n[systematic]\nenabled = true\nstrategy = "{STRATEGY}"\n'
COMMIT = "5f2b" * 10
DATA = object()  # supervisor()'s default: DUO_DATA_DIR is the data folder.
# Shortened, so each test takes seconds. Minutes and hours pass on the stand-in clock instead.
FAST = serve.Timings(tick=0.02, bind_retry=0.1, lock_retry=0.1, hold_grace=3, wedge_grace=2, shutdown_grace=3,
                     debounce=0.1, build_timeout=20, watchdog_timeout=20, probe_timeout=20)

FAKE_DUO = r'''"""Stands in for python -m duo run, duo.site, duo watchdog and duo probe. FAKE_OUT/ROLE.mode says how,
and FAKE_OUT/calls.jsonl gets a line for each thing it does."""
import json
import os
import signal
import sys
import time

out, role = os.environ["FAKE_OUT"], sys.argv[1]
try:
    with open(os.path.join(out, role + ".mode")) as chosen:
        how = chosen.read().strip()
except OSError:
    how = ""


def note(event, **fields):
    with open(os.path.join(out, "calls.jsonl"), "a") as calls:
        calls.write(json.dumps({"role": role, "event": event, "pid": os.getpid(), **fields}) + "\n")


if role == "run":  # beat (the default), silent, stubborn, or "exit N"
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: (note("term"), how == "stubborn" or stopping.append(True)))
    names = ("DUO_DATA_DIR", "PYTHONUNBUFFERED", "TZ", "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS")
    note("start", argv=sys.argv[1:], cwd=os.getcwd(), pgid=os.getpgid(0), env={n: os.environ.get(n) for n in names})
    print("fake bot: started", flush=True)
    if how.startswith("exit"):
        print("fake bot: refusing to start", flush=True)
        sys.exit(int(how.split()[1]))
    heartbeat = os.path.join(os.environ["DUO_DATA_DIR"], "heartbeat_systematic.json")
    while not stopping:
        if how in ("", "beat"):
            with open(heartbeat + ".part", "w") as beat:
                json.dump({"ts": time.time(), "status": "ok"}, beat)
            os.replace(heartbeat + ".part", heartbeat)
        if os.path.exists(os.path.join(out, "crash")):
            os.remove(os.path.join(out, "crash"))
            print("fake bot: crashed", flush=True)
            sys.exit(1)
        time.sleep(0.02)
    print("Stopped.", flush=True)
    note("stop")
elif role == "site":  # ok (the default), fail, partial (no status.json) or slow
    folder = sys.argv[sys.argv.index("--out") + 1]
    note("build", argv=sys.argv[1:], how=how)
    if how == "slow":
        time.sleep(60)
    if how == "fail":
        print("fake site: the database couldn't be read", flush=True)
        sys.exit(1)
    files = {"index.html": f"<p>build {os.getpid()}</p>", "compare.md": "# Compare\n", "decisions.jsonl": "{}\n",
             ".status.json-half": "{", "status.json": json.dumps({"generated_at": time.time()})}
    for name, text in files.items():
        if not (how == "partial" and name == "status.json"):
            with open(os.path.join(folder, name), "w") as part:
                part.write(text)
    print(f"Status page written to {folder}.", flush=True)
elif role == "watchdog":  # ok (the default) or fail
    note("watchdog")
    if how == "fail":
        print("The bot is down: its last heartbeat was 21 minutes ago (2026-11-07 11:13:20 UTC).")
        print("Power: not checked, as this machine isn't a Mac.")
        sys.exit(1)
    print("Bot alive: heartbeat 3s ago, status ok.")
    print("Power: not checked, as this machine isn't a Mac.")
elif role == "probe":  # exits with its mode, 1 by default
    note("probe")
    for number in range(25):
        print(f"probe line {number}")
    sys.exit(int(how or 1))
'''

WRITER = r'''import os
import sqlite3
import sys
import time

database, stop = sys.argv[1:]
connection = sqlite3.connect(database, timeout=30, isolation_level=None)
connection.execute("PRAGMA journal_mode=WAL")
writes = 0
while not os.path.exists(stop):
    writes += 1
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO sys_heartbeats(ts, status, halts, controls) VALUES(?, 'ok', '[]', ?)",
                       (1794050000.0 + writes, "x" * 1000))
    connection.execute("INSERT INTO state(key, value) VALUES('writes', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(writes),))
    connection.execute("COMMIT")
    if writes % 40 == 0:
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
    time.sleep(0.001)
print(writes, flush=True)
'''

LAUNCHER = r'''import importlib.util
import sys
from pathlib import Path

serve_path, data, root, site, script, grace = sys.argv[1:]
spec = importlib.util.spec_from_file_location("render_serve", serve_path)
serve = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = serve
spec.loader.exec_module(serve)
python = sys.executable
supervisor = serve.Supervisor(
    data, root=Path(root), site_dir=Path(site), port=0, host="127.0.0.1", is_mount=lambda path: True,
    bot_port_busy=lambda: False, child_command=[python, script, "run"], watchdog_command=[python, script, "watchdog"],
    probe_command=[python, script, "probe"],
    build_command=lambda out: [python, script, "site", "--out", str(out), "--schedule", "loop"],
    timings=serve.Timings(tick=0.05, shutdown_grace=float(grace), loop=1, build_offset=0))
sys.exit(supervisor.run())
'''

LOCK_HOLDER = ("import fcntl, os, sys, time\n"
               "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
               "fcntl.flock(fd, fcntl.LOCK_EX)\n"
               "print('locked', flush=True)\n"
               "time.sleep(60)\n")

# Keeps every other connection out of the database until its stdin closes, as a sqlite3 without -readonly can: EXCLUSIVE
# locking mode, and a read to take the lock.
DB_HOLDER = ("import sqlite3, sys\n"
             "connection = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
             "connection.execute('PRAGMA locking_mode=EXCLUSIVE')\n"
             "connection.execute('BEGIN EXCLUSIVE')\n"
             "connection.execute('SELECT COUNT(*) FROM state').fetchone()\n"
             "print('locked', flush=True)\n"
             "sys.stdin.read()\n"
             "connection.execute('COMMIT')\n"
             "connection.close()\n")


def make_run(data, strategy=STRATEGY, quote="USD", started=1793886825.85, record=None, beats=()):
    """A paper run's database as the runner leaves it: the runner's own schema, in WAL mode, and a start record, either
    record as given (False for none) or one for strategy and quote. Returns its sha256, as the seed's marker has it."""
    if record is None:
        record = json.dumps({"strategy_id": strategy, "quote": quote, "started_at": started})
    db = DB(data / "paper_systematic.db")
    try:
        db.conn.executescript(SYSTEMATIC_SCHEMA)
        if record is not False:
            db.set("sys_start", record)
        for ts in beats:
            db.execute("INSERT INTO sys_heartbeats(ts, status, halts, controls) VALUES(?, 'ok', '[]', '[]')", (ts,))
    finally:
        db.conn.close()
    return None if record is False else hashlib.sha256(record.encode()).hexdigest()


def add_beat(data, ts):
    """A heartbeat in the run's database at ts, as the loop writes one at each check."""
    connection = sqlite3.connect(data / "paper_systematic.db", timeout=10)
    try:
        with connection:
            connection.execute("INSERT INTO sys_heartbeats(ts, status, halts, controls) VALUES(?, 'ok', '[]', '[]')",
                               (ts,))
    finally:
        connection.close()


def remove_run(data):
    for name in ("paper_systematic.db", "paper_systematic.db-wal", "paper_systematic.db-shm"):
        (data / name).unlink(missing_ok=True)


def write_seed(data, start_sha256):
    """render_seed.json, as ctl.py seed writes it."""
    marker = {"schema": 1, "sha": "c" * 40, "db_sha256": "d" * 64, "seeded_at": 1794050000.0,
              "seeded_at_utc": "2026-11-07 11:13:20 UTC", "sys_tick_at": 1794049200.0,
              "last_cron_at": 1794049200.0, "cron_runs": 245, "start_sha256": start_sha256}
    (data / "render_seed.json").write_text(json.dumps(marker))
    return marker


def writes_in(database):
    """The writer's count so far, read as any reader would."""
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        row = connection.execute("SELECT value FROM state WHERE key = 'writes'").fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class Clock:
    """time.time, moved on by hand: minutes or hours pass at once."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.time() + self.offset

    def advance(self, seconds):
        self.offset += seconds


class Harness(unittest.TestCase):
    """A data folder (the disk: a mount while self.mounted says so), a repository root with its own config.toml, the
    stand-in script and clock, and supervisors made on them."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.data, self.root, self.fakes, self.site = (self.tmp / name for name in ("data", "repo", "fakes", "site"))
        for folder in (self.data, self.root, self.fakes):
            folder.mkdir()
        (self.root / "config.toml").write_text(CONFIG)
        self.script = self.tmp / "fake_duo.py"
        self.script.write_text(FAKE_DUO)
        self.mounted, self.busy, self.clock, self.out = True, False, Clock(), None

    def replace_run(self, **options):
        """A new paper run's database, and the seed's marker for it."""
        remove_run(self.data)
        self.sha = make_run(self.data, **options)
        self.marker = write_seed(self.data, self.sha)

    def env(self, **extra):
        env = {key: value for key, value in os.environ.items() if not key.startswith("DUO_")}
        env.update(FAKE_OUT=str(self.fakes), RENDER_GIT_COMMIT=COMMIT, **extra)
        return env

    def supervisor(self, data_dir=DATA, **options):
        """A supervisor on the stand-ins, with the data folder as DUO_DATA_DIR unless data_dir is given."""
        python, script = sys.executable, str(self.script)
        self.out = io.StringIO()
        settings = dict(root=self.root, site_dir=self.site, port=0, host="127.0.0.1", env=self.env(),
                        clock=self.clock, is_mount=lambda path: self.mounted and path == str(self.data),
                        bot_port_busy=lambda: self.busy, child_command=[python, script, "run"],
                        build_command=lambda out: [python, script, "site", "--out", str(out), "--schedule", "loop"],
                        watchdog_command=[python, script, "watchdog"], probe_command=[python, script, "probe"],
                        timings=FAST, out=self.out)
        settings.update(options)
        supervisor = serve.Supervisor(str(self.data) if data_dir is DATA else data_dir, **settings)
        self.addCleanup(supervisor._release_lock)
        return supervisor

    def serving(self, supervisor):
        """supervisor's HTTP server, on a port of its own on 127.0.0.1."""
        self.assertTrue(supervisor.listen())
        self.addCleanup(supervisor.server.server_close)
        self.addCleanup(supervisor.server.shutdown)
        return supervisor

    def running(self, supervisor):
        """supervisor.run() in a thread, as on Render, but stopped by stop() where Render sends SIGTERM."""
        results = []
        thread = threading.Thread(target=lambda: results.append(supervisor.run(signals=False)), daemon=True)
        thread.start()

        def finish():
            supervisor.stop()
            thread.join(30)
            self.assertEqual(results, [0], "it stopped, and 0 was all it returned")

        self.addCleanup(finish)
        self.thread, self.results = thread, results
        return supervisor

    def until(self, condition, what, timeout=15, said=None):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"{what}: not within {timeout} s. It said:\n{(said or self.said)()}")
            time.sleep(0.02)

    def said(self):
        return self.out.getvalue() if self.out else ""

    def mode(self, role, how):
        (self.fakes / f"{role}.mode").write_text(how)

    def calls(self, role=None, event=None):
        """What the stand-ins did, in order: whole lines only, as the last may be half written."""
        path = self.fakes / "calls.jsonl"
        found = [json.loads(line) for line in (path.read_text().split("\n")[:-1] if path.exists() else [])]
        return [call for call in found if role in (None, call["role"]) and event in (None, call["event"])]

    def get(self, path, supervisor=None, method="GET"):
        """(status, headers, body) of a request to the supervisor's server."""
        connection = http.client.HTTPConnection("127.0.0.1", (supervisor or self.sup).port, timeout=10)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()


class Gate(Harness):
    """Each check before the bot starts, in its order: the first that fails is the reason it waits."""

    def setUp(self):
        super().setUp()
        self.replace_run(beats=[1794049200.0])
        self.sup = self.supervisor()
        self.assertTrue(self.sup.take_lock())

    def test_it_passes_when_everything_holds(self):
        self.assertIsNone(self.sup.gate())

    def test_a_folder_that_isnt_a_mount_is_refused_before_anything_else(self):
        self.mounted, self.busy = False, True
        (self.data / "HOLD").touch()
        self.assertEqual(self.sup.gate(), f"{self.data} isn't a mount point, so the service's disk isn't there "
                                          f"(RUNBOOK 13.2)")

    def test_hold_keeps_the_bot_stopped(self):
        self.busy = True
        (self.data / "HOLD").touch()
        self.assertEqual(self.sup.gate(), serve.HELD)

    def test_no_database_is_no_bot(self):
        remove_run(self.data)
        reason = self.sup.gate()
        self.assertIn("no paper run on the disk yet", reason)
        self.assertIn("the bot never starts one", reason)

    def test_no_start_record_or_a_broken_one_is_refused(self):
        for record, says in ((False, "it has no start record"), ("{half", "isn't a JSON object"),
                             ('["a list"]', "isn't a JSON object")):
            with self.subTest(record=record):
                self.replace_run(record=record)
                self.assertIn(says, self.sup.gate())
        remove_run(self.data)
        (self.data / "paper_systematic.db").write_bytes(b"not a database " * 100)
        self.assertIn("couldn't be read (file is not a database)", self.sup.gate())

    def test_a_start_record_for_another_strategy_or_quote_is_refused(self):
        for strategy, quote in (("F1__ENS__BTC_ETH__vol40", "USD"), (STRATEGY, "GBP")):
            with self.subTest(strategy=strategy, quote=quote):
                self.replace_run(strategy=strategy, quote=quote)
                self.assertEqual(self.sup.gate(), f"{self.data / 'paper_systematic.db'} holds a paper run of "
                                                  f"{strategy} in {quote}, but config.toml asks for {STRATEGY} in USD")

    def test_the_start_record_the_runner_writes_for_config_toml_passes(self):
        """The real config.toml, read as the runner reads it: the start record prepare() writes for its strategy
        passes, and one written for another strategy is refused."""
        configured = tomllib.loads((ROOT / "config.toml").read_text())["systematic"]["strategy"]
        for strategy in (configured, "F1__ENS__BTC_ETH__vol40"):
            with self.subTest(strategy=strategy):
                data = self.tmp / strategy
                data.mkdir()
                config = replace(make_sys_config(strategy, max_asset_pct=Decimal(100)), data_dir=data)
                db = DB(config.db_path)
                prepare(config, db, FakeSysMarket({"BTC": 70000.0, "ETH": 2500.0}, 1794050000.0))
                write_seed(data, hashlib.sha256(db.get("sys_start").encode()).hexdigest())
                db.conn.close()
                supervisor = serve.Supervisor(str(data), root=ROOT, is_mount=lambda path: True,
                                              bot_port_busy=lambda: False, out=io.StringIO())
                self.addCleanup(supervisor._release_lock)
                self.assertTrue(supervisor.take_lock())
                reason = supervisor.gate()
                if strategy == configured:
                    self.assertIsNone(reason)
                else:
                    self.assertIn(f"{strategy} in USD, but config.toml asks for {configured} in USD", reason)

    def test_config_toml_must_have_the_systematic_runner_on_as_duo_reads_it(self):
        """Unless [systematic] enabled is true, python -m duo run starts the agents' paper engine on paper.db, or
        refuses to start, never this run. The real config.toml, changed each way and read by duo's own load(): the
        gate passes exactly when duo would run the systematic runner."""
        real = (ROOT / "config.toml").read_text()
        self.assertRegex(real, r"(?m)^enabled = true$")
        for name, text, says in (
                ("on", real, None),
                ("false", re.sub(r"(?m)^enabled = true$", "enabled = false", real), "the systematic runner off"),
                ("missing, which duo reads as false", re.sub(r"(?m)^enabled = true\n", "", real),
                 "the systematic runner off"),
                ("no [systematic] at all", re.sub(r"(?ms)^\[systematic\]\n.*?(?=^\[)", "", real),
                 "the systematic runner off"),
                ("not true or false", re.sub(r"(?m)^enabled = true$", 'enabled = "true"', real),
                 "isn't true or false")):
            with self.subTest(name):
                (self.root / "config.toml").write_text(text)
                try:
                    on = config_module.load(self.root / "config.toml").systematic_on
                except config_module.ConfigError:
                    on = False
                self.assertEqual(on, says is None, "duo reads it this way")
                reason = self.sup.gate()
                if says is None:
                    self.assertIsNone(reason)
                else:
                    self.assertIsNotNone(reason, "the gate let the bot start")
                    self.assertIn(says, reason)
                    self.assertIn("RUNBOOK 13.10", reason)

    def test_no_seed_marker_or_one_for_another_start_record_is_refused(self):
        marker = self.data / "render_seed.json"
        for text, says in ((None, "didn't come through the seed"), ("{half", "couldn't be read"),
                           (json.dumps({**self.marker, "start_sha256": "e" * 64}), "written for another start record"),
                           ('"not an object"', "written for another start record")):
            with self.subTest(marker=text):
                if text is None:
                    marker.unlink(missing_ok=True)
                else:
                    marker.write_text(text)
                self.assertIn(says, self.sup.gate())

    def test_something_on_the_bot_s_port_is_another_bot(self):
        self.busy = True
        self.assertEqual(self.sup.gate(), "something already listens on 127.0.0.1:8787, so another bot may be running")

    def test_the_look_at_the_bot_s_port_finds_a_listener(self):
        self.assertEqual(serve.BOT_PORT, tomllib.loads((ROOT / "config.toml").read_text())["dashboard"]["port"])
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertTrue(serve.listening("127.0.0.1", port))
        self.assertFalse(serve.listening("127.0.0.1", port))

    def test_a_lock_another_process_holds_is_another_supervisor(self):
        self.sup._release_lock()
        holder = subprocess.Popen([sys.executable, "-c", LOCK_HOLDER, str(self.data / ".supervisor.lock")],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        other = self.supervisor()
        self.assertFalse(other.take_lock())
        self.assertEqual(other.gate(), "another supervisor holds the lock")
        holder.kill()
        holder.wait()
        self.assertTrue(other.take_lock())
        self.assertIsNone(other.gate())

    def test_it_reads_the_database_read_only_and_never_immutable(self):
        """Plain mode=ro, with SQLite's locking: immutable=1 could misread a database that's being written."""
        opened, connect = [], sqlite3.connect

        def recorded(database, *args, **kwargs):
            opened.append(str(database))
            return connect(database, *args, **kwargs)

        database = self.data / "paper_systematic.db"
        before = database.read_bytes()
        with mock.patch.object(serve.sqlite3, "connect", recorded):
            self.assertIsNone(self.sup.gate())
            self.assertTrue(self.sup.backup(), self.sup.status()["backup"]["error"])
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual([name for name in opened if name.startswith("file:")],
                         [f"{database.resolve().as_uri()}?mode=ro"] * 2, "the gate's look and the backup's copy")
        self.assertFalse([name for name in opened if "immutable" in name])


class Lifecycle(Harness):
    """The bot's lifecycle, with supervisor.run() going in a thread: its start, HOLD, exits and their backoff, a
    wedged bot, the probe, BACKUP_NOW, the log, and a DUO_DATA_DIR it can't use."""

    def setUp(self):
        super().setUp()
        self.replace_run()

    def test_the_bot_starts_only_once_the_gate_passes_and_with_the_spec_s_environment(self):
        (self.data / "render_seed.json").unlink()
        self.sup = supervisor = self.running(self.supervisor())
        self.until(lambda: "render_seed.json isn't there" in supervisor.reason, "waiting for the seed's marker")
        time.sleep(0.3)
        self.assertEqual(self.calls("run"), [], "no bot while the gate is shut")
        write_seed(self.data, self.sha)
        self.until(lambda: supervisor.state == "running" and self.calls("run", "start"), "the bot started")
        [start] = self.calls("run", "start")
        self.assertEqual(start["argv"], ["run"])
        self.assertEqual(os.path.realpath(start["cwd"]), os.path.realpath(self.root), "it runs in the repository")
        self.assertEqual(start["env"], {"DUO_DATA_DIR": str(self.data), "PYTHONUNBUFFERED": "1", "TZ": "UTC",
                                        "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
        self.assertEqual(start["pgid"], os.getpgid(0), "in the supervisor's own process group")
        self.until(lambda: "fake bot: started" in supervisor.status()["child"]["tail"], "its output kept")
        self.assertIn("bot: fake bot: started\n", self.said(), "and passed on to the log")
        self.assertEqual(supervisor.status()["child"]["pid"], start["pid"])
        self.until(lambda: self.get("/ok")[::2] == (200, b"ok\n"), "/ok says ok once a build and the watchdog pass")
        time.sleep(0.3)
        self.assertEqual(len(self.calls("run", "start")), 1, "one bot, never a second")

    def test_hold_stops_the_bot_and_removing_it_starts_the_bot_again(self):
        self.sup = supervisor = self.running(self.supervisor())
        self.until(lambda: self.calls("run", "start"), "the bot started")
        (self.data / "HOLD").touch()
        self.until(lambda: supervisor.state == "held", "held")
        self.assertEqual([call["event"] for call in self.calls("run")], ["start", "term", "stop"])
        status = supervisor.status()
        self.assertEqual((status["child"]["pid"], status["hold"], status["reason"]), (None, True, serve.HELD))
        self.assertIn("Stopped.", status["child"]["tail"])
        self.assertEqual(self.get("/ok")[::2], (503, b"held\n"))
        time.sleep(0.3)
        self.assertEqual(len(self.calls("run", "start")), 1, "it stays stopped while held")
        (self.data / "HOLD").unlink()
        self.until(lambda: supervisor.state == "running" and len(self.calls("run", "start")) == 2, "started again")
        self.assertEqual(supervisor.restarts, 0, "a hold isn't a restart")

    def test_a_bot_that_ignores_sigterm_is_killed_after_its_grace(self):
        self.mode("run", "stubborn")
        supervisor = self.running(self.supervisor(timings=replace(FAST, hold_grace=1, shutdown_grace=1)))
        self.until(lambda: self.calls("run", "start"), "the bot started")
        held_at = time.monotonic()
        (self.data / "HOLD").touch()
        self.until(lambda: supervisor.state == "held", "held")
        self.assertGreaterEqual(time.monotonic() - held_at, 1, "it had its grace first")
        self.assertEqual([call["event"] for call in self.calls("run")], ["start", "term"], "it never stopped itself")
        self.assertIn("the bot was still running after 1 s: SIGKILL", self.said())
        self.assertIn(f"the bot stopped, exit code {-signal.SIGKILL}", self.said())

    def test_an_exit_backs_off_30_60_120_300_then_600_s(self):
        self.mode("run", "exit 0")
        supervisor = self.running(self.supervisor())
        waits = []
        for exits in range(1, 7):
            self.until(lambda: supervisor.restarts == exits and supervisor.state == "backoff", f"exit {exits}")
            child = supervisor.status()["child"]
            waits.append(child["retry_at"] - child["last_exit"]["at"])
            self.clock.advance(waits[-1] + 1)
        self.assertEqual(waits, [30, 60, 120, 300, 600, 600])
        self.assertEqual(child["last_exit"]["code"], 0, "an exit 0 counts too")
        self.assertIn("fake bot: refusing to start", child["last_exit"]["tail"])
        self.assertEqual(len(self.calls("run", "start")), 6)

    def test_a_bot_that_ran_30_minutes_starts_the_backoff_afresh(self):
        self.mode("run", "exit 1")
        supervisor = self.running(self.supervisor(timings=replace(FAST, wedge_after=10 ** 9)))
        self.until(lambda: supervisor.restarts == 1 and supervisor.state == "backoff", "the first exit")
        self.clock.advance(31)
        self.until(lambda: supervisor.restarts == 2 and supervisor.state == "backoff", "the second exit")
        child = supervisor.status()["child"]
        self.assertEqual(child["retry_at"] - child["last_exit"]["at"], 60)
        self.mode("run", "beat")
        self.clock.advance(61)
        self.until(lambda: supervisor.state == "running" and len(self.calls("run", "start")) == 3, "a bot that stays")
        self.clock.advance(31 * 60)
        (self.fakes / "crash").touch()
        self.until(lambda: supervisor.restarts == 3 and supervisor.state == "backoff", "its exit, 31 minutes on")
        child = supervisor.status()["child"]
        self.assertEqual((child["retry_at"] - child["last_exit"]["at"], child["last_exit"]["code"]), (30, 1))
        self.assertIn("fake bot: crashed", child["last_exit"]["tail"])

    def test_a_wedged_bot_is_stopped_and_started_again(self):
        self.mode("run", "silent")
        supervisor = self.running(self.supervisor())
        self.until(lambda: self.calls("run", "start"), "the bot started")
        time.sleep(0.2)
        self.assertEqual(supervisor.state, "running", "no heartbeat is fine for its first 20 minutes")
        self.clock.advance(21 * 60)
        self.until(lambda: supervisor.state == "running" and len(self.calls("run", "start")) == 2, "started again")
        self.assertEqual([call["event"] for call in self.calls("run")], ["start", "term", "stop", "start"])
        self.assertEqual(supervisor.restarts, 1)
        self.assertIn("wedged: no readable heartbeat 21 minutes after it started", self.said())

    def test_wedged_takes_20_minutes_running_and_20_quiet_and_no_hold(self):
        supervisor = self.supervisor()
        now, heartbeat, hold = time.time(), self.data / "heartbeat_systematic.json", self.data / "HOLD"
        for running, quiet, held, wedged in ((19, None, False, False), (21, 19, False, False), (21, 21, False, True),
                                             (21, None, False, True), (21, 21, True, False)):
            with self.subTest(running_minutes=running, quiet_minutes=quiet, held=held):
                supervisor.child = serve.Child(None, now - running * 60, None)
                if quiet is None:
                    heartbeat.unlink(missing_ok=True)
                else:
                    heartbeat.write_text(json.dumps({"ts": now - quiet * 60}))
                if held:
                    hold.touch()
                else:
                    hold.unlink(missing_ok=True)
                self.assertEqual(supervisor.wedged(now) is not None, wedged)
        supervisor.child = None

    def test_the_probe_runs_once_at_the_start(self):
        supervisor = self.running(self.supervisor())
        self.until(lambda: supervisor.status()["probe"]["at"] is not None, "the probe ran")
        probe = supervisor.status()["probe"]
        self.assertEqual(probe["exit_code"], 1)
        self.assertEqual(probe["output"], [f"probe line {number}" for number in range(5, 25)], "its last 20 lines")
        time.sleep(0.2)
        self.assertEqual(len(self.calls("probe")), 1)

    def test_backup_now_asks_for_a_backup_and_is_removed(self):
        supervisor = self.running(self.supervisor(timings=replace(FAST, wedge_after=10 ** 9)))
        self.until(lambda: supervisor.status()["backup"]["stamp"], "a first backup, as there's none yet")
        first = supervisor.status()["backup"]["stamp"]
        self.clock.advance(60)
        time.sleep(0.2)
        self.assertEqual(supervisor.status()["backup"]["stamp"], first, "the next isn't due for 6 hours")
        (self.data / "BACKUP_NOW").touch()
        self.until(lambda: not (self.data / "BACKUP_NOW").exists(), "BACKUP_NOW done and removed")
        second = supervisor.status()["backup"]["stamp"]
        self.assertNotEqual(second, first)
        self.assertEqual(os.readlink(self.data / "backups" / "latest"), second)
        self.clock.advance(6 * 3600 + 1)
        self.until(lambda: supervisor.status()["backup"]["stamp"] not in (first, second), "a backup 6 hours on")

    def test_the_loop_s_first_check_is_backed_up_by_itself_within_5_minutes(self):
        """As at the cutover: the seed's database ends at GitHub's last check, the marker's last_cron_at. The backup
        taken as the seed lands holds only that, so GitHub won't keep it; the next comes once the loop has checked."""
        self.replace_run(beats=[1794049200.0])
        supervisor = self.running(self.supervisor(timings=replace(FAST, wedge_after=10 ** 9)))
        self.until(lambda: supervisor.status()["backup"]["stamp"], "the backup as the seed lands")
        first = supervisor.status()["backup"]["stamp"]
        manifest = self.data / "backups" / first / "manifest.json"
        self.assertEqual(json.loads(manifest.read_text())["newest_heartbeat_at"], self.marker["last_cron_at"])
        add_beat(self.data, 1794049500.0)  # The loop's first check on Render.
        self.clock.advance(301)
        self.until(lambda: supervisor.status()["backup"]["stamp"] not in (None, first), "a backup by itself")
        latest = json.loads((self.data / "backups" / "latest" / "manifest.json").read_text())
        self.assertEqual(latest["newest_heartbeat_at"], 1794049500.0)
        self.assertFalse((self.data / "BACKUP_NOW").exists(), "no one asked for it")

    def test_events_go_to_stdout_and_with_the_time_to_supervisor_log_on_the_disk(self):
        supervisor = self.supervisor(timings=replace(FAST, log_bytes=400))
        for number in range(12):
            supervisor.log(f"event {number}")
        self.assertEqual(self.said().splitlines()[-2:], ["supervisor: event 10", "supervisor: event 11"])
        log, moved = self.data / "supervisor.log", self.data / "supervisor.log.1"
        self.assertTrue(moved.exists(), "it moved aside at its size")
        self.assertLess(log.stat().st_size, 400)
        self.assertRegex(log.read_text().splitlines()[-1], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC supervisor: event 11$")
        self.assertTrue(supervisor.status()["log_tail"][-1].endswith(" event 11"))
        self.mounted = False
        supervisor.log("while the disk is away")
        self.assertIn("supervisor: while the disk is away", self.said())
        self.assertNotIn("away", log.read_text() + moved.read_text(), "nothing written where the disk isn't")

    def test_without_a_usable_duo_data_dir_it_serves_a_page_saying_so_and_waits(self):
        for setting, says in ((None, "DUO_DATA_DIR isn't set"), ("relative/data", "isn't an absolute path")):
            with self.subTest(setting=setting):
                self.sup = supervisor = self.running(self.supervisor(data_dir=setting))
                self.until(lambda: says in supervisor.reason, "waiting")
                code, _, page = self.get("/")
                self.assertEqual(code, 200)
                self.assertIn(says.encode(), page)
                code, _, said = self.get("/ok")
                self.assertEqual((code, said[:22]), (503, b"waiting: DUO_DATA_DIR "))
                self.assertEqual(self.get("/healthz")[::2], (200, b"alive\n"))
                disk = json.loads(self.get("/supervisor.json")[2])["data_dir"]
                self.assertEqual((disk["path"], disk["is_mount"]), (setting, False))
                time.sleep(0.2)
                self.assertEqual(self.calls(), [], "no bot, no build and no probe")
                supervisor.stop()
                self.thread.join(10)
                self.assertEqual(self.results, [0])


class Pages(Harness):
    """Page builds: into a fresh folder each time, served through a link swapped in one step, and the last good page
    kept."""

    def setUp(self):
        super().setUp()
        self.replace_run()
        self.sup = self.serving(self.supervisor())

    def test_before_any_build_it_serves_a_page_that_says_so(self):
        code, headers, page = self.get("/")
        self.assertEqual((code, headers["Content-Type"]), (200, "text/html; charset=utf-8"))
        self.assertIn(b"couldn't be built yet", page)
        self.assertIn(b"supervisor says: waiting: starting.", page)
        self.assertIn(b'<a href="supervisor.json">', page)
        self.assertEqual(self.get("/status.json")[0], 404)

    def test_a_good_build_is_served_and_a_failed_one_leaves_the_last_good_page(self):
        self.assertTrue(self.sup.build())
        served = os.readlink(self.site / "current")
        first = self.get("/")[2]
        self.assertIn(b"<p>build ", first)
        for how, says in (("fail", "the database couldn't be read"),
                          ("partial", "it exited 0 without writing both index.html and status.json")):
            with self.subTest(build=how):
                self.mode("site", how)
                self.assertFalse(self.sup.build())
                page = self.sup.status()["page"]
                self.assertFalse(page["ok"])
                self.assertIn(says, page["error"])
                self.assertEqual(os.readlink(self.site / "current"), served, "still the last good build")
                self.assertEqual(self.get("/")[2], first)
                self.assertEqual(os.listdir(self.site / "builds"), [Path(served).name], "the failed one is gone")
        self.mode("site", "ok")
        self.assertTrue(self.sup.build())
        self.assertNotEqual(os.readlink(self.site / "current"), served)
        self.assertNotEqual(self.get("/")[2], first)
        page = self.sup.status()["page"]
        self.assertEqual((page["ok"], page["error"]), (True, None))
        self.assertLess(abs(page["built_at"] - self.clock()), 5)
        self.assertEqual(self.calls("site", "build")[-1]["argv"][-2:], ["--schedule", "loop"])

    def test_it_keeps_three_builds_and_always_the_one_it_serves(self):
        for _ in range(5):
            self.assertTrue(self.sup.build())
        builds = self.site / "builds"
        names = sorted(os.listdir(builds), key=lambda name: int(name.rsplit("-", 1)[1]))
        self.assertEqual(len(names), 3)
        self.assertEqual(os.readlink(self.site / "current"), f"builds/{names[-1]}")
        serve.swap_link(self.site / "current", f"builds/{names[0]}")
        newer = [f"20991231T235959Z-{number}" for number in (1, 2, 3)]
        for name in newer:
            (builds / name).mkdir()
        self.sup._prune_builds()
        self.assertEqual(sorted(os.listdir(builds)), sorted([names[0], *newer]))

    def test_the_watchdog_runs_after_each_good_build_and_only_then(self):
        self.mode("watchdog", "fail")
        self.assertTrue(self.sup.build())
        watchdog = self.sup.status()["watchdog"]
        self.assertFalse(watchdog["ok"])
        self.assertEqual(watchdog["output"][0], "The bot is down: its last heartbeat was 21 minutes ago "
                                                "(2026-11-07 11:13:20 UTC).")
        self.mode("site", "fail")
        self.assertFalse(self.sup.build())
        self.assertEqual(len(self.calls("watchdog")), 1, "none after a failed build")
        self.assertEqual(self.sup.status()["watchdog"], watchdog)

    def test_builds_come_45_s_after_each_of_the_bot_s_loops(self):
        day = calendar.timegm((2026, 11, 12, 0, 0, 0))
        supervisor = self.supervisor(timings=serve.Timings())

        def at(clock):
            hours, minutes, seconds = map(int, clock.split(":"))
            return day + hours * 3600 + minutes * 60 + seconds

        for now, due in (("12:00:00", "12:02:45"), ("12:02:44", "12:02:45"), ("12:02:45", "12:07:45"),
                         ("12:05:00", "12:07:45"), ("12:59:59", "13:02:45"), ("23:58:00", "24:02:45")):
            with self.subTest(now=now):
                self.assertEqual(supervisor.next_build(at(now)), at(due))

    def test_a_change_of_state_asks_for_a_build_10_s_on(self):
        supervisor = self.supervisor(timings=serve.Timings())
        supervisor._set_state("held", serve.HELD)
        self.assertLess(abs(supervisor._build_wanted - (self.clock() + 10)), 1)
        supervisor._build_wanted = None
        supervisor._set_state("held", serve.HELD)
        self.assertIsNone(supervisor._build_wanted, "no change, no build")


class Http(Harness):
    """What it answers on $PORT, and what it won't serve."""

    def setUp(self):
        super().setUp()
        self.replace_run()
        self.sup = self.serving(self.supervisor())
        self.assertTrue(self.sup.take_lock())

    def test_healthz_says_alive_whatever_the_bot_is_doing(self):
        for state in ("waiting", "running", "held", "backoff", "stopping"):
            self.sup.state = state
            self.assertEqual(self.get("/healthz")[::2], (200, b"alive\n"))

    def test_ok_is_200_only_while_the_bot_runs_with_a_fresh_watchdog_pass_and_page(self):
        now = self.clock()

        def put(state="running", reason="", built=now - 60, checked=now - 60, passed=True,
                first="Bot alive: heartbeat 3s ago, status ok."):
            self.sup.state, self.sup.reason = state, reason
            self.sup.page_info["built_at"] = built
            self.sup.watchdog_info.update(at=checked, ok=passed)
            self.sup._watchdog_first = first

        put()
        for path in ("/ok", "/ok.txt"):
            self.assertEqual(self.get(path)[::2], (200, b"ok\n"))
        down = "The bot is down: its last heartbeat was 21 minutes ago (2026-11-07 11:13:20 UTC)."
        for change, said in ((dict(state="held", reason=serve.HELD), "held"),
                             (dict(state="waiting", reason="no paper run on the disk yet"),
                              "waiting: no paper run on the disk yet"),
                             (dict(state="backoff", reason="the bot exited with code 1"),
                              "backoff: the bot exited with code 1"),
                             (dict(state="stopping", reason="HOLD appeared"), "stopping: HOLD appeared"),
                             (dict(built=None), "page not built yet"),
                             (dict(built=now - 12 * 60), "page not rebuilt for 12 minutes"),
                             (dict(checked=None, passed=None), "watchdog: not run yet"),
                             (dict(passed=False, first=down), f"watchdog: {down}"),
                             (dict(checked=now - 11 * 60), "watchdog: last ran 11 minutes ago")):
            with self.subTest(said=said):
                put(**change)
                self.assertEqual(self.get("/ok")[::2], (503, f"{said}\n".encode()))

    def test_supervisor_json_has_the_spec_s_fields_and_never_the_environment(self):
        self.sup._release_lock()
        supervisor = self.serving(self.supervisor(env=self.env(SENTINEL_SETTING="never-in-supervisor-json")))
        self.assertTrue(supervisor.take_lock())
        (self.data / "HOLD").touch()
        code, headers, body = self.get("/supervisor.json", supervisor)
        self.assertEqual((code, headers["Content-Type"]), (200, "application/json"))
        self.assertNotIn(b"never-in-supervisor-json", body)
        status = json.loads(body)
        self.assertEqual(set(status), {"schema", "now", "state", "reason", "hold", "child", "page", "watchdog",
                                       "backup", "probe", "data_dir", "seed", "supervisor", "log_tail"})
        self.assertEqual(set(status["child"]), {"pid", "started_at", "restarts", "retry_at", "last_exit", "vmrss_kb",
                                                "tail"})
        self.assertEqual(set(status["page"]), {"built_at", "ok", "error"})
        self.assertEqual(set(status["watchdog"]), {"at", "ok", "output"})
        self.assertEqual(set(status["backup"]), {"at", "stamp", "sha256", "error"})
        self.assertEqual(set(status["probe"]), {"at", "exit_code", "output"})
        self.assertEqual(set(status["data_dir"]), {"path", "is_mount", "free_bytes", "total_bytes", "db_bytes"})
        self.assertEqual(set(status["supervisor"]), {"pid", "ppid", "pgid", "proc1_cmdline", "started_at", "python",
                                                     "sqlite_version", "commit", "lock"})
        self.assertEqual((status["schema"], status["hold"], status["seed"]), (1, True, self.marker))
        disk = status["data_dir"]
        self.assertEqual((disk["path"], disk["is_mount"]), (str(self.data), True))
        self.assertEqual(disk["db_bytes"], (self.data / "paper_systematic.db").stat().st_size)
        self.assertGreaterEqual(disk["total_bytes"], disk["free_bytes"])
        self.assertGreater(disk["free_bytes"], 0)
        about = status["supervisor"]
        self.assertEqual((about["pid"], about["ppid"], about["pgid"]), (os.getpid(), os.getppid(), os.getpgid(0)))
        self.assertEqual((about["python"], about["commit"], about["lock"]), (platform.python_version(), COMMIT, True))
        # The SQLite that opens the run's database: builds differ in whether they open it as the seed places it.
        self.assertEqual(about["sqlite_version"], sqlite3.sqlite_version)
        self.assertTrue([line for line in status["log_tail"] if "took the lock" in line])

    def test_state_serves_the_latest_backup_and_404_without_one(self):
        kinds = {"paper_systematic.db": "application/octet-stream", "heartbeat_systematic.json": "application/json",
                 "manifest.json": "application/json"}
        for name in kinds:
            self.assertEqual(self.get(f"/state/{name}")[0], 404)
        (self.data / "heartbeat_systematic.json").write_text(json.dumps({"ts": 1794050000.0, "status": "ok"}))
        self.assertTrue(self.sup.backup(), self.sup.status()["backup"]["error"])
        latest = self.data / "backups" / "latest"
        for name, kind in kinds.items():
            with self.subTest(name=name):
                code, headers, body = self.get(f"/state/{name}")
                self.assertEqual((code, headers["Content-Type"]), (200, kind))
                self.assertEqual(body, (latest / name).read_bytes())
        for path in ("/state/", "/state/latest", "/state/supervisor.log", "/state/manifest.json/x"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)

    def test_no_listings_no_dotfiles_and_nothing_outside_the_served_folders(self):
        self.assertTrue(self.sup.build())
        self.assertTrue(self.sup.backup())
        served = Path(os.path.realpath(self.site / "current"))
        (served / "inner").mkdir()
        (served / "inner" / "page.html").write_text("<p>inner</p>")
        (served / "marker.json").symlink_to(self.data / "render_seed.json")
        self.assertEqual(self.get("/status.json?t=1")[0], 200)
        for path in ("/.status.json-half", "/%2estatus.json-half", "/inner/", "/inner/page.html", "/marker.json",
                     "/builds/", "/current/index.html", "/../data/render_seed.json", "/..%2fdata%2frender_seed.json",
                     "/state/../.supervisor.lock", "/state/..%2f.supervisor.lock", "/state/.", "/state//manifest.json",
                     "/index.html/", "/index.html%00", "/%5c..%5cdata"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.get("/", method=method)[0], 501)

    def test_every_answer_says_no_store_and_has_its_content_type(self):
        self.assertTrue(self.sup.build())
        page, data, text = "text/html; charset=utf-8", "application/json", "text/plain; charset=utf-8"
        for path, code, kind in (("/", 200, page), ("/index.html", 200, page), ("/status.json", 200, data),
                                 ("/compare.md", 200, "text/markdown; charset=utf-8"),
                                 ("/decisions.jsonl", 200, "application/x-ndjson"), ("/healthz", 200, text),
                                 ("/ok", 503, text), ("/supervisor.json", 200, data), ("/missing.html", 404, text)):
            with self.subTest(path=path):
                got, headers, body = self.get(path)
                self.assertEqual((got, headers["Content-Type"], headers["Cache-Control"]), (code, kind, "no-store"))
                self.assertEqual(int(headers["Content-Length"]), len(body))
        got, headers, body = self.get("/", method="HEAD")
        self.assertEqual((got, body, headers["Cache-Control"]), (200, b"", "no-store"))
        self.assertEqual(int(headers["Content-Length"]), len(self.get("/")[2]))
        self.assertEqual(self.get("/", method="POST")[1]["Cache-Control"], "no-store")

    def test_a_connection_that_stops_sending_is_closed_after_http_timeout(self):
        """Each read waits at most http_timeout: a visitor who sends part of a request and then nothing is let go, where
        its thread would otherwise wait for as long as the visitor kept the connection open."""
        supervisor = self.serving(self.supervisor(timings=replace(FAST, http_timeout=0.3)))
        with socket.create_connection(("127.0.0.1", supervisor.port), timeout=3) as visitor:
            visitor.sendall(b"GET /hea")
            try:
                self.assertEqual(visitor.recv(100), b"", "closed, with no answer")
            except TimeoutError:
                self.fail("the server kept the connection open for 3 s")
        self.assertEqual(self.get("/healthz", supervisor)[::2], (200, b"alive\n"))

    def test_a_visitor_who_resets_the_connection_leaves_nothing_on_stderr(self):
        """socketserver prints a traceback on stderr for any request that fails, which would bury the supervisor's and
        the bot's lines in Render's Logs. A visitor who hangs up, here with a reset halfway through the request line,
        says nothing at all, and any other failure is one line in the log."""
        server = self.sup.server
        with contextlib.redirect_stderr(io.StringIO()) as stderr, \
                mock.patch.object(server, "shutdown_request", wraps=server.shutdown_request) as finished:
            visitor = socket.create_connection(("127.0.0.1", self.sup.port), timeout=5)
            visitor.sendall(b"GET /hea")
            time.sleep(0.2)  # So the server is reading the request line when the reset comes.
            visitor.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            visitor.close()  # With a linger of 0 s: a reset, not a close.
            self.until(lambda: finished.call_count == 1, "the server done with the connection")
            with mock.patch.object(self.sup, "status", side_effect=RuntimeError("a bug")):
                with self.assertRaises(ConnectionError, msg="closed, with no answer"):
                    self.get("/supervisor.json")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual([line for line in self.said().splitlines() if "HTTP" in line],
                         ["supervisor: an HTTP request failed: RuntimeError('a bug')"])

    def test_a_connection_beyond_http_connections_is_closed_at_once(self):
        """Each connection being handled holds a thread, and one that sends nothing holds it until http_timeout. Past
        http_connections at once, a new one is closed at once, so visitors who stall can't pile up threads, and the
        service answers again as soon as they go."""
        supervisor = self.serving(self.supervisor(timings=replace(FAST, http_connections=2)))
        idle = [socket.create_connection(("127.0.0.1", supervisor.port), timeout=5) for _ in range(2)]
        try:
            with socket.create_connection(("127.0.0.1", supervisor.port), timeout=3) as third:
                try:
                    self.assertEqual(third.recv(100), b"", "closed, with no answer")
                except TimeoutError:
                    self.fail("a third connection was kept open while two were")
        finally:
            for connection in idle:
                connection.close()

        def answers():
            try:
                return self.get("/healthz", supervisor)[::2] == (200, b"alive\n")
            except (OSError, http.client.HTTPException):
                return False

        self.until(answers, "/healthz answered once the two closed")


class Backups(Harness):
    """Backups: a consistent, self-contained copy with its manifest, latest swapped in one step, 8 kept, and a failure
    that leaves nothing behind."""

    def setUp(self):
        super().setUp()
        self.replace_run(beats=[1794049200.0])
        self.sup = self.supervisor()
        self.assertTrue(self.sup.take_lock())

    def stamps(self):
        return sorted(name for name in os.listdir(self.data / "backups") if re.fullmatch(r"\d{8}T\d{6}Z", name))

    def test_a_backup_taken_while_another_process_writes_is_whole_and_its_manifest_says_what_it_holds(self):
        database, stop = self.data / "paper_systematic.db", self.tmp / "stop-writing"
        (self.data / "heartbeat_systematic.json").write_text(json.dumps({"ts": 1794050000.0, "status": "ok"}))
        writer = subprocess.Popen([sys.executable, "-c", WRITER, str(database), str(stop)], stdout=subprocess.PIPE,
                                  text=True)
        self.addCleanup(writer.wait)
        self.addCleanup(writer.kill)
        self.until(lambda: writes_in(database) >= 50, "the writer under way")
        stamps = []
        for _ in range(5):
            self.assertTrue(self.sup.backup(), self.sup.status()["backup"]["error"])
            stamps.append(self.sup.status()["backup"]["stamp"])
            self.clock.advance(1)
            time.sleep(0.05)
        stop.touch()
        written = int(writer.communicate(timeout=30)[0])
        self.assertEqual(len(set(stamps)), 5)
        self.assertEqual(os.readlink(self.data / "backups" / "latest"), stamps[-1])
        counts = []
        for stamp in stamps:
            with self.subTest(stamp=stamp):
                folder = self.data / "backups" / stamp
                self.assertEqual(sorted(os.listdir(folder)), ["heartbeat_systematic.json", "manifest.json",
                                                              "paper_systematic.db"], "and no -wal or -shm")
                copy = folder / "paper_systematic.db"
                self.assertEqual(copy.read_bytes()[18:20], b"\x01\x01", "a rollback-journal file, not WAL")
                connection = sqlite3.connect(f"{copy.as_uri()}?mode=ro&immutable=1", uri=True)
                try:
                    integrity = connection.execute("PRAGMA integrity_check").fetchall()
                    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                    rows, newest = connection.execute(
                        "SELECT COUNT(*), MAX(ts) FROM sys_heartbeats WHERE ts > 1794050000").fetchone()
                    writes = int(connection.execute("SELECT value FROM state WHERE key = 'writes'").fetchone()[0])
                    start = connection.execute("SELECT value FROM state WHERE key = 'sys_start'").fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual((integrity, mode), ([("ok",)], "delete"))
                self.assertEqual(rows, writes, "every write whole, or not there at all: one moment's copy")
                counts.append(writes)
                beat = folder / "heartbeat_systematic.json"
                manifest = json.loads((folder / "manifest.json").read_text())
                self.assertEqual(manifest, {
                    "schema": 1, "taken_at": manifest["taken_at"],
                    "taken_at_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(manifest["taken_at"])),
                    "db": {"sha256": sha256(copy), "size": copy.stat().st_size},
                    "heartbeat": {"sha256": sha256(beat), "size": beat.stat().st_size},
                    "start_sha256": hashlib.sha256(start.encode()).hexdigest(), "newest_heartbeat_at": newest,
                    "integrity": "ok", "seed": self.marker, "commit": COMMIT})
                self.assertEqual(manifest["start_sha256"], self.sha)
                self.assertEqual(beat.read_bytes(), (self.data / "heartbeat_systematic.json").read_bytes())
        self.assertEqual(counts, sorted(counts))
        self.assertLess(counts[0], written, "the writer carried on through the backups")

    def test_a_failed_backup_leaves_nothing_behind_and_says_why(self):
        self.assertTrue(self.sup.backup())
        good = self.sup.status()["backup"]
        (self.data / "BACKUP_NOW").touch()
        for name, spoil, says in (
                ("no start record", lambda: self.replace_run(record=False), "it has no start record"),
                ("another start record", lambda: (remove_run(self.data), make_run(self.data, started=1.0)),
                 "the copy's start record isn't the one render_seed.json was written for"),
                ("not a database", lambda: (remove_run(self.data),
                                            (self.data / "paper_systematic.db").write_bytes(b"not a database " * 100)),
                 "file is not a database")):
            with self.subTest(name):
                spoil()
                self.clock.advance(1)
                self.assertFalse(self.sup.backup())
                backup = self.sup.status()["backup"]
                self.assertEqual([backup[key] for key in ("at", "stamp", "sha256")],
                                 [good[key] for key in ("at", "stamp", "sha256")])
                self.assertIn(says, backup["error"])
                self.assertEqual(sorted(os.listdir(self.data / "backups")), sorted([good["stamp"], "latest"]),
                                 "nothing half made is left")
                self.assertEqual(os.readlink(self.data / "backups" / "latest"), good["stamp"])
                self.assertTrue((self.data / "BACKUP_NOW").exists(), "the request stands")
                self.assertFalse(self.sup.backup_due(self.clock()), "not again at once")
                self.assertTrue(self.sup.backup_due(self.clock() + 301), "but 5 minutes on")

    def test_a_database_held_locked_fails_the_backup_rather_than_holding_everything_up(self):
        """A connection in EXCLUSIVE locking mode keeps every reader out for as long as it likes. CPython's backup()
        would wait for it with no limit, holding the lock that the bot's starts and stops and the page builds wait for,
        so no HOLD could stop the bot meanwhile. Once the database has been locked for backup_lock_wait, the backup
        fails instead, leaving nothing behind, and the next works once the database is free."""
        self.sup._release_lock()
        self.sup = supervisor = self.supervisor(timings=replace(FAST, backup_lock_wait=0.5))
        self.assertTrue(supervisor.take_lock())
        holder = subprocess.Popen([sys.executable, "-c", DB_HOLDER, str(self.data / "paper_systematic.db")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.stdin.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        results = []
        backing_up = threading.Thread(target=lambda: results.append(supervisor.backup()), daemon=True)
        backing_up.start()
        backing_up.join(10)
        if backing_up.is_alive():
            holder.kill()  # So the backup can end, rather than outlive the test.
            backing_up.join(30)
            self.fail("backup() was still waiting for the locked database 10 s on")
        self.assertEqual(results, [False])
        self.assertIn("the database stayed locked for over 0.5 s, so no backup was taken: something holds it locked, "
                      "a sqlite3 without -readonly in the Shell, say (RUNBOOK 13.10)",
                      supervisor.status()["backup"]["error"])
        self.assertEqual(os.listdir(self.data / "backups"), [], "no backup, and no staging folder left")
        self.assertTrue(supervisor._ops.acquire(blocking=False), "the lock it held is free")
        supervisor._ops.release()
        holder.stdin.close()
        self.assertEqual(holder.wait(10), 0, "it let go")
        self.assertFalse(supervisor.backup_due(self.clock()), "not again at once")
        self.clock.advance(301)
        self.assertTrue(supervisor.backup_due(self.clock()), "but 5 minutes on")
        self.assertTrue(supervisor.backup(), supervisor.status()["backup"]["error"])

    def test_one_is_due_every_6_hours_once_the_seed_is_in_held_or_not(self):
        other = self.supervisor()
        self.assertFalse(other.backup_due(self.clock()), "never without the lock")
        now = self.clock()
        self.assertTrue(self.sup.backup_due(now), "none yet")
        marker = self.data / "render_seed.json"
        marker.rename(self.tmp / "marker")
        self.assertFalse(self.sup.backup_due(now), "not before the seed")
        (self.tmp / "marker").rename(marker)
        (self.data / "HOLD").touch()
        self.assertTrue(self.sup.backup_due(now), "held makes no difference")
        self.assertTrue(self.sup.backup())
        taken = self.sup.status()["backup"]["at"]
        self.assertFalse(self.sup.backup_due(taken + 5 * 3600))
        self.assertTrue(self.sup.backup_due(taken + 6 * 3600 + 1))
        (self.data / "BACKUP_NOW").touch()
        self.assertTrue(self.sup.backup_due(taken + 60), "BACKUP_NOW asks for one at once")
        (self.data / "BACKUP_NOW").unlink()
        self.sup._release_lock()
        self.assertTrue(other.take_lock())
        self.assertFalse(other.backup_due(taken + 60), "after a restart, the newest on the disk counts")
        self.assertEqual(other.status()["backup"]["stamp"], self.sup.status()["backup"]["stamp"])

    def test_once_the_loop_has_checked_a_backup_holding_that_check_is_due_by_itself(self):
        """The first backup is taken as the seed lands, before the loop's first check, and GitHub keeps only a backup
        holding one (pull_backup.py). So once the database holds a check after the marker's last_cron_at while the
        latest backup doesn't, one is due: looked for every 5 minutes, without BACKUP_NOW or 6 hours."""
        cron = self.marker["last_cron_at"]
        self.assertTrue(self.sup.backup())  # As the seed lands: GitHub's checks only.
        first = self.sup.status()["backup"]
        now = first["at"]
        self.assertFalse(self.sup.backup_due(now + 1), "no check from the loop yet")
        add_beat(self.data, cron + 300)  # The loop's first check.
        self.assertFalse(self.sup.backup_due(now + 2), "looked for at most every 5 minutes")
        opened, connect = [], sqlite3.connect

        def recorded(database, *args, **kwargs):
            opened.append(str(database))
            return connect(database, *args, **kwargs)

        with mock.patch.object(serve.sqlite3, "connect", recorded):
            self.assertTrue(self.sup.backup_due(now + 1 + 300), "due once the loop has checked")
        self.assertEqual(opened, [f"{(self.data / 'paper_systematic.db').resolve().as_uri()}?mode=ro"],
                         "the database read as the gate reads it: plain read-only, never immutable")
        self.assertIn("a backup is due: the loop's checks on Render (the newest at ", self.said())
        self.clock.advance(1)
        self.assertTrue(self.sup.backup())
        second = self.sup.status()["backup"]
        self.assertNotEqual(second["stamp"], first["stamp"])
        manifest = json.loads((self.data / "backups" / "latest" / "manifest.json").read_text())
        self.assertEqual(manifest["newest_heartbeat_at"], cron + 300)
        add_beat(self.data, cron + 600)
        for later in (600, 1200, 5 * 3600):
            self.assertFalse(self.sup.backup_due(now + later), "the latest backup holds a check from the loop")
        self.assertTrue(self.sup.backup_due(second["at"] + 6 * 3600 + 1), "and the 6-hourly ones carry on")

    def test_it_asks_once_per_newest_heartbeat_and_never_makes_a_stream_of_backups(self):
        cron = self.marker["last_cron_at"]
        self.assertTrue(self.sup.backup())
        now = self.sup.status()["backup"]["at"]
        add_beat(self.data, cron + 300)
        self.assertTrue(self.sup.backup_due(now))
        # A backup that failed made nothing: it's asked for again, but only once there's a newer check.
        self.assertFalse(self.sup.backup_due(now + 301), "never twice for the same newest heartbeat")
        add_beat(self.data, cron + 600)
        self.assertTrue(self.sup.backup_due(now + 602))
        # A copy that somehow still lacks the check isn't followed by another, and another: 8 are kept, and one every 5
        # minutes would rotate every good one out within 40 minutes.
        self.clock.advance(700)
        self.assertTrue(self.sup.backup())
        taken = self.sup.status()["backup"]["at"]
        latest = self.data / "backups" / "latest" / "manifest.json"
        latest.write_text(json.dumps({**json.loads(latest.read_text()), "newest_heartbeat_at": cron}))
        for step in range(1, 13):
            add_beat(self.data, cron + 600 + 300 * step)
            self.assertFalse(self.sup.backup_due(taken + 301 * step), f"{step * 5} minutes on")
        self.assertTrue(self.sup.backup_due(taken + 6 * 3600 + 1), "the 6-hourly ones carry on")

    def test_it_keeps_eight_and_never_the_one_latest_points_to(self):
        for _ in range(10):
            self.assertTrue(self.sup.backup())
            self.clock.advance(7 * 3600)
        stamps = self.stamps()
        self.assertEqual(len(stamps), 8)
        self.assertEqual(os.readlink(self.data / "backups" / "latest"), stamps[-1])
        serve.swap_link(self.data / "backups" / "latest", stamps[0])
        newer = [f"209901{day:02d}T000000Z" for day in range(1, 9)]
        for name in newer:
            (self.data / "backups" / name).mkdir()
        self.sup._prune_backups()
        self.assertEqual(self.stamps(), [stamps[0], *newer])


class Commands(unittest.TestCase):
    """What it runs, and what it's made of: one bot, the read-only build, the watchdog and the probe, and nothing but
    the standard library."""

    def test_the_commands_it_builds_are_one_bot_the_read_only_build_the_watchdog_and_the_probe(self):
        supervisor = serve.Supervisor("/var/data", out=io.StringIO())
        python, folder = sys.executable, Path("/tmp/duo-site/builds/20261112T131320Z-1")
        commands = [supervisor.child_command, supervisor.build_command(folder), supervisor.watchdog_command,
                    supervisor.probe_command]
        self.assertEqual(commands, [[python, "-m", "duo", "run"],
                                    [python, "-m", "duo.site", "--out", str(folder), "--schedule", "loop"],
                                    [python, "-m", "duo", "watchdog"], [python, "-m", "duo", "probe"]])
        self.assertEqual([command[3] for command in commands if command[1:3] == ["-m", "duo"]],
                         ["run", "watchdog", "probe"], "one run, and never duo site or duo tick")

    def test_it_never_names_duo_site_or_tick_and_imports_only_the_standard_library(self):
        source = SERVE.read_text()
        self.assertIsNone(re.search(r'"duo",\s*"(site|tick)"', source))
        imported = set(re.findall(r"^(?:from|import) (\w+)", source, re.M))
        self.assertLessEqual(imported, set(sys.stdlib_module_names) | {"__future__"})

    def test_the_service_s_timings_are_the_spec_s(self):
        t = serve.Timings()
        self.assertEqual((t.hold_grace, t.wedge_after, t.wedge_grace, t.shutdown_grace), (180, 1200, 60, 20))
        self.assertEqual((t.backoff, t.backoff_reset), ((30, 60, 120, 300, 600), 1800))
        self.assertEqual((t.loop, t.build_offset, t.debounce), (300, 165, 10))
        self.assertEqual((t.build_timeout, t.watchdog_timeout, t.probe_timeout), (120, 60, 120))
        self.assertEqual((t.fresh, t.backup_every, t.keep_builds, t.keep_backups), (600, 6 * 3600, 3, 8))
        self.assertEqual((t.lock_retry, t.log_bytes), (30, 1 << 20))
        self.assertEqual((t.backup_lock_wait, t.http_timeout, t.http_connections), (30, 30, 64))
        # A tick to notice the signal, the bot's grace, a second each for the two threads it waits for, and half a
        # second for the HTTP server: it's out before 25 s, and Render's SIGKILL comes at 30.
        self.assertLess(t.tick + t.shutdown_grace + 2 + 0.5, 25)


class Shutdown(Harness):
    """SIGTERM, as Render sends it, to a supervisor in a process of its own, with the bot up, a slow build under way and
    two connections held open: it exits 0 well within Render's 30 s, and leaves no process behind."""

    def test_sigterm_stops_the_bot_kills_the_build_and_exits_0_in_time(self):
        self.replace_run()
        launcher = self.tmp / "launcher.py"
        launcher.write_text(LAUNCHER)
        for bot, grace, within in (("beat", 20, 5), ("stubborn", 2, 7)):
            with self.subTest(bot=bot):
                self.fakes = self.tmp / f"fakes-{bot}"
                self.fakes.mkdir()
                self.mode("run", bot)
                log = self.tmp / f"supervisor-{bot}.log"
                with open(log, "w") as out:
                    process = subprocess.Popen([sys.executable, str(launcher), str(SERVE), str(self.data),
                                                str(self.root), str(self.tmp / f"site-{bot}"), str(self.script),
                                                str(grace)], env=self.env(), stdout=out, stderr=subprocess.STDOUT)
                self.addCleanup(lambda process=process: process.poll() is None and process.kill())
                said = log.read_text
                self.until(lambda: self.calls("run", "start"), "the bot started", said=said)
                builds = len(self.calls("site"))
                self.mode("site", "slow")
                self.until(lambda: [c for c in self.calls("site")[builds:] if c["how"] == "slow"], "a slow build",
                           said=said)
                build = [call for call in self.calls("site") if call["how"] == "slow"][0]
                bot_pid = self.calls("run", "start")[0]["pid"]
                # Two visitors who keep their connections open, one halfway through a request line, each with a thread
                # waiting to read: they mustn't hold up the exit.
                port = int(re.search(r"supervisor: serving on 127\.0\.0\.1:(\d+)\n", said())[1])
                held = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]
                for connection in held:
                    self.addCleanup(connection.close)
                held[0].sendall(b"GET /hea")
                with socket.create_connection(("127.0.0.1", port), timeout=5) as visitor:
                    visitor.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
                    self.assertTrue(visitor.recv(100).startswith(b"HTTP/1.0 200 "),
                                    "answered, after the two were taken")
                asked = time.monotonic()
                process.send_signal(signal.SIGTERM)
                if bot == "beat":
                    process.send_signal(signal.SIGTERM)  # A second changes nothing.
                code = process.wait(timeout=30)
                took = time.monotonic() - asked
                self.assertEqual(code, 0, said())
                self.assertLess(took, within, said())
                self.assertIn("supervisor: shutting-down: SIGTERM\n", said())
                self.assertIn("supervisor: stopped\n", said())
                if bot == "beat":
                    self.assertEqual([call["event"] for call in self.calls("run")], ["start", "term", "stop"])
                else:
                    self.assertGreaterEqual(took, grace, "it had its grace first")
                    self.assertEqual([call["event"] for call in self.calls("run")], ["start", "term"])
                    self.assertIn(f"the bot was still running after {grace} s: SIGKILL", said())
                for pid in (bot_pid, build["pid"]):
                    self.until(lambda: not alive(pid), f"process {pid} gone", said=said)


if __name__ == "__main__":
    unittest.main()
