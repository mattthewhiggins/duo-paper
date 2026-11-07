"""python -u ops/render/serve.py: the paper bot's supervisor, and Render's start command for its web service.

On Render the bot is one `python -m duo run`, this process's child, on the service's disk (DUO_DATA_DIR, /var/data).
`python -m duo run` starts a NEW paper run on any database without a start record, so the bot is started only while all
of these hold, and the first that doesn't is published as the reason it's waiting:
1. DUO_DATA_DIR is a mount point: the disk, not the instance's own files, which every deploy wipes.
2. There's no HOLD file there. The owner holds the bot with one (RUNBOOK 13.6), and it stays stopped.
3. paper_systematic.db is there,
4. holding a start record for config.toml's strategy and quote, and config.toml has the systematic runner on: without
   [systematic] enabled = true, python -m duo run would start the agents' paper engine on paper.db instead,
5. and render_seed.json, which the seed writes, carries that start record's hash: the database came through the seed.
6. Nothing listens on 127.0.0.1:8787, where a running bot serves its dashboard, so no other bot runs here.
7. This process holds the lock on DUO_DATA_DIR/.supervisor.lock, so no other supervisor does.

A HOLD stops a running bot (SIGTERM, then SIGKILL 180 s later). A bot that exits by itself is started again after 30 s,
then 60, 120 and 300 s, then every 600 s while it keeps exiting; one whose heartbeat is over 20 minutes old is stopped
and started again. 45 s after each of the bot's 5-minute loops the page is rebuilt with `python -m duo.site`, which only
reads, into a fresh folder that's served once it's complete, and then `python -m duo watchdog` runs. Every 6 hours,
or when BACKUP_NOW appears, the database is copied with SQLite's backup API into DUO_DATA_DIR/backups, keeping the
newest 8. The first copy is taken as the seed lands, and the next within 5 minutes of the loop's first check, since
GitHub keeps only a copy holding one.

On $PORT: the page; /healthz, Render's health check, 200 while this runs; /ok, 200 only while the bot runs and the
watchdog's pass and the page are both fresh, for a monitor outside Render; /supervisor.json, what it's doing and why;
and /state/, the latest backup, which GitHub copies each day. Each read or write on a connection waits at most 30 s,
and one that comes while 64 are being handled is closed at once, so visitors who stall can't pile up threads.

It never runs `python -m duo tick`, `python -m duo site` (which opens the database writable and checkpoints it) or a
second bot, and never creates DUO_DATA_DIR. It never exits over a problem it can wait out: Render restarts a service
that exits, and suspends one that keeps doing so. SIGTERM, Render's stop, gives the bot 20 s and ends in exit 0 within
25 s, before Render's SIGKILL at 30 s. It's standard library only and reaches duo through child processes, so a change
to duo that fails to import can't take the page and its port down with it.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import html
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORT = 10000  # Render's default for $PORT.
SITE_DIR = Path("/tmp/duo-site")  # The served builds. A deploy wipes it, and the first build makes it again.
DB_NAME = "paper_systematic.db"
HEARTBEAT_NAME = "heartbeat_systematic.json"
SEED_NAME = "render_seed.json"
MANIFEST_NAME = "manifest.json"
BOT_PORT = 8787  # config.toml's [dashboard] port, where a running bot serves its dashboard.
STAMP = "%Y%m%dT%H%M%SZ"
STAMP_NAME = re.compile(r"\d{8}T\d{6}Z")
BUILD_NAME = re.compile(r"(\d{8}T\d{6}Z)-(\d+)")
TEXT = "text/plain; charset=utf-8"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json",
                 ".md": "text/markdown; charset=utf-8", ".jsonl": "application/x-ndjson", ".txt": TEXT}
STATE_FILES = {DB_NAME: "application/octet-stream", HEARTBEAT_NAME: "application/json",
               MANIFEST_NAME: "application/json"}
HELD = "HOLD is on the disk, so the bot stays stopped until it's removed (RUNBOOK 13.6)"
FALLBACK = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duo paper run</title>
<p>The status page couldn't be built yet. The service's supervisor says: {}.</p>
<p>Everything it knows is in <a href="supervisor.json">supervisor.json</a>.</p>
</html>
"""


@dataclass(frozen=True)
class Timings:
    """How long it waits, in seconds, and how much it keeps. The defaults are the service's; tests shorten them."""
    tick: float = 1.0  # How often the bot's lifecycle and the maintenance thread look around.
    bind_retry: float = 5
    lock_retry: float = 30
    hold_grace: float = 180  # A decision loop whose every request times out takes about 105 s.
    wedge_after: float = 1200
    wedge_grace: float = 60
    shutdown_grace: float = 20  # Render's SIGKILL comes 30 s after SIGTERM; a service with a disk can't change that.
    backoff: tuple[float, ...] = (30, 60, 120, 300, 600)
    backoff_reset: float = 1800  # A bot that ran this long before it exited starts the backoff afresh.
    loop: float = 300  # The bot's loop runs every 5 minutes from minute 2 of the hour...
    build_offset: float = 165  # ...so builds at 02:45, 07:45, ... carry that loop's heartbeat.
    debounce: float = 10  # A build this long after the state last changed: seeded, held, unheld.
    build_timeout: float = 120
    watchdog_timeout: float = 60
    probe_timeout: float = 120
    fresh: float = 600  # /ok wants a watchdog pass and a page build younger than this.
    backup_every: float = 6 * 3600
    # A backup that failed is tried again after this, BACKUP_NOW or not. It's also how often the database is looked at
    # for the loop's first check, which GitHub needs in a backup (Supervisor._catching_up).
    backup_retry: float = 300
    # A backup waits this long for a database that something holds locked, then fails, rather than wait with no limit
    # while the bot's starts and stops and the page builds wait for it (Supervisor._give_up_if_locked).
    backup_lock_wait: float = 30
    keep_builds: int = 3
    keep_backups: int = 8
    log_bytes: int = 1 << 20  # supervisor.log moves to supervisor.log.1 at 1 MB.
    # Each read on a connection, and each 64 KB written, waits at most this long, so only a visitor who stalls, or reads
    # slower than about 2 KB/s, is let go...
    http_timeout: float = 30
    http_connections: int = 64  # ...and one that comes while this many are being handled is closed at once.


@dataclass
class Child:
    """The bot, while it runs."""
    process: subprocess.Popen
    started_at: float
    reader: threading.Thread


class NotBackedUp(Exception):
    """A copy that mustn't become a backup. The message says why."""


class RunnerOff(Exception):
    """config.toml doesn't run the systematic runner, so python -m duo run wouldn't run this paper run. The message
    says why."""


