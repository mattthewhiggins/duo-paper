"""python3 ops/render/ctl.py COMMAND: the paper bot's controls, typed in the Render service's Shell.

    cd /opt/render/project/src && python3 ops/render/ctl.py COMMAND

On Render the bot is one python -m duo run, a child of the supervisor (ops/render/serve.py), and its database is on the
service's disk. These commands are how you work it from the Shell (RUNBOOK 13):

    check      changes nothing beyond a write test and the SQLite self-test, which remove what they make: the Pythons,
               the disk, SQLite, config.toml, GitHub, the supervisor, the dashboard, the seed's marker and HOLD
    selftest   the SQLite self-test in this Python: does its SQLite open the run's database as the supervisor does
               (check and seed run it in the service's own Python, VENV_PY)
    status     python -m duo status
    probe      python -m duo probe: does this instance reach Kraken and Binance
    resume     python -m duo resume, which lifts a halt; it's built to run beside the loop (RUNBOOK 13.5)
    halt       stops trading at once, as the dashboard's Stop trading does; the checks carry on (RUNBOOK 13.5)
    flatten    sells everything at the next 5-minute loop, then stays halted; asks you to type FLATTEN, or --yes
    hold       stops the bot and keeps it stopped, for a deploy or a restart: the HOLD file (RUNBOOK 13.6). A HOLD
               the seed made becomes this hold's, so the bot stays stopped after the seed
    unhold     lets it start again, and waits up to 4 minutes for the loop's first check
    backup     asks the supervisor for a backup now, and prints its manifest (RUNBOOK 13.7)
    events [--limit N]
               the newest N events (20) in the bot's event log, oldest first, read-only: its warnings, and why a
               decision failed, which Render's Logs don't show (RUNBOOK 13.4)
    seed SHA --sha256 HEX [--allow-flatten]
               the cutover, once: puts paper-state's database at commit SHA on the disk, once the fence is on GitHub
               (RUNBOOK 13.3)

The data folder is DUO_DATA_DIR, or /var/data when that isn't set. Every command but check and selftest refuses unless
it's a mount point: the run lives only on the service's disk, and any other folder is the instance's own, which every
deploy wipes. duo's commands run as VENV_PY -m duo from the repository's root with DUO_DATA_DIR set to the data folder,
VENV_PY being the service's own Python: $VENV_ROOT/bin/python, else the repository's .venv/bin/python, else this one.
halt and flatten go to the bot's dashboard on 127.0.0.1:8787, which only this instance can reach, and the supervisor
answers on 127.0.0.1:$PORT.

The seed never writes over anything: a database, its -wal or -shm, a heartbeat or a marker already on the disk stops it,
and run again on the run it seeded, it says so and changes nothing. A SQLite self-test that fails, or a config.toml
without the systematic runner on, stops it too. Before it changes anything it also checks on GitHub that the fence is on
paper-state and the tag render-seed marks SHA (RUNBOOK 13.3 step 5): seeded from any other commit, the run would fork.
It downloads paper-state's files into DATA/.incoming, on the disk itself so that moving them is a rename, and checks the
database is this paper run, with GitHub's checks in it and nothing left mid-trade. Only then does it move them into
place under HOLD, the heartbeat first and the database second, and write render_seed.json, which the supervisor needs
before it starts the bot. Anything that goes wrong before that removes what the seed made and leaves the disk as it
was. Then it takes its HOLD away, unless ctl.py hold has taken it over, and waits up to 4 minutes for the loop's first
check.

Standard library only, for Python 3.11 or later, and it never imports duo, so Render's own python3 runs it too.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = "/var/data"  # The disk's mount path, when DUO_DATA_DIR isn't set (RUNBOOK 13.2).
PORT = "10000"  # Render's default, when PORT isn't set.
DASHBOARD = "http://127.0.0.1:8787"  # The bot's own ([dashboard] in config.toml): only this instance can reach it.
RAW = "https://raw.githubusercontent.com/mattthewhiggins/duo-paper"
# GitHub's branches and tags as they are now: git's ref advertisement, which git ls-remote reads. No login needed.
REFS = "https://github.com/mattthewhiggins/duo-paper.git/info/refs?service=git-upload-pack"
NOTE = "MOVED_TO_RENDER.md"  # The fence's note on paper-state, in place of the database (RUNBOOK 13.3 step 5).
TAG = "render-seed"  # Marks the commit the fence was pushed on, which is the one to seed from.

DB_NAME = "paper_systematic.db"
HEARTBEAT = "heartbeat_systematic.json"
MARKER = "render_seed.json"
HOLD = "HOLD"
BACKUP_NOW = "BACKUP_NOW"
INCOMING = ".incoming"
SEEDED = (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", HEARTBEAT, MARKER)  # The seed never writes over any of these.
SEED_HOLD = "Made by python3 ops/render/ctl.py seed"  # How the HOLD the seed makes starts.

POLL_SECONDS = 5
HOLD_SECONDS = 240  # The supervisor gives the bot 180 s to stop: a loop whose every request times out takes about 105.
# For the loop's first check after unhold or the seed. The bot checks as it starts, and the supervisor starts it within
# seconds, unless it has to wait for one that's stopping.
UNHOLD_SECONDS = 240
BACKUP_SECONDS = 180
START_SECONDS = 240
FETCH_TRIES = 3
FETCH_TIMEOUT = 30
FETCH_PAUSE = 10
SELFTEST_SECONDS = 60  # For the SQLite self-test in the service's own Python.

COMMIT = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
STAMP = re.compile(r"\d{8}T\d{6}Z")
PKT_LENGTH = re.compile(rb"[0-9a-fA-F]{4}")
REF = re.compile(rb"([0-9a-f]{40}) (\S+)")
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # Loopback requests never go through a proxy.


class Refused(Exception):
    """The seed can't go ahead. The message finishes the sentence 'Not seeded: ...'."""