def systematic_off(config: dict) -> str | None:
    """Why config.toml, parsed, doesn't run the systematic runner, or None when it does. It's read as duo/config.py
    reads it: [systematic] enabled is false when it's missing, or the section is, and must be true or false. Anything
    but true, and python -m duo run starts the agents' paper engine on paper.db, or refuses to start: never this run."""
    section = config.get("systematic")
    enabled = section.get("enabled", False) if isinstance(section, dict) else False
    if enabled is True:
        return None
    if isinstance(enabled, bool):
        return ("config.toml has the systematic runner off ([systematic] enabled is false or missing, and duo reads "
                "missing as false), so python -m duo run would start the agents' paper engine on paper.db, not this "
                "paper run: set enabled = true and deploy (RUNBOOK 13.10)")
    return (f"config.toml's [systematic] enabled is {enabled!r}, which isn't true or false, so python -m duo run would "
            f"refuse to start: set enabled = true and deploy (RUNBOOK 13.10)")


def utc(ts: float | None) -> str | None:
    return None if ts is None else time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def listening(host: str, port: int) -> bool:
    """Whether something accepts connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def start_sha256(value) -> str:
    """The sha256 of sys_start's value exactly as stored: what the seed's marker carries as start_sha256."""
    return hashlib.sha256(value if isinstance(value, bytes) else str(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fsync(path: Path) -> None:
    """Flushes a file's bytes, or a folder's entries, to the disk."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def swap_link(link: Path, target: str) -> None:
    """Points link at target in one step: a new link beside it, renamed over it. A folder can't be renamed over one
    with files in it, but a link can, so a reader finds the old target or the new one, never neither."""
    temporary = link.with_name(f".{link.name}-{os.getpid()}")
    with suppress(FileNotFoundError):
        temporary.unlink()
    os.symlink(target, temporary)
    os.replace(temporary, link)


def output_lines(raw: bytes | None) -> list[str]:
    return (raw or b"").decode("utf-8", "replace").splitlines()


def send(process: subprocess.Popen, signum: int) -> None:
    with suppress(ProcessLookupError):
        process.send_signal(signum)


def vmrss_kb(pid: int | None) -> int | None:
    """A process's resident memory in kB, from /proc on Linux. None anywhere else."""
    if pid is None:
        return None
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def proc1_cmdline() -> str | None:
    """PID 1's command line, from /proc on Linux: Render doesn't say how it runs the start command. None elsewhere."""
    try:
        return Path("/proc/1/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return None


class Supervisor:
    """The service. What it reaches outside this process can be swapped for a stand-in, so tests run the real thing:
    is_mount, the four commands, the clock, the port it serves and the look at the bot's port."""

    def __init__(self, data_dir: str | None, root: Path = ROOT, site_dir: Path = SITE_DIR, port: int = PORT,
                 host: str = "0.0.0.0", env: dict | None = None, is_mount=os.path.ismount, bot_port_busy=None,
                 clock=time.time, child_command: list[str] | None = None, build_command=None,
                 watchdog_command: list[str] | None = None, probe_command: list[str] | None = None,
                 timings: Timings = Timings(), out=None):
        python = sys.executable
        self.root, self.site, self.port, self.host = Path(root), Path(site_dir), port, host
        self.env = dict(os.environ if env is None else env)
        self.is_mount, self.clock, self.t, self.out = is_mount, clock, timings, out
        self.bot_port_busy = bot_port_busy or (lambda: listening("127.0.0.1", BOT_PORT))
        self.child_command = child_command or [python, "-m", "duo", "run"]
        # duo.site, never `duo site`: the module only reads, while the command opens the database writable and
        # checkpoints it as it closes, a second writer beside the bot.
        self.build_command = build_command or (
            lambda out: [python, "-m", "duo.site", "--out", str(out), "--schedule", "loop"])
        self.watchdog_command = watchdog_command or [python, "-m", "duo", "watchdog"]
        self.probe_command = probe_command or [python, "-m", "duo", "probe"]

        self.data_dir_setting = data_dir
        self.data = Path(os.path.normpath(data_dir)) if data_dir and os.path.isabs(data_dir) else None
        if self.data is not None:
            self.db, self.heartbeat_file = self.data / DB_NAME, self.data / HEARTBEAT_NAME
            self.seed_file, self.hold_file = self.data / SEED_NAME, self.data / "HOLD"
            self.backup_now, self.backups = self.data / "BACKUP_NOW", self.data / "backups"
            self.lock_file, self.log_file = self.data / ".supervisor.lock", self.data / "supervisor.log"

        self._lock = threading.RLock()  # The published fields below, which the HTTP threads read.
        self._ops = threading.Lock()  # Bot starts and stops, page builds and backups: one at a time.
        self._stop = threading.Event()
        self._signal = None  # Set by the signal handler, which mustn't take a lock: see _on_signal.
        self._helpers, self._helpers_lock = set(), threading.Lock()
        self._print_lock, self._file_lock = threading.Lock(), threading.Lock()
        self.state, self.reason = "waiting", "starting"
        self.child: Child | None = None
        self.output = deque(maxlen=60)  # The bot's last lines.
        self.restarts, self.retry_at, self.last_exit = 0, None, None
        self._failures, self._held, self._backoff_reason = 0, False, ""
        self.page_info = {"built_at": None, "ok": None, "error": None}
        self.watchdog_info = {"at": None, "ok": None, "output": []}
        self._watchdog_first = None
        self.backup_info = {"at": None, "stamp": None, "sha256": None, "error": None}
        self._backup_failed_at = None
        self._catch_up_at, self._caught_up = 0.0, None  # When _catching_up looks next, and what it last asked for.
        self.probe_info = {"at": None, "exit_code": None, "output": []}
        self.log_tail = deque(maxlen=40)
        self._lock_fd, self._lock_problem, self._next_lock_try = None, "the lock hasn't been taken yet", 0.0
        self._next_build, self._build_wanted, self._builds = 0.0, None, 0
        self.server, self._threads = None, []
        self.started_at = clock()

    # ---- Logging ----

    def log(self, message: str) -> None:
        """One line per event: to stdout, which is Render's log, and with the time to supervisor.log on the disk."""
        now = self.clock()
        with self._lock:
            self.log_tail.append(f"{utc(now)} {message}")
        self._print(f"supervisor: {message}")
        if self._mounted():
            with self._file_lock, suppress(OSError):
                if self.log_file.exists() and self.log_file.stat().st_size >= self.t.log_bytes:
                    os.replace(self.log_file, self.log_file.with_name(self.log_file.name + ".1"))
                with open(self.log_file, "a", encoding="utf-8") as log:
                    log.write(f"{utc(now)} supervisor: {message}\n")

    def _print(self, line: str) -> None:
        with self._print_lock, suppress(OSError, ValueError):  # A closed stdout mustn't stop the service.
            print(line, file=self.out or sys.stdout, flush=True)

    def _mounted(self) -> bool:
        try:
            return self.data is not None and bool(self.is_mount(str(self.data)))
        except OSError:
            return False

    # ---- Stopping ----

    def _on_signal(self, signum, frame) -> None:
        # Only a note. A handler runs between two steps of the main thread, which may hold any lock just then, an
        # Event's own included, so taking one here could wait for ever. The main thread looks at least once a tick.
        self._signal = signum

    def stop(self) -> None:
        """Asks it to shut down, as SIGTERM does. Asking again changes nothing."""
        self._stop.set()

    def _stopping(self) -> bool:
        if self._signal is not None:
            self._stop.set()
        return self._stop.is_set()

    # ---- The main thread: the bot's lifecycle ----

    def run(self, signals: bool = True) -> int:
        """The service, until SIGTERM or SIGINT. Then it stops everything and returns 0, its only exit code."""
        if signals:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
        self.log(f"starting: pid {os.getpid()}, Python {platform.python_version()}, "
                 f"commit {self.env.get('RENDER_GIT_COMMIT') or 'unknown'}")
        if self.listen():
            problem = self.data_problem()
            if problem:
                self._set_state("waiting", problem)
                while not self._stopping():  # Only a deploy with DUO_DATA_DIR set can mend this.
                    self._stop.wait(self.t.tick)
            else:
                self._threads = [self._thread(self._probe, "probe"), self._thread(self._maintain, "maintenance")]
                while not self._stopping():
                    try:
                        self.step()
                    except Exception as error:  # A bug here mustn't end the service: it's logged, and tried again.
                        self.log(f"unexpected error while looking after the bot: {error!r}")
                    self._stop.wait(self.t.tick)
        return self.shut_down()

    def listen(self) -> bool:
        """Serves $PORT, first of all: Render puts a deploy live once something answers there. A port in use is tried
        again every few seconds, not exited on. False if asked to stop before it could bind."""
        said = False
        while not self._stopping():
            try:
                server = Server((self.host, self.port), Handler)
                break
            except OSError as error:
                if not said:
                    self.log(f"can't listen on {self.host}:{self.port} ({error}); trying again every "
                             f"{self.t.bind_retry:g} s")
                    said = True
                self._stop.wait(self.t.bind_retry)
        else:
            return False
        server.supervisor, server.slots = self, threading.BoundedSemaphore(self.t.http_connections)
        self.server, self.port = server, server.server_address[1]
        self._thread(server.serve_forever, "http", poll_interval=0.5)
        self.log(f"serving on {self.host}:{self.port}")
        return True

    def data_problem(self) -> str | None:
        """Why DUO_DATA_DIR can't be used at all, or None. Nothing mends that but a deploy with it set."""
        if not self.data_dir_setting:
            return "DUO_DATA_DIR isn't set: set it to /var/data in the service's Environment and deploy (RUNBOOK 13.2)"
        if self.data is None:
            return (f"DUO_DATA_DIR is {self.data_dir_setting!r}, which isn't an absolute path: set it to /var/data and "
                    f"deploy (RUNBOOK 13.2)")
        return None

    def step(self) -> None:
        """One look at the bot: it notices an exit, stops the bot for HOLD or a wedge, or starts it once the gate
        passes."""
        now = self.clock()
        self._take_lock(now)
        child = self.child
        if child is not None:
            code = child.process.poll()
            if code is not None:
                self._exited(child, code, now)
            elif self.hold_file.exists():
                self._stop_child("HOLD appeared", self.t.hold_grace)
            else:
                wedged = self.wedged(now)
                if wedged is None:
                    self._set_state("running")
                elif self._stop_child(f"wedged: {wedged}", self.t.wedge_grace):
                    with self._lock:
                        self.restarts += 1
            return
        if self._mounted() and self.hold_file.exists():
            self._held = True
            self._set_state("held", HELD)
            return
        if self._held:  # HOLD was removed: back to the gate at once, whatever the backoff said.
            self._held = False
            with self._lock:
                self.retry_at = None
        if self.retry_at is not None:
            if now < self.retry_at:
                self._set_state("backoff", self._backoff_reason)
                return
            with self._lock:
                self.retry_at = None
        reason = self.gate()
        if reason is None:
            self._start()
        else:
            self._set_state("held" if reason == HELD else "waiting", reason)

    # ---- The gate ----

    def gate(self) -> str | None:
        """Why the bot mustn't start now, from the first check (this module's docstring lists them) that fails, or None
        when they all pass."""
        if self.data is None:
            return self.data_problem()
        if not self._mounted():
            return f"{self.data} isn't a mount point, so the service's disk isn't there (RUNBOOK 13.2)"
        if self.hold_file.exists():
            return HELD
        if not self.db.exists():
            return (f"no paper run on the disk yet: {self.db} isn't there. It's seeded once, at the cutover "
                    f"(RUNBOOK 13.3), and the bot never starts one")
        start, problem = self.start_record()
        if problem:
            return problem
        try:
            marker = json.loads(self.seed_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return f"{self.seed_file} isn't there, so the database didn't come through the seed (RUNBOOK 13.3)"
        except (OSError, ValueError) as error:
            return f"{self.seed_file} couldn't be read ({error})"
        if not isinstance(marker, dict) or marker.get("start_sha256") != start:
            return f"{self.seed_file} was written for another start record: its start_sha256 isn't the database's"
        if self.bot_port_busy():
            return f"something already listens on 127.0.0.1:{BOT_PORT}, so another bot may be running"
        if self._lock_fd is None:
            return self._lock_problem or "this supervisor doesn't hold the lock"
        return None

    def start_record(self) -> tuple[str | None, str | None]:
        """(start_sha256, None) when the database holds config.toml's paper run, else (None, why not). It's read through
        a plain read-only connection, never immutable=1, which skips SQLite's locking and so could misread a database
        the bot or a command in the Shell is writing."""
        try:
            connection = sqlite3.connect(f"{self.db.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
            try:
                found = None
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'state'").fetchone():
                    found = connection.execute("SELECT value FROM state WHERE key = 'sys_start'").fetchone()
            finally:
                connection.close()
        except sqlite3.Error as error:
            return None, f"{self.db} couldn't be read ({error})"
        if not found:
            return None, f"{self.db} holds no paper run: it has no start record"
        try:
            start = json.loads(found[0])
        except (TypeError, ValueError):
            start = None
        if not isinstance(start, dict):
            return None, f"{self.db}'s start record isn't a JSON object"
        try:
            strategy, quote = self.wanted()
        except RunnerOff as off:
            return None, str(off)
        except KeyError as missing:
            return None, f"config.toml is missing {missing}"
        except (OSError, ValueError, AttributeError) as error:
            return None, f"config.toml couldn't be read ({error})"
        if (start.get("strategy_id"), start.get("quote")) != (strategy, quote):
            return None, (f"{self.db} holds a paper run of {start.get('strategy_id')} in {start.get('quote')}, but "
                          f"config.toml asks for {strategy} in {quote}")
        return start_sha256(found[0]), None

    def wanted(self) -> tuple[str, str]:
        """(strategy, quote) that config.toml asks for, read as duo/config.py reads them. prepare() carries on only a
        run whose start record has both: the strategy is [systematic] strategy, and the quote is the book currency it
        works out from the cost model, which for this run is [portfolio] quote's USD. RunnerOff when config.toml
        doesn't run the systematic runner at all."""
        raw = tomllib.loads((self.root / "config.toml").read_text(encoding="utf-8"))
        off = systematic_off(raw)
        if off:
            raise RunnerOff(off)
        return raw["systematic"]["strategy"].strip(), raw["portfolio"]["quote"].upper()

    # ---- The lock: one supervisor per disk ----

    def take_lock(self) -> bool:
        """Tries for the lock at once. Whether this process holds it."""
        self._next_lock_try = 0.0
        self._take_lock(self.clock())
        return self._lock_fd is not None

    def _take_lock(self, now: float) -> None:
        """Takes the flock on .supervisor.lock for the rest of this process's life, once the disk is there. While
        another process holds it, it's tried again every 30 s."""
        if self._lock_fd is not None or now < self._next_lock_try or not self._mounted():
            return
        self._next_lock_try = now + self.t.lock_retry
        try:
            fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as error:
            problem = f"couldn't open {self.lock_file} ({error})"
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                os.close(fd)
                problem = ("another supervisor holds the lock" if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
                           else f"couldn't lock {self.lock_file} ({error})")
            else:
                with suppress(OSError):  # Only for a person who looks: which process holds it.
                    os.ftruncate(fd, 0)
                    os.write(fd, f"{os.getpid()}\n".encode())
                self._lock_fd, self._lock_problem = fd, None
                self.log(f"took the lock on {self.lock_file}")
                return
        if problem != self._lock_problem:
            self.log(f"{problem}; trying again every {self.t.lock_retry:g} s")
        self._lock_problem = problem

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            with suppress(OSError):
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    # ---- The bot ----

    def child_env(self) -> dict:
        """The environment for the bot, the build, the watchdog and the probe: this one, plus the data folder by its
        absolute path, unbuffered output so Render's log gets each line at once, UTC, and one thread for numpy's maths
        libraries on an instance with half a CPU."""
        env = dict(self.env)
        env.update(DUO_DATA_DIR=str(self.data), PYTHONUNBUFFERED="1", TZ="UTC", OPENBLAS_NUM_THREADS="1",
                   OMP_NUM_THREADS="1")
        return env

    def _start(self) -> None:
        """Starts the bot, in this process's group, unless the gate closed while a build or backup kept it waiting."""
        if not self._acquire():
            return
        try:
            reason = self.gate()
            if reason is not None:
                self._set_state("held" if reason == HELD else "waiting", reason)
                return
            now = self.clock()
            try:
                process = subprocess.Popen(self.child_command, cwd=self.root, env=self.child_env(),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except OSError as error:
                self._back_off(now, 0.0, f"the bot couldn't be started ({error})")
                return
            with self._lock:
                self.output.clear()
            self.child = Child(process, now, self._thread(self._read, "bot-output", process))
            self.log(f"started the bot: pid {process.pid}")
            self._set_state("running")
        finally:
            self._ops.release()

    def _read(self, process: subprocess.Popen) -> None:
        """Passes the bot's output on to Render's log as it comes, keeping the last 60 lines."""
        with suppress(OSError, ValueError), process.stdout:
            for raw in process.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                with self._lock:
                    self.output.append(line[:2000])
                self._print(f"bot: {line}")

    def _tail(self, count: int) -> list[str]:
        with self._lock:
            return list(self.output)[-count:]

    def _exited(self, child: Child, code: int, now: float) -> None:
        """The bot exited without being asked to. Exit 0 counts too: it's meant to run until it's stopped."""
        self.child = None
        child.reader.join(timeout=2)  # So its last lines, which usually say why, are in.
        tail, ran = self._tail(20), now - child.started_at
        with self._lock:
            self.last_exit = {"code": code, "at": now, "tail": tail}
            self.restarts += 1
        self.log(f"the bot exited by itself with code {code} after {ran:.0f} s. Its last lines: "
                 f"{' | '.join(tail[-3:]) or 'none'}")
        self._back_off(now, ran, f"the bot exited with code {code}")

    def _back_off(self, now: float, ran: float, why: str) -> None:
        """Waits before the next start: 30 s, 60 s, 120 s, 300 s, then 600 s each time, from the first again once a
        bot has run for 30 minutes."""
        if ran > self.t.backoff_reset:
            self._failures = 0
        delay = self.t.backoff[min(self._failures, len(self.t.backoff) - 1)]
        self._failures += 1
        with self._lock:
            self.retry_at = now + delay
        self._backoff_reason = f"{why}; it's started again in {delay:g} s, at {utc(now + delay)}"
        self._set_state("backoff", self._backoff_reason)

    def wedged(self, now: float) -> str | None:
        """Why the running bot counts as wedged, or None. It does once it's been running over 20 minutes, isn't held,
        and its heartbeat is over 20 minutes old: the loop beats every 5 minutes, exchange outages included, so only a
        stuck process or a failing disk goes that quiet."""
        child = self.child
        if child is None or now - child.started_at <= self.t.wedge_after or self.hold_file.exists():
            return None
        try:
            beat = float(json.loads(self.heartbeat_file.read_text())["ts"])
        except (OSError, ValueError, KeyError, TypeError):
            return f"no readable heartbeat {(now - child.started_at) / 60:.0f} minutes after it started"
        if now - beat <= self.t.wedge_after:
            return None
        return f"its last heartbeat was {(now - beat) / 60:.0f} minutes ago"

    def _stop_child(self, why: str, grace: float) -> bool:
        """SIGTERM, then SIGKILL if it's still running after grace seconds. False if a shutdown came first: that
        finishes the job, within its own deadline."""
        if not self._acquire():
            return False
        try:
            child = self.child
            self._set_state("stopping", why)
            self.log(f"stopping the bot (pid {child.process.pid}): SIGTERM, and SIGKILL if it's still running after "
                     f"{grace:g} s")
            send(child.process, signal.SIGTERM)
            code = self._wait(child.process, grace)
            if code is None:
                if self._stopping():
                    return False
                self.log(f"the bot was still running after {grace:g} s: SIGKILL")
                child.process.kill()
                code = child.process.wait()
            self._stopped(child, code)
            return True
        finally:
            self._ops.release()

    def _stopped(self, child: Child, code: int) -> None:
        self.child = None
        child.reader.join(timeout=2)
        self.log(f"the bot stopped, exit code {code}")

    def _wait(self, process: subprocess.Popen, seconds: float, interruptible: bool = True) -> int | None:
        """The process's exit code, or None if it's still running after seconds (or, if interruptible, once a shutdown
        is asked for)."""
        deadline = time.monotonic() + seconds
        while True:
            try:
                return process.wait(timeout=max(0.0, min(0.2, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline or (interruptible and self._stopping()):
                    return None

    def shut_down(self) -> int:
        """Stops everything within 25 s of the signal, before Render's SIGKILL at 30 s. The build, the watchdog and the
        probe only read, so they're killed at once; the bot gets SIGTERM and 20 s to finish its loop, then SIGKILL.
        Returns 0."""
        self._stop.set()
        self._set_state("shutting-down", signal.Signals(self._signal).name if self._signal is not None else "asked to")
        self._kill_helpers()
        child = self.child
        if child is not None:
            send(child.process, signal.SIGTERM)
            code = self._wait(child.process, self.t.shutdown_grace, interruptible=False)
            if code is None:
                self.log(f"the bot was still running after {self.t.shutdown_grace:g} s: SIGKILL")
                child.process.kill()
                code = child.process.wait()
            self._stopped(child, code)
        for thread in self._threads:
            thread.join(timeout=1)
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self._release_lock()
        self.log("stopped")
        return 0

    # ---- Helpers: the build, the watchdog and the probe ----

    def _run(self, command: list[str], timeout: float) -> tuple[int | None, list[str]]:
        """Runs a helper, each of which only reads, to its end, or kills it at timeout. Returns its exit code (None if
        it didn't finish) and its output. A shutdown kills it at once."""
        with self._helpers_lock:
            if self._stopping():
                return None, ["not run: shutting down"]
            try:
                process = subprocess.Popen(command, cwd=self.root, env=self.child_env(), stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except OSError as error:
                return None, [f"couldn't be run ({error})"]
            self._helpers.add(process)
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
            return None, output_lines(output) + [f"killed after {timeout:g} s"]
        finally:
            with self._helpers_lock:
                self._helpers.discard(process)
        return process.returncode, output_lines(output)

    def _kill_helpers(self) -> None:
        with self._helpers_lock:
            for process in self._helpers:
                with suppress(OSError):
                    process.kill()

    def _acquire(self) -> bool:
        """Takes the lock that keeps bot starts and stops apart from builds and backups, which read the database as it
        is: a bot starting or stopping mid-build could change it under them. False if a shutdown came first; that
        goes ahead without it."""
        while not self._stopping():
            if self._ops.acquire(timeout=0.2):
                return True
        return False

    def _thread(self, target, name: str, *args, **kwargs) -> threading.Thread:
        thread = threading.Thread(target=target, name=name, args=args, kwargs=kwargs, daemon=True)
        thread.start()
        return thread

    def _probe(self) -> None:
        """python -m duo probe, once at the start: can this instance reach Kraken and Binance? It only reads them."""
        code, lines = self._run(self.probe_command, self.t.probe_timeout)
        with self._lock:
            self.probe_info = {"at": self.clock(), "exit_code": code, "output": lines[-20:]}
        if not self._stopping():
            self.log(f"probe: exit code {code}, {lines[-1] if lines else 'no output'}")

    # ---- The maintenance thread: the page, the watchdog and backups ----

    def _maintain(self) -> None:
        self.build()  # At once, so a deploy's page is up within seconds.
        while not self._stopping():
            try:
                now = self.clock()
                with self._lock:
                    due = now >= self._next_build or (self._build_wanted is not None and now >= self._build_wanted)
                if due:
                    self.build()
                if self.backup_due(self.clock()):
                    self.backup()
            except Exception as error:  # As for the bot's lifecycle: logged, and tried again.
                self.log(f"unexpected error in maintenance: {error!r}")
            self._stop.wait(self.t.tick)

    def next_build(self, now: float) -> float:
        """When the next scheduled build is due: 45 s after the bot's next loop, at minute 2, 7, 12, ... of the hour, so
        the page carries that loop's heartbeat."""
        period, offset = self.t.loop, self.t.build_offset
        return (math.floor((now - offset) / period) + 1) * period + offset

    def build(self) -> bool:
        """Builds the page into a fresh folder and, if that worked, serves it from then on and runs the watchdog. A
        failed build leaves the last good page up. True if it worked."""
        with self._lock:
            self._build_wanted, self._next_build = None, self.next_build(self.clock())
        if not self._acquire():
            return False
        folder, code, lines = None, None, []
        try:
            folder = self._build_folder()
            code, lines = self._run(self.build_command(folder), self.t.build_timeout)
        except OSError as error:
            lines = [f"couldn't make a folder for it in {self.site} ({error})"]
        finally:
            self._ops.release()
        problem = None
        if code != 0:
            problem = "\n".join(lines[-20:]) or f"it exited with code {code}, saying nothing"
        elif not ((folder / "index.html").is_file() and (folder / "status.json").is_file()):
            problem = "\n".join(lines[-19:] + ["it exited 0 without writing both index.html and status.json"])
        else:
            try:
                swap_link(self.site / "current", f"builds/{folder.name}")
            except OSError as error:
                problem = f"couldn't serve {folder.name} ({error})"
        if problem is not None:
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
            with self._lock:
                self.page_info.update(ok=False, error=problem)
            if not self._stopping():  # A build the shutdown killed isn't news.
                self.log(f"page build failed (exit code {code}), so the last good page stays up: "
                         f"{problem.splitlines()[-1]}")
            return False
        with self._lock:
            news = self.page_info["ok"] is not True
            self.page_info = {"built_at": self.clock(), "ok": True, "error": None}
        if news:
            self.log(f"page built and served: {folder.name}")
        self._prune_builds()
        self._watch()
        return True

    def _build_folder(self) -> Path:
        """A new, empty folder under builds/, named by the time and a count."""
        builds = self.site / "builds"
        builds.mkdir(parents=True, exist_ok=True)
        while True:
            self._builds += 1
            folder = builds / f"{time.strftime(STAMP, time.gmtime(self.clock()))}-{self._builds}"
            with suppress(FileExistsError):
                folder.mkdir()
                return folder

    def _prune_builds(self) -> None:
        """Keeps the newest 3 builds, and always the one being served."""
        serving = Path(os.path.realpath(self.site / "current")).name
        found = []
        with suppress(OSError):
            for path in (self.site / "builds").iterdir():
                match = BUILD_NAME.fullmatch(path.name)
                if match and path.is_dir() and not path.is_symlink():
                    found.append((match[1], int(match[2]), path))
        for _, _, path in sorted(found)[:-self.t.keep_builds]:
            if path.name != serving:
                shutil.rmtree(path, ignore_errors=True)

    def _watch(self) -> None:
        """python -m duo watchdog, after each good build: the bot's verdict on its heartbeat, which /ok passes on."""
        code, lines = self._run(self.watchdog_command, self.t.watchdog_timeout)
        if self._stopping():
            return
        ok, first = code == 0, next((line for line in lines if line.strip()), None)
        with self._lock:
            news = self.watchdog_info["ok"] is not ok
            self.watchdog_info = {"at": self.clock(), "ok": ok, "output": lines[-10:]}
            self._watchdog_first = first
        if news:
            self.log(f"watchdog {'passes' if ok else 'fails'}: {first or f'exit code {code}'}")

    # ---- Backups ----

    def backup_due(self, now: float) -> bool:
        """Every 6 hours, or at once when BACKUP_NOW is there, once the seed has placed a run (render_seed.json and the
        database), held or running alike; and only with the lock, so no other supervisor writes backups too. Also
        once the loop's first check is in the database but not yet in the latest backup (_catching_up)."""
        if self._lock_fd is None or not self._mounted() or not (self.seed_file.exists() and self.db.exists()):
            return False
        if self._backup_failed_at is not None and now - self._backup_failed_at < self.t.backup_retry:
            return False
        if self.backup_now.exists():
            return True
        last = self._last_backup_at()
        return last is None or now - last > self.t.backup_every or self._catching_up(now)

    def _catching_up(self, now: float) -> bool:
        """Whether the latest backup still ends at GitHub's last check (its newest_heartbeat_at is at or before the
        marker's last_cron_at) while the database already holds a later one, the loop's. GitHub keeps only a backup
        with a check from the loop in it (pull_backup.py), and the first is taken as the seed lands, before the loop's
        first check: without this, every Render backup run would fail for up to 6 hours after the cutover.

        It looks once every 5 minutes, as it opens the database, and asks once for each newest heartbeat, only while no
        backup has been taken since it last asked. A backup taken since that still ends at GitHub's last isn't followed
        by another, so a copy that somehow misses the check can't become a stream that rotates the 8 kept ones out. A
        backup that failed made nothing, so it's asked for again once there's a newer check."""
        if now < self._catch_up_at:
            return False
        self._catch_up_at = now + self.t.backup_retry
        try:
            cron = float(json.loads(self.seed_file.read_text(encoding="utf-8"))["last_cron_at"])
            ended = json.loads((self.backups / "latest" / MANIFEST_NAME).read_text(encoding="utf-8"))
            ended = None if ended["newest_heartbeat_at"] is None else float(ended["newest_heartbeat_at"])
        except (OSError, ValueError, KeyError, TypeError):  # No marker time or backup to go by: the other rules do.
            return False
        if not math.isfinite(cron) or (ended is not None and ended > cron):
            return False
        stamp = self._latest_name()
        if self._caught_up is not None and self._caught_up[0] != stamp:
            return False  # A backup was taken since it asked, and it still ends at GitHub's last: once is enough.
        newest = self._newest_heartbeat()
        if newest is None or newest <= cron or (self._caught_up is not None and newest == self._caught_up[1]):
            return False
        self._caught_up = (stamp, newest)
        self.log(f"a backup is due: the loop's checks on Render (the newest at {utc(newest)}) aren't in the latest "
                 f"backup, which ends at GitHub's last, and GitHub keeps only a backup that has them")
        return True

    def _newest_heartbeat(self) -> float | None:
        """MAX(ts) of the database's heartbeats, read as the gate reads its start record: plain read-only, never
        immutable. None when there's none, or it can't be read."""
        try:
            connection = sqlite3.connect(f"{self.db.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
            try:
                newest = connection.execute("SELECT MAX(ts) FROM sys_heartbeats").fetchone()[0]
            finally:
                connection.close()
            return None if newest is None else float(newest)
        except (sqlite3.Error, TypeError, ValueError):
            return None

    def _last_backup_at(self) -> float | None:
        with self._lock:
            at = self.backup_info["at"]
        if at is None:  # None taken since this process started: after a restart, the newest on the disk counts.
            try:
                manifest = json.loads((self.backups / "latest" / MANIFEST_NAME).read_text(encoding="utf-8"))
                at, sha256 = float(manifest["taken_at"]), manifest["db"]["sha256"]
            except (OSError, ValueError, KeyError, TypeError):
                return None
            with self._lock:
                self.backup_info.update(at=at, stamp=self._latest_name(), sha256=sha256)
        return at

    def backup(self) -> bool:
        """Takes a backup now, as _copy says. True if it worked. A failure is recorded and tried again 5 minutes on."""
        if not self._acquire():
            return False
        now = self.clock()
        try:
            stamp, manifest = self._copy(now)
        except (OSError, sqlite3.Error, ValueError, NotBackedUp) as error:
            self._backup_failed_at = now
            with self._lock:
                self.backup_info["error"] = f"{utc(now)}: {error}"
            self.log(f"backup failed, and the last good one stays: {error}")
            return False
        finally:
            self._ops.release()
        self._backup_failed_at = None
        with self._lock:
            self.backup_info = {"at": manifest["taken_at"], "stamp": stamp, "sha256": manifest["db"]["sha256"],
                                "error": None}
        self._prune_backups()
        with suppress(OSError):
            self.backup_now.unlink()
        self.log(f"backup {stamp} taken: {manifest['db']['size']} bytes, newest heartbeat "
                 f"{utc(manifest['newest_heartbeat_at']) or 'none'}")
        return True

    def _copy(self, now: float) -> tuple[str, dict]:
        """A consistent copy of the database in backups/<STAMP>/, and backups/latest pointed at it. Returns (STAMP, its
        manifest).

        The database is read through a plain read-only connection, never immutable=1: immutable skips SQLite's locking,
        so a copy taken while the bot checkpoints could mix two moments. The backup API copies every page in one step,
        under one read lock, and the source is closed straight after, so no read is left open to hold up the bot's
        checkpoints. A database that something holds locked for over backup_lock_wait fails the backup instead
        (_give_up_if_locked). The copy is then taken out of WAL mode, so it's one self-contained file, and must pass
        integrity_check. It's all made in a staging folder on the same disk and renamed into place, so a failure leaves
        nothing behind."""
        self.backups.mkdir(exist_ok=True)
        for leftover in self.backups.glob(".tmp-*"):  # From a backup that a restart cut short.
            shutil.rmtree(leftover, ignore_errors=True)
        named = now
        while (self.backups / time.strftime(STAMP, time.gmtime(named))).exists():
            named += 1
        stamp = time.strftime(STAMP, time.gmtime(named))
        staging = self.backups / f".tmp-{stamp}"
        staging.mkdir()
        try:
            copy = staging / DB_NAME
            source = sqlite3.connect(f"{self.db.resolve().as_uri()}?mode=ro", uri=True,
                                     timeout=self.t.backup_lock_wait)
            try:
                target = sqlite3.connect(copy)
                try:
                    source.backup(target, pages=-1, progress=self._give_up_if_locked)
                    source.close()
                    mode = target.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                    check = [str(row[0]) for row in target.execute("PRAGMA integrity_check")]
                    tables = {row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                    start = newest = None
                    if "state" in tables:
                        start = target.execute("SELECT value FROM state WHERE key = 'sys_start'").fetchone()
                    if "sys_heartbeats" in tables:
                        newest = target.execute("SELECT MAX(ts) FROM sys_heartbeats").fetchone()[0]
                finally:
                    target.close()
            finally:
                source.close()
            if str(mode).lower() != "delete":
                raise NotBackedUp(f"the copy stayed in {mode} mode")
            if check != ["ok"]:
                raise NotBackedUp(f"the copy fails integrity_check: {'; '.join(check[:3])}")
            if start is None:
                raise NotBackedUp("the copy holds no paper run: it has no start record")
            marker = json.loads(self.seed_file.read_text(encoding="utf-8"))
            if not isinstance(marker, dict) or marker.get("start_sha256") != start_sha256(start[0]):
                raise NotBackedUp(f"the copy's start record isn't the one {SEED_NAME} was written for")
            for suffix in ("-wal", "-shm"):  # Left by WAL mode, which the copy no longer uses.
                leftover = copy.with_name(copy.name + suffix)
                if suffix == "-wal" and leftover.exists() and leftover.stat().st_size:
                    raise NotBackedUp("the copy kept a WAL file with pages in it")
                with suppress(FileNotFoundError):
                    leftover.unlink()
            heartbeat = None
            with suppress(FileNotFoundError):
                beat = self.heartbeat_file.read_bytes()
                (staging / HEARTBEAT_NAME).write_bytes(beat)
                heartbeat = {"sha256": hashlib.sha256(beat).hexdigest(), "size": len(beat)}
            manifest = {"schema": 1, "taken_at": now, "taken_at_utc": utc(now),
                        "db": {"sha256": file_sha256(copy), "size": copy.stat().st_size}, "heartbeat": heartbeat,
                        "start_sha256": start_sha256(start[0]), "newest_heartbeat_at": newest, "integrity": "ok",
                        "seed": marker, "commit": self.env.get("RENDER_GIT_COMMIT") or None}
            (staging / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
            for path in staging.iterdir():
                fsync(path)
            fsync(staging)
            os.rename(staging, self.backups / stamp)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        fsync(self.backups)
        swap_link(self.backups / "latest", stamp)
        fsync(self.backups)
        return stamp, manifest

    def _give_up_if_locked(self, status: int, remaining: int, total: int) -> None:
        """The backup API's progress callback, after each step of the copy. A step comes back BUSY only once the
        source's busy timeout, backup_lock_wait, has run out, so the first already means the database was locked that
        long. CPython's backup() would then keep trying with no limit, holding _ops: no HOLD could stop the bot, no bot
        that exited would be started again and no page would be built while it lasted. So the first BUSY or LOCKED
        ends the backup, which is tried again 5 minutes on."""
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise NotBackedUp(f"the database stayed locked for over {self.t.backup_lock_wait:g} s, so no backup was "
                              f"taken: something holds it locked, a sqlite3 without -readonly in the Shell, say "
                              f"(RUNBOOK 13.10)")

    def _prune_backups(self) -> None:
        """Keeps the newest 8 backups, and always the one latest points to, should the clock ever have gone back."""
        keep = self._latest_name()
        with suppress(OSError):
            stamps = sorted(path for path in self.backups.iterdir()
                            if STAMP_NAME.fullmatch(path.name) and path.is_dir() and not path.is_symlink())
            for path in stamps[:-self.t.keep_backups]:
                if path.name != keep:
                    shutil.rmtree(path, ignore_errors=True)

    def _latest_name(self) -> str | None:
        with suppress(OSError):
            return Path(os.readlink(self.backups / "latest")).name
        return None

    # ---- What it says: /supervisor.json, /ok and the files it serves ----

    def _set_state(self, state: str, reason: str = "") -> None:
        """Publishes the state and why, and logs a change. A new state asks for a page build 10 s on, so the page shows
        it: seeded, held, unheld."""
        with self._lock:
            if (state, reason) == (self.state, self.reason):
                return
            if state != self.state:
                self._build_wanted = self.clock() + self.t.debounce
            self.state, self.reason = state, reason
        self.log(f"{state}: {reason}" if reason else state)

    def status(self) -> dict:
        """/supervisor.json, made when asked. Every field is named here: never the environment, which holds Render's
        own settings."""
        now, child = self.clock(), self.child
        pid = child.process.pid if child else None
        with self._lock:
            status = {
                "schema": 1, "now": now, "state": self.state, "reason": self.reason, "hold": False,
                "child": {"pid": pid, "started_at": child.started_at if child else None, "restarts": self.restarts,
                          "retry_at": self.retry_at, "last_exit": self.last_exit, "vmrss_kb": None,
                          "tail": list(self.output)[-20:]},
                "page": dict(self.page_info), "watchdog": dict(self.watchdog_info), "backup": dict(self.backup_info),
                "probe": dict(self.probe_info), "data_dir": None, "seed": None,
                # sqlite_version: SQLite builds differ in whether they open the run's database read-only as the seed
                # places it, with no -wal or -shm (ctl.py's self-test).
                "supervisor": {"pid": os.getpid(), "ppid": os.getppid(), "pgid": os.getpgid(0), "proc1_cmdline": None,
                               "started_at": self.started_at, "python": platform.python_version(),
                               "sqlite_version": sqlite3.sqlite_version,
                               "commit": self.env.get("RENDER_GIT_COMMIT") or None, "lock": self._lock_fd is not None},
                "log_tail": list(self.log_tail),
            }
        status["hold"] = self.data is not None and self.hold_file.exists()
        status["child"]["vmrss_kb"] = vmrss_kb(pid)
        status["data_dir"], status["seed"] = self._disk(), self._marker()
        status["supervisor"]["proc1_cmdline"] = proc1_cmdline()
        return status

    def _disk(self) -> dict:
        disk = {"path": self.data_dir_setting or None, "is_mount": False, "free_bytes": None, "total_bytes": None,
                "db_bytes": None}
        if self.data is not None:
            disk.update(path=str(self.data), is_mount=self._mounted())
            with suppress(OSError):
                stats = os.statvfs(self.data)
                disk.update(free_bytes=stats.f_bavail * stats.f_frsize, total_bytes=stats.f_blocks * stats.f_frsize)
            with suppress(OSError):
                disk["db_bytes"] = self.db.stat().st_size
        return disk

    def _marker(self) -> dict | None:
        """render_seed.json as the seed wrote it, or None."""
        if self.data is None:
            return None
        with suppress(OSError, ValueError):
            marker = json.loads(self.seed_file.read_text(encoding="utf-8"))
            return marker if isinstance(marker, dict) else None
        return None

    def ok(self) -> tuple[bool, str]:
        """/ok: (True, "ok") only while the bot runs, the page was built under 10 minutes ago and the watchdog passed
        under 10 minutes ago; otherwise (False, one line saying why). Worked out when asked, so a stuck maintenance
        thread shows as stale rather than as the last thing it found."""
        now = self.clock()
        with self._lock:
            state, reason, built = self.state, self.reason, self.page_info["built_at"]
            checked, passed, first = self.watchdog_info["at"], self.watchdog_info["ok"], self._watchdog_first
        if state == "held":
            return False, "held"
        if state != "running":
            return False, f"{state}: {reason}" if reason else state
        if built is None:
            return False, "page not built yet"
        if now - built >= self.t.fresh:
            return False, f"page not rebuilt for {(now - built) // 60:.0f} minutes"
        if checked is None:
            return False, "watchdog: not run yet"
        if not passed:
            return False, f"watchdog: {first or 'failed'}"
        if now - checked >= self.t.fresh:
            return False, f"watchdog: last ran {(now - checked) // 60:.0f} minutes ago"
        return True, "ok"

    def find(self, path: str) -> tuple[str, str] | bytes | None:
        """What a GET of path serves from the folders: (file, content type); the fallback page's bytes while no build
        has worked yet; or None, for a 404. No folder listings, no dotfiles (a build's half-written temp files are
        some), and nothing outside the served folders, links out of them included."""
        if path == "/":
            parts = ["index.html"]
        elif path.startswith("/"):
            parts = path[1:].split("/")
        else:
            return None
        if any(not part or part.startswith(".") or "\\" in part or "\0" in part for part in parts):
            return None
        if parts[0] == "state":
            if len(parts) != 2 or parts[1] not in STATE_FILES or self.data is None:
                return None
            folder, name, kind = self.backups / "latest", parts[1], STATE_FILES[parts[1]]
        elif len(parts) == 1:  # The page is one folder of files.
            folder, name = self.site / "current", parts[0]
            kind = CONTENT_TYPES.get(Path(name).suffix, "application/octet-stream")
        else:
            return None
        real = os.path.realpath(folder)
        if not os.path.isdir(real):
            return self.fallback() if folder.name == "current" and name == "index.html" else None
        file = os.path.realpath(os.path.join(real, name))
        if os.path.dirname(file) != real or not os.path.isfile(file):
            return None
        return file, kind

    def fallback(self) -> bytes:
        """The page before any build has worked: it says so, with the supervisor's state, and links supervisor.json."""
        with self._lock:
            said = f"{self.state}: {self.reason}" if self.reason else self.state
        return FALLBACK.format(html.escape(said, quote=False)).encode()


class Server(ThreadingHTTPServer):
    """A thread per connection, none of which holds up the exit, and at most http_connections of them at once: one more
    is closed at once, so visitors who stall can't pile up threads. No reverse DNS lookup of its own address as it
    binds, which can stall it, and no traceback on stderr for a request that fails, which would bury the supervisor's
    and the bot's lines in Render's Logs."""
    supervisor: Supervisor
    slots: threading.BoundedSemaphore  # One for each connection being handled.

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]

    def process_request(self, request, client_address) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)  # http_connections are being handled already: this one isn't answered.
            return
        try:
            super().process_request(request, client_address)
        except BaseException:  # Its thread didn't start, so it can't give the slot back.
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address) -> None:
        """A request that failed is one line in the log, where socketserver prints a traceback on stderr. A visitor who
        hung up, or let a read or write time out, says nothing."""
        error = sys.exc_info()[1]
        if not isinstance(error, (ConnectionError, TimeoutError)):
            self.supervisor.log(f"an HTTP request failed: {error!r}")


class Handler(BaseHTTPRequestHandler):
    """GET and HEAD. The base class answers any other method with 501."""
    server_version = "duo-supervisor"
    sys_version = ""

    def setup(self) -> None:
        # A timeout on each read and write: http.server closes a connection whose read or write times out.
        self.timeout = self.server.supervisor.t.http_timeout
        super().setup()

    def do_GET(self) -> None:
        self.answer(body=True)

    def do_HEAD(self) -> None:
        self.answer(body=False)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")  # On every answer: each is only true for now.
        super().end_headers()

    def log_message(self, format, *args) -> None:
        pass  # A line per request, a monitor's /ok every minute among them, would bury the bot's lines in the log.

    def answer(self, body: bool) -> None:
        supervisor: Supervisor = self.server.supervisor
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        try:
            if path == "/healthz":
                self.reply(200, TEXT, b"alive\n", body)
            elif path in ("/ok", "/ok.txt"):
                ok, said = supervisor.ok()
                self.reply(200 if ok else 503, TEXT, f"{said}\n".encode(), body)
            elif path == "/supervisor.json":
                self.reply(200, "application/json", json.dumps(supervisor.status(), indent=1).encode(), body)
            else:
                found = supervisor.find(path)
                if found is None:
                    self.reply(404, TEXT, b"not found\n", body)
                elif isinstance(found, bytes):
                    self.reply(200, CONTENT_TYPES[".html"], found, body)
                else:
                    self.send_file(*found, body)
        except (ConnectionError, TimeoutError):
            pass  # The visitor hung up, or stopped reading.

    def reply(self, code: int, kind: str, data: bytes, body: bool) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)

    def send_file(self, path: str, kind: str, body: bool) -> None:
        try:
            source = open(path, "rb")
        except OSError:  # Pruned between the look and now.
            self.reply(404, TEXT, b"not found\n", body)
            return
        with source:
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(os.fstat(source.fileno()).st_size))
            self.end_headers()
            if body:
                shutil.copyfileobj(source, self.wfile)


def main() -> None:
    try:
        port = int(os.environ.get("PORT") or PORT)
    except ValueError:  # Render sets it; anything else gets Render's default.
        port = PORT
    site = Path(os.environ.get("DUO_SITE_DIR") or SITE_DIR)
    sys.exit(Supervisor(os.environ.get("DUO_DATA_DIR"), site_dir=site, port=port).run())


if __name__ == "__main__":
    main()