def utc(ts: float | None) -> str | None:
    """A time as duo writes one: 2026-11-06 21:35:02 UTC."""
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def venv_python(environ, root: Path = ROOT) -> str:
    """The service's own Python, which its build installed duo's requirements into."""
    candidates = [Path(environ["VENV_ROOT"]) / "bin" / "python"] if environ.get("VENV_ROOT") else []
    for candidate in [*candidates, root / ".venv" / "bin" / "python"]:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def python_version(python: str) -> str:
    try:
        result = subprocess.run([python, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return f"couldn't run it: {error}"
    return (result.stdout or result.stderr).strip() or f"--version exited {result.returncode}"


def fetch_url(url: str) -> bytes:
    """One GET. An HTTP error raises urllib.error.HTTPError, so a 404 can be told from a failure."""
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
        return response.read()


def advertised_refs(body: bytes) -> dict[str, str]:
    """GitHub's branches and tags from its answer at REFS, as git ls-remote reads them: {name: commit}, an annotated
    tag's commit being under its name plus ^{}. ValueError when body isn't git's ref advertisement.

    That's a run of pkt-lines, each after its length in 4 hex digits that count themselves, 0000 being a flush:
    '# service=git-upload-pack' and a flush, then '<40 hex> <name>' for each ref, the first with the server's
    capabilities after a NUL, and a flush at the end."""
    refs, service, flushed, at = {}, False, False, 0
    while at < len(body):
        head = body[at:at + 4]
        if not PKT_LENGTH.fullmatch(head):
            raise ValueError(f"there's no pkt-line length at byte {at}")
        size = int(head, 16)
        flushed = size == 0
        if flushed:
            at += 4
            continue
        if size < 4 or at + size > len(body):
            raise ValueError(f"the pkt-line at byte {at} says it's {size} bytes long, and {len(body) - at} are left")
        line = body[at + 4:at + size].split(b"\0", 1)[0].removesuffix(b"\n")
        at += size
        if not service:
            if line != b"# service=git-upload-pack":
                break
            service = True
            continue
        match = REF.fullmatch(line)
        if not match:
            raise ValueError(f"{line[:80]!r} isn't a ref")
        refs[match[2].decode("utf-8", "replace")] = match[1].decode()
    if not service:
        raise ValueError("it doesn't start with git's '# service=git-upload-pack'")
    if not flushed:
        raise ValueError("it's cut short: it doesn't end with a flush")
    return refs


def run_command(command: list[str], cwd: Path, env: dict) -> int:
    """Runs command with its output going straight to this terminal, and returns its exit code."""
    return subprocess.run(command, cwd=cwd, env=env).returncode


def write_new(path: Path, data: bytes) -> None:
    """A new file holding data, never one written over, and on the disk before this returns."""
    with open(path, "xb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def sync_folder(path: Path) -> None:
    """Makes the renames in path last through a crash."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def time_key(state: dict, key: str) -> float | None:
    """A state key that holds a time, or None when it isn't set."""
    value = state.get(key)
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number):
        raise Refused(f"its {key} is {value!r}, which isn't a time")
    return number


def event_time(ts) -> str:
    """An event's time as UTC, or as it's stored when it isn't a time."""
    if isinstance(ts, (int, float)) and math.isfinite(ts):
        try:
            return utc(ts)
        except (OverflowError, OSError, ValueError):
            pass
    return str(ts)


def at_least_one(text: str) -> int:
    """--limit's value, a whole number from 1 up."""
    try:
        number = int(text)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"{text!r} isn't a whole number from 1 up")
    return number


def shown_time(value) -> str:
    """A state key's time as UTC, for reading: as it's stored if it isn't one."""
    try:
        return utc(time_key({"key": value}, "key")) or "not set"
    except Refused:
        return repr(value)


def sqlite_selftest(folder: Path, connect=sqlite3.connect) -> str | None:
    """Whether this Python's SQLite opens the run's database as the supervisor will: None if it does, else what it
    couldn't do. It's tried on a database of its own in folder, a new empty one, which it removes after.

    The seed places the database as GitHub saved it, a WAL database with every commit in the file and no -wal or -shm
    beside it, and the supervisor's gate and backups read it through a plain read-only open (file:...?mode=ro), never
    immutable. Some SQLite builds refuse that open, Apple's among them ("unable to open database file"), and on one of
    those the bot would never start (RUNBOOK 13.2)."""
    database = folder / "selftest.db"
    leftovers = [folder / f"{database.name}{suffix}" for suffix in ("-wal", "-shm")]
    doing = "couldn't make a WAL database"
    try:
        connection = connect(database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE selftest (said TEXT)")
            connection.execute("INSERT INTO selftest VALUES ('ok')")
            connection.commit()
        finally:
            connection.close()
        if any(path.exists() for path in leftovers):  # Some builds keep them after the last close; the seed has none.
            doing = "couldn't take its -wal and -shm away"
            connection = connect(database)
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
            for path in leftovers:
                path.unlink(missing_ok=True)
        if database.read_bytes()[18:20] != b"\x02\x02":  # The header's read and write versions: 2 is WAL.
            return "made a database that isn't in WAL mode, so the test shows nothing"
        doing = "couldn't open a WAL database with no -wal or -shm read-only, as the supervisor opens the run's"
        connection = connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        try:
            row = connection.execute("SELECT said FROM selftest").fetchone()
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as error:
        return f"{doing} ({type(error).__name__}: {error})"
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return None if row == ("ok",) else f"read {row!r} back, not the row it wrote"


def systematic_off(config: dict) -> str | None:
    """Why config.toml, parsed, doesn't run the systematic runner, or None when it does, read as duo/config.py reads it
    (and as the supervisor's gate does): [systematic] enabled is false when it's missing, or the section is, and must be
    true or false. Anything but true, and python -m duo run starts the agents' paper engine on paper.db, or refuses to
    start: never this run."""
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


def describe(status: dict | None, error: str = "") -> str:
    """The supervisor's state in a few words."""
    if status is None:
        return f"the supervisor doesn't answer ({error})" if error else "the supervisor doesn't answer"
    state, reason = status.get("state") or "unknown", status.get("reason")
    return f"{state}: {reason}" if reason else state


def last_words(status: dict) -> list[str]:
    """How the bot last exited, from /supervisor.json: a line with its exit code, then its last 5 lines, indented.
    They're child.last_exit's, or the bot's latest output when no exit is recorded."""
    child = status.get("child") if isinstance(status.get("child"), dict) else {}
    last = child.get("last_exit") if isinstance(child.get("last_exit"), dict) else None
    tail = (last if last is not None else child).get("tail")
    lines = [f"    {line}" for line in tail[-5:]] if isinstance(tail, list) else []
    head = f"It exited with code {last.get('code')}" if last is not None else "How it exited isn't recorded"
    return [f"{head}, and its last lines were:" if lines else f"{head}, and it printed nothing.", *lines]


class Controls:
    """The commands. What they reach can be swapped for the tests: fetch (a URL to its bytes), is_mount, the
    supervisor's and the dashboard's URLs, the clock and sleep, run (a duo command), ask (what the owner types) and
    connect (sqlite3.connect, for the SQLite self-test)."""

    def __init__(self, environ=None, *, root: Path = ROOT, config: Path | None = None, fetch=fetch_url,
                 is_mount=os.path.ismount, supervisor: str | None = None, dashboard: str = DASHBOARD,
                 clock=time.time, sleep=time.sleep, run=run_command, ask=input, connect=sqlite3.connect):
        self.environ = dict(os.environ if environ is None else environ)
        self.root = Path(root)
        self.config = Path(config) if config else self.root / "config.toml"
        self.data = Path(self.environ.get("DUO_DATA_DIR") or DATA_DIR)
        self.python = venv_python(self.environ, self.root)
        self.supervisor = supervisor or f"http://127.0.0.1:{self.environ.get('PORT') or PORT}"
        self.dashboard = dashboard
        self.fetch, self.is_mount, self.run, self.ask, self.connect = fetch, is_mount, run, ask, connect
        self.clock, self.sleep = clock, sleep

    def command(self, args: argparse.Namespace) -> int:
        """Runs the parsed command. The exit code is 0 when it's done, 1 when it isn't (it says why), and 2 when it was
        refused before anything was done: the data folder isn't the disk, or the arguments can't be used."""
        if args.command not in ("check", "selftest"):
            problem = self.not_the_disk()
            if problem:
                print(f"{problem}: nothing was done. The run lives only on the service's disk: check DUO_DATA_DIR (it "
                      f"should be {DATA_DIR}) and the service's Disk settings (RUNBOOK 13.2). python3 "
                      f"ops/render/ctl.py check shows what this Shell sees.")
                return 2
        if args.command in ("status", "probe", "resume"):
            return self.duo(args.command)
        handlers = {"check": self.check, "selftest": self.selftest_command, "halt": self.halt,
                    "flatten": self.flatten, "hold": self.hold, "unhold": self.unhold, "backup": self.backup,
                    "events": self.events, "seed": self.seed}
        return handlers[args.command](args)

    def not_the_disk(self) -> str | None:
        """Why the data folder isn't the service's disk, or None when it is."""
        if not self.data.is_absolute():
            return f"DUO_DATA_DIR is {self.data}, which isn't an absolute path"
        if not self.is_mount(str(self.data)):
            return f"{self.data} isn't a mount point, so it isn't the service's disk"
        return None

    def duo(self, *args: str) -> int:
        """python -m duo ARGS with the service's Python, from the repository's root, on the disk."""
        return self.run([self.python, "-m", "duo", *args], self.root, {**self.environ, "DUO_DATA_DIR": str(self.data)})

    # ---- The SQLite self-test (sqlite_selftest) ----

    def selftest(self) -> tuple[bool, str]:
        """The SQLite self-test in the service's own Python, VENV_PY, which the supervisor runs in: the python3 that
        runs this may be another build, with another SQLite. It runs in this process when VENV_PY is this Python, and
        otherwise as VENV_PY -I ctl.py selftest, whose one line it reads. (passed, what it found)."""
        if os.path.realpath(self.python) == os.path.realpath(sys.executable):
            return self.selftest_here()
        command = [self.python, "-I", str(Path(__file__).resolve()), "selftest"]
        try:
            result = subprocess.run(command, cwd=self.root, env={**self.environ, "DUO_DATA_DIR": str(self.data)},
                                    capture_output=True, text=True, timeout=SELFTEST_SECONDS)
        except (OSError, subprocess.SubprocessError) as error:
            return False, f"it couldn't be run in {self.python} ({error})"
        lines = (result.stdout.strip() or result.stderr.strip()).splitlines()
        said = lines[-1] if lines else f"{self.python} said nothing, and exited {result.returncode}"
        return result.returncode == 0 and said.startswith("ok: "), said.removeprefix("ok: ").removeprefix("failed: ")

    def selftest_here(self) -> tuple[bool, str]:
        """The SQLite self-test in this Python, in a fresh folder on the disk, DATA/.selftest-<pid>, which it removes.
        When the data folder isn't a mount it can write to, it tries a temporary folder instead, and says so. (passed,
        what it found, starting with which SQLite and which Python)."""
        folder = None
        if self.not_the_disk() is None:
            folder = self.data / f".selftest-{os.getpid()}"
            shutil.rmtree(folder, ignore_errors=True)  # Left by a test that was cut off, in a process with this pid.
            try:
                folder.mkdir()
            except OSError:
                folder = None
        where = "" if folder else f"; tried in a temporary folder, as {self.data} isn't a mount that can be written to"
        what = f"SQLite {sqlite3.sqlite_version} ({sys.executable})"
        try:
            problem = sqlite_selftest(folder or Path(tempfile.mkdtemp(prefix=".selftest-")), self.connect)
        except OSError as error:
            problem = f"couldn't make a folder to try it in ({error})"
        if problem is None:
            return True, (f"{what} opened a WAL database with no -wal or -shm read-only, as the supervisor opens the "
                          f"run's{where}")
        return False, f"{what} {problem}{where}"

    def selftest_command(self, args=None) -> int:
        """One line, ok or failed and then what it found, which check and the seed read when it runs in VENV_PY."""
        passed, said = self.selftest_here()
        print(f"{'ok' if passed else 'failed'}: {said}")
        return 0 if passed else 1

    # ---- The supervisor ----

    def supervisor_status(self) -> tuple[dict | None, str]:
        """(/supervisor.json, or None when it didn't answer with one; why not)."""
        try:
            with LOCAL.open(f"{self.supervisor}/supervisor.json", timeout=5) as response:
                status = json.load(response)
        except (OSError, ValueError, http.client.HTTPException) as error:
            return None, f"{type(error).__name__}: {error}"
        return (status, "") if isinstance(status, dict) else (None, "its answer isn't a JSON object")

    def wait_for(self, done, seconds: int, what: str) -> tuple[bool, dict | None, str]:
        """Asks the supervisor every POLL_SECONDS until done(its status) or seconds have passed, printing each state it
        gives. Returns (whether done, its last status, why it didn't answer when it didn't)."""
        print(f"Waiting for {what}, for up to {seconds // 60} minutes...")
        deadline, shown = self.clock() + seconds, None
        while True:
            status, error = self.supervisor_status()
            if describe(status, error) != shown:
                shown = describe(status, error)
                print(f"  {shown}")
            if status is not None and done(status):
                return True, status, error
            if self.clock() >= deadline:
                return False, status, error
            self.sleep(POLL_SECONDS)

    def heartbeat_at(self) -> float | None:
        """When heartbeat_systematic.json on the disk says the loop last checked, or None when it's missing or can't be
        read."""
        try:
            ts = float(json.loads((self.data / HEARTBEAT).read_text())["ts"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return ts if math.isfinite(ts) else None

    def wait_for_check(self, seconds: int) -> tuple[str, str, list[str]]:
        """Waits up to seconds for the loop's first check from now: heartbeat_systematic.json on the disk with a ts at
        or after the wait began, while /supervisor.json, when it answers, says running. It asks the supervisor every
        POLL_SECONDS, printing each state it gives, and stops early once the bot has exited: the state is backoff, or
        child.restarts has risen above the first it gave. Returns ('checked', when; 'exited', the supervisor's state,
        and how the bot exited as lines to print; or 'late', why it hasn't checked)."""
        began = self.clock()
        print(f"Waiting for the loop's first check, for up to {seconds // 60} minutes...")
        deadline, shown, first = began + seconds, None, None
        while True:
            status, error = self.supervisor_status()
            if describe(status, error) != shown:
                shown = describe(status, error)
                print(f"  {shown}")
            beat = self.heartbeat_at()
            if status is not None:
                child = status.get("child") if isinstance(status.get("child"), dict) else {}
                restarts = child.get("restarts") if isinstance(child.get("restarts"), int) else None
                first = restarts if first is None else first
                if status.get("state") == "backoff" or (restarts is not None and restarts > first):
                    return "exited", describe(status), last_words(status)
            if beat is not None and beat >= began and (status is None or status.get("state") == "running"):
                return "checked", utc(beat), []
            if self.clock() >= deadline:
                break
            self.sleep(POLL_SECONDS)
        if status is None:
            why = (f"the supervisor doesn't answer from this Shell ({error}), so open the service's /supervisor.json "
                   f"in a browser to see what it's doing")
        elif status.get("state") == "running":
            newest = f"its newest is from {utc(beat)}" if beat is not None else "there's none on the disk"
            why = f"the supervisor says the bot is running, but {HEARTBEAT} has no check since {utc(began)} ({newest})"
        else:
            why = f"{describe(status)}. That's what the supervisor is waiting for"
        return "late", why, []

    def made_by(self, by: str) -> str:
        return f"Made by python3 ops/render/ctl.py {by}, {utc(self.clock())}.\n"

    def make(self, name: str, by: str) -> tuple[int, int] | None:
        """Creates DATA/name unless it's there already. Returns the new file's (st_dev, st_ino), which tells it from
        a file put in its place later, or None when it was there already."""
        try:
            with open(self.data / name, "x") as out:
                out.write(self.made_by(by))
                made = os.fstat(out.fileno())
        except FileExistsError:
            return None
        return made.st_dev, made.st_ino

    def is_file(self, name: str, made: tuple[int, int]) -> bool:
        """Whether DATA/name is still the file make() made: made is the (st_dev, st_ino) it returned."""
        try:
            found = os.stat(self.data / name)
        except OSError:
            return False
        return (found.st_dev, found.st_ino) == made

    def hold_text(self) -> str:
        """What HOLD says, or nothing when it isn't there or can't be read."""
        try:
            return (self.data / HOLD).read_text(errors="replace")
        except OSError:
            return ""

    def since(self, name: str) -> str:
        try:
            return utc((self.data / name).stat().st_mtime)
        except OSError:
            return "a moment ago"

    # ---- check ----

    def check(self, args=None) -> int:
        """Changes nothing beyond a write test and the SQLite self-test, which remove what they make: what this Shell
        sees and reaches, GitHub and the supervisor among them, for the checks before the cutover (RUNBOOK 13.3)."""
        problem = self.not_the_disk()
        rows = [("Working folder", os.getcwd()),
                ("This Python", f"{sys.executable} ({platform.python_version()})"),
                ("VENV_PY", f"{self.python} ({python_version(self.python)})"),
                ("DUO_DATA_DIR", self.environ.get("DUO_DATA_DIR") or f"not set, so {DATA_DIR}"),
                ("Data folder", str(self.data)),
                ("Mount point", "yes" if problem is None else f"no: {problem}")]
        writable = False
        if self.data.is_dir():
            usage = shutil.disk_usage(self.data)
            rows.append(("Free space", f"{usage.free / 1e6:,.0f} MB of {usage.total / 1e6:,.0f} MB"))
            try:
                fd, name = tempfile.mkstemp(dir=self.data, prefix=".ctl-check-")
                os.close(fd)
                os.unlink(name)
                writable = True
                rows.append(("Write test", "ok: made a file there and deleted it"))
            except OSError as error:
                rows.append(("Write test", f"failed: {error}"))
        else:
            rows.append(("Free space", "the folder doesn't exist"))
        tested, said = self.selftest()
        rows.append(("SQLite self-test", f"{'ok' if tested else 'failed'}: {said}"))
        try:
            strategy, quote = self.expected_run()
            rows.append(("config.toml", f"the systematic runner: {strategy} in {quote}"))
            off = None
        except Refused as error:
            rows.append(("config.toml", "not usable: see below"))
            off = str(error)
        github = self.github_problems()
        rows.append(("GitHub", f"doesn't answer from here: {'; '.join(github)}" if github else
                     f"answers from here ({REFS}, {RAW}/main/.python-version)"))
        status, error = self.supervisor_status()
        rows += [("Supervisor", f"{self.supervisor}/supervisor.json: {describe(status, error)}"),
                 ("Dashboard", self.dashboard_answers()),
                 ("Seed marker", self.marker_text()),
                 ("HOLD", f"there, since {self.since(HOLD)}" if (self.data / HOLD).exists() else "none")]
        width = max(len(name) for name, _ in rows)
        for name, value in rows:
            print(f"{name:<{width}}  {value}")
        ready = True
        if problem is not None or not writable:
            print("Not ready: the other commands refuse until the data folder is the service's disk and can be written "
                  "to (RUNBOOK 13.2).")
            ready = False
        if not tested:
            print("Not ready: the SQLite self-test failed, so the seed must not go ahead: the supervisor opens the "
                  "run's database the same way, and would never start the bot (RUNBOOK 13.2).")
            ready = False
        if off is not None:
            print(f"Not ready: {off}. The seed refuses it, and the supervisor won't start the bot.")
            ready = False
        if github and not (self.data / MARKER).exists():  # Once seeded, nothing here downloads from GitHub.
            print(f"Not ready: Render can't download from GitHub now ({'; '.join(github)}), and the seed needs it "
                  f"(RUNBOOK 13.2).")
            ready = False
        if status is None or status.get("schema") != 1:
            why = error if status is None else f"its answer has schema {status.get('schema')!r}, not 1"
            print(f"Not ready: this Shell can't reach the supervisor at {self.supervisor}/supervisor.json ({why}), so "
                  f"hold, unhold and backup can't confirm what they did, and halt and flatten can't reach the bot: "
                  f"don't seed (RUNBOOK 13.2).")
            ready = False
        else:
            folder = status.get("data_dir")
            theirs = folder.get("path") if isinstance(folder, dict) else None
            if not isinstance(theirs, str):
                print(f"Not ready: the supervisor's data folder is not given (/supervisor.json's data_dir.path is "
                      f"missing or null), but this Shell's is {self.data}: the service's Environment must have "
                      f"DUO_DATA_DIR={DATA_DIR}, and the supervisor's reason says what to do (RUNBOOK 13.2).")
                ready = False
            elif os.path.realpath(theirs) != os.path.realpath(self.data):
                print(f"Not ready: the supervisor's data folder is {theirs}, but this Shell's is {self.data}: set "
                      f"DUO_DATA_DIR={theirs} in the Shell, and check again (RUNBOOK 13.2).")
                ready = False
        if ready:
            print("The disk is mounted and can be written to, so the other commands can run (RUNBOOK 13).")
            return 0
        return 1

    def github_problems(self) -> list[str]:
        """What's wrong with the two GitHub addresses the seed downloads from, each '<address>: <problem>', or none
        when both answer. One GET each, with no second try, so check stays quick: a 404 is GitHub answering."""
        problems = []
        for url in (REFS, f"{RAW}/main/.python-version"):  # main has .python-version once the Render code is on it.
            try:
                self.fetch(url)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    problems.append(f"{url}: HTTP {error.code}")
            except (OSError, ValueError, http.client.HTTPException) as error:
                problems.append(f"{url}: {type(error).__name__}: {error}")
        return problems

    def dashboard_answers(self) -> str:
        # GET / only, a static page: /api/state would write to the database. It's read to the end, or the bot's server
        # would log the broken pipe.
        try:
            with LOCAL.open(f"{self.dashboard}/", timeout=5) as response:
                response.read()
                code = response.status
        except urllib.error.HTTPError as error:
            code = error.code
        except (OSError, http.client.HTTPException) as error:
            return f"nothing answers on {self.dashboard} ({type(error).__name__}): the bot isn't running here"
        return f"{self.dashboard} answers (HTTP {code}): the bot is running here"

    def marker_text(self) -> str:
        path = self.data / MARKER
        if not path.exists():
            return "none: not seeded yet (RUNBOOK 13.3)"
        try:
            return json.dumps(json.loads(path.read_text()))
        except (OSError, ValueError) as error:
            return f"there, but it can't be read: {error}"

    # ---- halt and flatten, through the bot's dashboard ----

    def halt(self, args=None) -> int:
        if not self.post("/api/halt"):
            return 1
        print("Halted: working orders are cancelled and no new ones are placed. The checks carry on, and it stays "
              "halted until python3 ops/render/ctl.py resume (RUNBOOK 13.5).")
        return 0

    def flatten(self, args) -> int:
        if not args.yes:
            print("This sells everything the paper run holds at the bot's next loop, within 5 minutes, and it then "
                  "stays halted until you resume it (RUNBOOK 13.5).")
            try:
                typed = self.ask("Type FLATTEN to go ahead, or anything else to stop: ")
            except EOFError:
                typed = ""
            if typed.strip() != "FLATTEN":
                print("Nothing was sent.")
                return 1
        if not self.post("/api/flatten"):
            return 1
        print("Asked: the bot sells everything at its next loop, within 5 minutes, and then stays halted. The page "
              "shows the sells. Resume with python3 ops/render/ctl.py resume when it should trade again (RUNBOOK "
              "13.5).")
        return 0

    def post(self, path: str) -> bool:
        """POSTs path to the bot's dashboard as its own buttons do, with the header it asks of every action. Says why
        when it wasn't done."""
        request = urllib.request.Request(f"{self.dashboard}{path}", data=b"", method="POST",
                                         headers={"X-Duo-Action": "1"})
        token = self.environ.get("DUO_DASHBOARD_TOKEN")
        if token:  # The dashboard asks for a password only when it has one.
            request.add_header("Authorization", "Basic " + base64.b64encode(f"duo:{token}".encode()).decode())
        try:
            with LOCAL.open(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            hint = " It wants a password: set DUO_DASHBOARD_TOKEN in this Shell." if error.code == 401 else ""
            print(f"The dashboard refused {path} (HTTP {error.code}), so nothing was done.{hint}")
            return False
        except (OSError, http.client.HTTPException) as error:
            print(f"Nothing answers on {self.dashboard} ({type(error).__name__}: {error}), so the bot isn't running in "
                  f"this instance and nothing was sent. python3 ops/render/ctl.py check shows the supervisor's state.")
            return False
        try:
            ok = json.loads(body).get("ok") is True
        except (ValueError, AttributeError):
            ok = False
        if not ok:
            print(f"The dashboard answered {path} with {body[:200]!r}, not ok. Check the page and the service's Logs.")
        return ok

    # ---- hold, unhold and backup, through files the supervisor watches ----

    def hold(self, args=None) -> int:
        if self.make(HOLD, "hold"):
            print(f"Made {self.data / HOLD}.")
        elif self.hold_text().startswith(SEED_HOLD):
            # The seed takes away its own HOLD when it's done, so this one takes it over: the seed leaves a HOLD that
            # isn't the file it made.
            try:
                self.take_hold("hold")
            except OSError as error:
                print(f"HOLD is the seed's, and it couldn't be made this hold's ({type(error).__name__}: {error}), so "
                      f"the seed takes it away when it's done. Run python3 ops/render/ctl.py hold again once the seed "
                      f"has finished (RUNBOOK 13.6).")
                return 1
            print("That HOLD was the seed's, which it takes away when it's done: it's this hold's now, so the bot "
                  "stays stopped after the seed until python3 ops/render/ctl.py unhold.")
        else:
            said = self.hold_text().strip()
            print(f"HOLD was already there, since {self.since(HOLD)}." + (f" It says: {said}" if said else ""))
        done, status, error = self.wait_for(lambda s: s.get("state") == "held", HOLD_SECONDS,
                                            "the supervisor to stop the bot")
        if done:
            print("Held: the bot is stopped, and stays stopped until python3 ops/render/ctl.py unhold. Deploy or "
                  "restart now if that's what this is for (RUNBOOK 13.6).")
            return 0
        print(f"HOLD is in place, but after {HOLD_SECONDS // 60} minutes the bot isn't held: "
              f"{describe(status, error)}. Look at /supervisor.json and the service's Logs before deploying "
              f"(RUNBOOK 13.6).")
        return 1

    def take_hold(self, by: str) -> None:
        """Makes HOLD this command's in one step, a new file renamed over it, so there's never a moment without one."""
        fd, temp = tempfile.mkstemp(dir=self.data, prefix=f".{HOLD}-")
        try:
            with os.fdopen(fd, "w") as out:
                out.write(self.made_by(by))
            os.replace(temp, self.data / HOLD)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def unhold(self, args=None) -> int:
        hold, began = self.data / HOLD, self.clock()
        if hold.exists():
            hold.unlink(missing_ok=True)
            print(f"Removed {hold}.")
        else:
            status, _ = self.supervisor_status()
            child = (status or {}).get("child")
            started = child.get("started_at") if isinstance(child, dict) else None
            if (status or {}).get("state") == "running" and isinstance(started, (int, float)) and started < began:
                print("There was no HOLD, and the bot is running.")
                return 0
            print("There was no HOLD.")
        outcome, said, lines = self.wait_for_check(UNHOLD_SECONDS)
        if outcome == "checked":
            print(f"Running: the loop checked at {said}. The page shows it within 5 minutes.")
            return 0
        if outcome == "exited":
            print(f"Not running: the bot exited at once: {said} (RUNBOOK 13.6). " + "\n".join(lines))
        else:
            print(f"Not running after {UNHOLD_SECONDS // 60} minutes: {said}. The service's Logs on Render say more "
                  f"(RUNBOOK 13.6).")
        return 1

    def backup(self, args=None) -> int:
        if not (self.data / DB_NAME).exists() or not (self.data / MARKER).exists():
            print("Nothing to back up yet: the disk has no seeded database (RUNBOOK 13.3). Nothing was done.")
            return 1
        before, _ = self.supervisor_status()
        last = (before or {}).get("backup") or {}
        if self.make(BACKUP_NOW, "backup"):
            print("Asked the supervisor for a backup now (BACKUP_NOW).")
        else:
            print("A backup was already asked for: BACKUP_NOW is there.")

        def new(status: dict) -> bool:
            backup = status.get("backup") or {}
            return any(backup.get(key) not in (None, last.get(key)) for key in ("at", "error"))

        done, status, error = self.wait_for(new, BACKUP_SECONDS, "the supervisor's backup")
        backup = (status or {}).get("backup") or {}
        failed = backup.get("error") not in (None, last.get("error"))  # An error from before isn't this backup's.
        if not done or failed:
            what = (f"The backup failed: {backup['error']}" if failed else
                    f"No new backup after {BACKUP_SECONDS // 60} minutes: {describe(status, error)}")
            then = ("BACKUP_NOW is still there, so the supervisor takes one when it can"
                    if (self.data / BACKUP_NOW).exists() else "BACKUP_NOW is gone")
            print(f"{what}. {then}; its Logs and /supervisor.json say more (RUNBOOK 13.7).")
            return 1
        stamp = backup.get("stamp")
        folder = stamp if isinstance(stamp, str) and STAMP.fullmatch(stamp) else "latest"
        path = self.data / "backups" / folder / "manifest.json"
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            print(f"The supervisor took a backup ({stamp}), but its manifest couldn't be read: {error}")
            return 1
        print(f"Backed up: {path}")
        print(json.dumps(manifest, indent=2))
        return 0

    # ---- events: the bot's event log, read-only ----

    def events(self, args) -> int:
        """The newest args.limit rows of the run's events table, oldest first. The runner's warnings, and the traceback
        of a decision that failed, go only there: the bot prints its banner and little else, so Render's Logs can't
        say why a decision is overdue (RUNBOOK 13.4)."""
        path = self.data / DB_NAME
        if not path.exists():
            print(f"No paper run on the disk yet: {path} isn't there (RUNBOOK 13.3).")
            return 1
        try:
            # A plain read-only open, as the supervisor's: never immutable, which would miss what the -wal holds.
            connection = self.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
            try:
                kept = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'")
                rows = connection.execute("SELECT ts, level, kind, text FROM events ORDER BY rowid DESC LIMIT ?",
                                          (args.limit,)).fetchall() if kept.fetchone() else []
            finally:
                connection.close()
        except sqlite3.Error as error:
            print(f"The events couldn't be read from {path} ({type(error).__name__}: {error}).")
            return 1
        if not rows:
            print(f"{path} has no events yet.")
            return 0
        for ts, level, kind, text in reversed(rows):
            first, *rest = str(text).splitlines() or [""]
            print(f"{event_time(ts)}  {level}  {kind}  {first}")
            for line in rest:
                print(f"    {line}")
        return 0

    # ---- seed: the cutover, once (RUNBOOK 13.3) ----

    def seed(self, args) -> int:
        sha, digest = args.sha.lower(), args.sha256.lower()
        if not COMMIT.fullmatch(sha) or not SHA256.fullmatch(digest):
            print("Not seeded: SHA must be paper-state's commit as 40 hex digits (git ls-remote prints it), and "
                  "--sha256 its database's sha256 as 64 hex digits. Nothing was done.")
            return 2
        there = [name for name in SEEDED if os.path.lexists(self.data / name)]
        if there:
            return self.seeded_already(sha, digest, there)
        # Before HOLD or a download: on a SQLite that can't open the database as the seed places it, or a config.toml
        # without the systematic runner on, the supervisor would never start the bot.
        tested, said = self.selftest()
        if not tested:
            print(f"Not seeded: the SQLite self-test failed: {said}. The supervisor opens the run's database the same "
                  f"way, so it would never start the bot: the seed mustn't go ahead (RUNBOOK 13.2). Nothing was done.")
            return 1
        print(f"SQLite self-test: ok: {said}.")
        try:
            self.expected_run()
            self.check_fence(sha)
        except Refused as error:
            print(f"Not seeded: {error}. Nothing was done.")
            return 1
        incoming, made_hold, made_incoming = self.data / INCOMING, None, False
        try:
            # It takes this HOLD away only while it's still this file: ctl.py hold takes it over in one step.
            made_hold = self.make(HOLD, "seed")
            print("Made HOLD, so the bot can't start before the seed is done." if made_hold else
                  f"HOLD was already there, since {self.since(HOLD)}, and it stays.")
            try:
                incoming.mkdir()
            except FileExistsError:
                raise Refused(f"{incoming} is already there: another seed is running, or one was cut off. If none is "
                              f"running, remove it (rm -r {incoming}) and seed again") from None
            made_incoming = True
            facts = self.stage(sha, digest, incoming, args.allow_flatten)
            there = [name for name in SEEDED if os.path.lexists(self.data / name)]
            if there:  # Something may have written one while the seed downloaded.
                raise Refused(f"{', '.join(there)} appeared in {self.data} while the seed ran, and it never writes "
                              f"over anything")
        except (Exception, KeyboardInterrupt) as error:
            if made_incoming:
                shutil.rmtree(incoming, ignore_errors=True)
            if made_hold and self.is_file(HOLD, made_hold):
                (self.data / HOLD).unlink(missing_ok=True)
            reason = (str(error) if isinstance(error, Refused) else
                      "interrupted" if isinstance(error, KeyboardInterrupt) else f"{type(error).__name__}: {error}")
            print(f"Not seeded: {reason}. Nothing was placed, and everything the seed made is gone.")
            return 1

        now = self.clock()
        marker = {"schema": 1, "sha": sha, "db_sha256": digest, "seeded_at": now, "seeded_at_utc": utc(now), **facts}
        try:
            self.place(incoming, marker)
        except (Exception, KeyboardInterrupt) as error:
            placed = [name for name in (HEARTBEAT, DB_NAME, MARKER) if (self.data / name).exists()]
            why = "interrupted" if isinstance(error, KeyboardInterrupt) else f"{type(error).__name__}: {error}"
            print(f"The seed failed while moving its files into place ({why}). In {self.data} now: "
                  f"{', '.join(placed) or 'none of them'}. HOLD stays, so the bot can't start. Delete nothing "
                  f"(RUNBOOK 13.10): have the assistant look (RUNBOOK 13.3 step 6).")
            return 1
        print(f"Placed the files in {self.data} and wrote {MARKER}:")
        print(json.dumps(marker, indent=2))
        if not made_hold:
            print("HOLD was there before the seed, so it stays, and the bot stays stopped until python3 "
                  "ops/render/ctl.py unhold (RUNBOOK 13.6).")
            return 0
        if self.is_file(HOLD, made_hold):
            (self.data / HOLD).unlink(missing_ok=True)
            print("Removed the HOLD the seed made.")
        elif (self.data / HOLD).exists():
            print("HOLD was replaced while the seed ran (python3 ops/render/ctl.py hold), so it stays, and the bot "
                  "stays stopped until python3 ops/render/ctl.py unhold (RUNBOOK 13.6).")
            return 0
        try:
            outcome, said, lines = self.wait_for_check(START_SECONDS)
        except KeyboardInterrupt:
            print("\nStopped waiting. The seed is done (render_seed.json is there), and the supervisor starts the bot "
                  "by itself: /supervisor.json and the page show how it goes (RUNBOOK 13.3 step 7).")
            return 130
        if outcome == "checked":
            # The supervisor backs up as soon as the marker is there, before the loop's first check, and GitHub keeps
            # only a backup with a check from Render in it. The supervisor takes that one by itself once the loop has
            # checked, so the workflow waits for it.
            print(f"Seeded, and the bot is running on Render: the loop's first check was at {said}. Next (RUNBOOK "
                  f"13.3): watch the page until it says Running normally, and /ok until it answers 200. The supervisor "
                  f"backs up by itself within about 5 minutes of that first check (backup.at in /supervisor.json "
                  f"changes); only then run the Render backup workflow once by hand. python3 ops/render/ctl.py backup "
                  f"forces one sooner.")
            return 0
        if outcome == "exited":
            print(f"Seeded, but the bot exited at once: {said} (RUNBOOK 13.3 step 6, and 13.4 for the reasons). "
                  + "\n".join(lines))
        else:
            print(f"Seeded, but the loop hasn't checked after {START_SECONDS // 60} minutes: {said}. The service's "
                  f"Logs on Render say more (RUNBOOK 13.3 step 6, and 13.4 for the reasons).")
        return 1

    def seeded_already(self, sha: str, digest: str, there: list[str]) -> int:
        """The seed run on a disk that has some of a run already, there: it never writes over a run, never completes a
        seed that was cut off, and changes nothing. Says what's there: 0 when it's this seed's run, whole, else 1."""
        marker = None
        if MARKER in there:
            try:
                marker = json.loads((self.data / MARKER).read_text())
            except (OSError, ValueError):
                marker = None
            marker = marker if isinstance(marker, dict) else None
        if marker is not None and (marker.get("sha"), marker.get("db_sha256")) == (sha, digest) and DB_NAME in there:
            print(f"Already seeded from {sha} at {marker.get('seeded_at_utc')}: the run is on the disk, and the seed "
                  f"never runs twice. Nothing was changed.")
            status, error = self.supervisor_status()
            print(f"The supervisor's state: {describe(status, error)}.")
            if (self.data / HOLD).exists():
                print("The seed was cut off before it took its HOLD away: python3 ops/render/ctl.py unhold starts the "
                      "bot (RUNBOOK 13.3 step 7)." if self.hold_text().startswith(SEED_HOLD) else
                      "HOLD is there, so the bot stays stopped until python3 ops/render/ctl.py unhold (RUNBOOK 13.6).")
            return 0
        names = f"{', '.join(there)} {'is' if len(there) == 1 else 'are'}"
        if marker is not None and (marker.get("sha"), marker.get("db_sha256")) != (sha, digest):
            why = (f"the disk was already seeded from {marker.get('sha')} at {marker.get('seeded_at_utc')} ({MARKER}), "
                   f"and the seed never runs twice or writes over a run")
        elif MARKER not in there and set(there) & {DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm"}:
            why = f"{names} on the disk but {MARKER} isn't, so a seed was cut off after it placed the database"
        else:
            why = f"{names} already in {self.data}, and the seed never writes over anything"
        # The disk may hold the only copy of the run, and the cut-off seed is never completed by itself.
        print(f"Not seeded: {why}. Delete nothing (RUNBOOK 13.10): have the assistant look (RUNBOOK 13.3 step 6). "
              f"Nothing was changed.")
        return 1

    def check_fence(self, sha: str) -> None:
        """Refused unless GitHub shows the fence on paper-state and the tag render-seed marking sha (RUNBOOK 13.3 step
        5), read from its branches and tags as they are now, then from two files at paper-state's tip. The fence is
        pushed with a lease on sha, so it's there only if sha was the last state GitHub saved, and the tag says which
        commit that was. Seeded from an older commit, or before the fence, the run would fork: Render's would go on
        from one state, and any run GitHub saved after it from another."""
        step, refs, why = "(RUNBOOK 13.3 step 5)", {}, None
        try:
            body = self.download(REFS)
            if body is None:
                why = f"{REFS} answered 404"
            else:
                refs = advertised_refs(body)
        except Refused as error:
            why = str(error)
        except ValueError as error:
            why = f"{REFS} isn't git's list of them ({error})"
        if why is not None:
            raise Refused(f"GitHub's branches and tags couldn't be read: {why}. Without them the seed can't check that "
                          f"the fence is on paper-state and the tag {TAG} marks {sha}: try again in a few minutes "
                          f"{step}")
        tip = refs.get("refs/heads/paper-state")
        if tip is None:
            raise Refused(f"GitHub has no paper-state branch, so the fence can't be on it {step}")
        if tip == sha:
            raise Refused(f"paper-state's tip on GitHub is still {sha}, so the fence isn't pushed yet: push it, then "
                          f"seed {step}")
        try:
            has = {name: self.download(f"{RAW}/{tip}/data/{name}") is not None for name in (DB_NAME, NOTE)}
        except Refused as error:
            raise Refused(f"paper-state's tip on GitHub, {tip}, couldn't be checked: {error}. Try again in a few "
                          f"minutes {step}") from None
        if has[DB_NAME]:
            raise Refused(f"paper-state's tip on GitHub, {tip}, still has data/{DB_NAME}, so it isn't the fence, "
                          f"which removes it: a run saved after {sha}, or the fence isn't pushed yet {step}")
        if not has[NOTE]:
            raise Refused(f"paper-state's tip on GitHub, {tip}, has no data/{NOTE}, so it isn't the fence {step}")
        # An annotated tag's own line names the tag object, and the line after it, with ^{}, the commit it marks.
        tag = refs.get(f"refs/tags/{TAG}^{{}}") or refs.get(f"refs/tags/{TAG}")
        if tag is None:
            raise Refused(f"GitHub has no tag {TAG}, which marks the commit the fence was pushed on: push it at that "
                          f"commit {step}")
        if tag != sha:
            raise Refused(f"the tag {TAG} marks {tag}, not {sha}. It marks the commit the fence was pushed on, so seed "
                          f"from that one; or if the tag is what's wrong, move it to {sha} with git tag -f {TAG} "
                          f"{sha}, then git push -f origin refs/tags/{TAG} {step}")
        print(f"GitHub: the fence is on paper-state (its tip, {tip[:7]}, has data/{NOTE} and no database), and the tag "
              f"{TAG} marks {sha[:7]}.")

    def stage(self, sha: str, digest: str, incoming: Path, allow_flatten: bool) -> dict:
        """Downloads paper-state's files at sha into incoming and checks the database. Returns the marker's facts."""
        base = f"{RAW}/{sha}/data/"
        print(f"Downloading paper-state's files at {sha}...")
        database = self.download(base + DB_NAME)
        if database is None:
            raise Refused(f"there's no data/{DB_NAME} at {sha}. Seed from the commit the fence was pushed on, not the "
                          f"fence itself (RUNBOOK 13.3)")
        got = hashlib.sha256(database).hexdigest()
        if got != digest:
            raise Refused(f"the downloaded database's sha256 is {got}, not {digest}")
        # A -wal would hold commits the .db file alone doesn't. save_state.sh never leaves one, but this doesn't rely on
        # it. A -shm holds no data.
        if self.download(f"{base}{DB_NAME}-wal") is not None:
            raise Refused(f"paper-state has data/{DB_NAME}-wal at {sha}, so the database alone isn't the whole state")
        heartbeat = self.download(base + HEARTBEAT)
        if heartbeat is not None:
            try:
                beat = json.loads(heartbeat)
            except ValueError:
                beat = None
            if not isinstance(beat, dict):
                raise Refused(f"data/{HEARTBEAT} at {sha} isn't a JSON object")
        print(f"  {DB_NAME}: {len(database):,} bytes, with the sha256 given")
        print(f"  {HEARTBEAT}: " + (f"{len(heartbeat):,} bytes" if heartbeat is not None else "none at that commit"))
        write_new(incoming / DB_NAME, database)
        if heartbeat is not None:
            write_new(incoming / HEARTBEAT, heartbeat)
        return self.inspect(incoming / DB_NAME, allow_flatten)

    def download(self, url: str) -> bytes | None:
        """url's bytes, or None when it isn't there (404). Refused when FETCH_TRIES tries all fail."""
        problem = ""
        for attempt in range(1, FETCH_TRIES + 1):
            try:
                return self.fetch(url)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                problem = f"HTTP {error.code}"
            except (OSError, ValueError, http.client.HTTPException) as error:
                problem = f"{type(error).__name__}: {error}"
            if attempt < FETCH_TRIES:
                print(f"  {url}: {problem}. Trying again in {FETCH_PAUSE} s.")
                self.sleep(FETCH_PAUSE)
        raise Refused(f"{url} couldn't be downloaded in {FETCH_TRIES} tries ({problem})")

    def expected_run(self) -> tuple[str, str]:
        """(strategy, quote) that config.toml asks for. python -m duo run carries on only a run whose start record has
        both (prepare() in duo/systematic.py): the strategy is [systematic] strategy, and the quote is the book currency
        prepare() works out from the cost model, USD for every coin in it, which is [portfolio] quote here. Refused when
        config.toml doesn't run the systematic runner at all."""
        try:
            raw = tomllib.loads(self.config.read_text())
            off = systematic_off(raw)
            if off is None:
                return raw["systematic"]["strategy"].strip(), raw["portfolio"]["quote"].upper()
        except (OSError, KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError) as error:
            raise Refused(f"{self.config} couldn't be read for its strategy and quote "
                          f"({type(error).__name__}: {error})") from None
        raise Refused(off)

    def inspect(self, path: Path, allow_flatten: bool) -> dict:
        """Checks the staged copy is this paper run with nothing left mid-trade, prints what it carries over, and
        returns the marker's facts. Read with immutable=1: nothing else touches the staged file, and a plain read-only
        open would make a -wal and -shm beside it."""
        strategy, quote = self.expected_run()
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            problems = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            if problems != ["ok"]:
                raise Refused("the database fails SQLite's integrity check: " + "; ".join(map(str, problems[:3])))
            state = dict(connection.execute("SELECT key, value FROM state").fetchall())
            raw = state.get("sys_start")
            try:
                start = json.loads(raw) if raw else None
            except ValueError:
                start = None
            if not isinstance(start, dict):
                raise Refused("the database holds no paper run: its start record (state key sys_start) is missing or "
                              "isn't JSON")
            if (start.get("strategy_id"), start.get("quote")) != (strategy, quote):
                raise Refused(f"the database holds a run of {start.get('strategy_id')} in {start.get('quote')}, but "
                              f"config.toml asks for {strategy} in {quote}, so python -m duo run would refuse it")
            resting = connection.execute("SELECT COUNT(*) FROM sys_children WHERE status = 'open'").fetchone()[0]
            if resting:
                raise Refused(f"{resting} order(s) are resting on the book (sys_children open), and the first loop on "
                              f"Render would close them unfilled without judging them. Cut over when none are working "
                              f"(RUNBOOK 13.3)")
            working = connection.execute("SELECT COUNT(*) FROM sys_orders WHERE status = 'working'").fetchone()[0]
            if working:
                raise Refused(f"{working} order(s) are still working (sys_orders). Cut over once the day's orders are "
                              f"done (RUNBOOK 13.3)")
            if state.get("flatten_requested") == "1" and not allow_flatten:
                raise Refused("a sell-everything request is pending (flatten_requested), and the first loop on Render "
                              "would sell everything and halt. Seed with --allow-flatten if that's what you want")
            tick_at = time_key(state, "sys_tick_at")
            # GitHub's runs are the heartbeats up to the last tick's own, which has its time exactly (python -m duo tick
            # stores now as sys_tick_at, then beats at now). The page counts the move from here.
            last_cron_at, cron_runs = connection.execute(
                "SELECT MAX(ts), COUNT(*) FROM sys_heartbeats WHERE ts <= ?", (tick_at,)).fetchone()
            if last_cron_at is None:  # The marker would say GitHub never checked, and pull_backup.py would refuse it.
                raise Refused(f"the database has no check from GitHub at or before its last tick (sys_tick_at "
                              f"{utc(tick_at) or 'not set'}), so it isn't paper-state's run, and GitHub could never "
                              f"keep a backup of it (pull_backup.py needs where GitHub's checks ended)")
            newest = connection.execute("SELECT ts, status FROM sys_heartbeats ORDER BY ts DESC LIMIT 1").fetchone()
        except sqlite3.Error as error:
            raise Refused(f"the download couldn't be read as the run's database ({error})") from None
        finally:
            connection.close()
        kill, beat = state.get("sys_kill"), "none"
        if newest:
            beat = f"{utc(newest[0])}, {newest[1]}, {(self.clock() - newest[0]) / 60:.0f} minutes ago"
        degraded = shown_time(state.get("sys_degraded_since"))
        if state.get("sys_degraded_since"):
            degraded += ": the watchdog counts degraded time from then"
        rows = [("integrity_check", "ok"),
                ("start record", f"{strategy} in {quote}, nothing resting or working"),
                ("flatten_requested", "yes, allowed by --allow-flatten" if state.get("flatten_requested") == "1"
                 else "no"),
                ("sys_kill", f"1, halted: {state.get('sys_kill_reason')}" if kill == "1" else kill or "not set"),
                ("sys_waiting", state.get("sys_waiting") or "not set"),
                ("sys_degraded_since", degraded),
                ("sys_tick_at", utc(tick_at) or "not set"),
                ("newest heartbeat", beat)]
        print("The download is this paper run:")
        for name, value in rows:
            print(f"  {name:<18}  {value}")
        return {"sys_tick_at": tick_at, "last_cron_at": last_cron_at, "cron_runs": cron_runs,
                "start_sha256": hashlib.sha256(raw.encode("utf-8") if isinstance(raw, str) else raw).hexdigest()}

    def place(self, incoming: Path, marker: dict) -> None:
        """The heartbeat first, then the database, then the marker, each a rename within the disk. Once the database is
        there, nothing may replace a heartbeat the bot has written."""
        if (incoming / HEARTBEAT).exists():
            os.replace(incoming / HEARTBEAT, self.data / HEARTBEAT)
        os.replace(incoming / DB_NAME, self.data / DB_NAME)
        sync_folder(self.data)
        fd, temp = tempfile.mkstemp(dir=self.data, prefix=f".{MARKER}-")
        try:
            with os.fdopen(fd, "w") as out:
                out.write(json.dumps(marker, indent=2) + "\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, self.data / MARKER)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
        sync_folder(self.data)
        shutil.rmtree(incoming, ignore_errors=True)


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 ops/render/ctl.py",
                                     description="The paper bot's controls in the Render service's Shell (RUNBOOK 13).")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, text in (
        ("check", "changes nothing beyond a write test and the SQLite self-test: the Pythons, the disk, SQLite, "
                  "config.toml, GitHub, the supervisor, the dashboard, the seed's marker and HOLD"),
        ("selftest", "the SQLite self-test in this Python (check and seed run it in the service's own, VENV_PY)"),
        ("status", "python -m duo status"),
        ("probe", "python -m duo probe: does this instance reach Kraken and Binance"),
        ("resume", "python -m duo resume: lifts a halt"),
        ("halt", "stops trading at once; the checks carry on"),
        ("hold", "stops the bot and keeps it stopped (the HOLD file)"),
        ("unhold", "lets it start again, and waits for the loop's first check"),
        ("backup", "asks the supervisor for a backup now, and prints its manifest"),
    ):
        commands.add_parser(name, help=text, description=text)
    flatten = commands.add_parser("flatten", help="sells everything at the next 5-minute loop, then stays halted")
    flatten.add_argument("--yes", action="store_true", help="don't ask for FLATTEN to be typed first")
    text = ("the newest events in the bot's event log, oldest first, read-only: its warnings, and why a decision "
            "failed, which Render's Logs don't show")
    events = commands.add_parser("events", help=text, description=text)
    events.add_argument("--limit", metavar="N", type=at_least_one, default=20, help="how many, 20 if not given")
    text = ("the cutover, once: paper-state's database at SHA onto the disk. Before it changes anything it checks on "
            "GitHub that the fence is on paper-state and the tag render-seed marks SHA")
    seed = commands.add_parser("seed", help=text, description=text)
    seed.add_argument("sha", metavar="SHA", help="paper-state's commit to seed from, 40 hex digits")
    seed.add_argument("--sha256", metavar="HEX", required=True, help="its database's sha256, 64 hex digits")
    seed.add_argument("--allow-flatten", action="store_true",
                      help="seed even with a sell-everything request pending, which the first loop then carries out")
    return parser


def main(argv=None, controls: Controls | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return (controls or Controls()).command(args)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # So each line of a long wait shows at once, piped or not.
    sys.exit(main())
