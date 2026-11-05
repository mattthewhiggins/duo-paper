"""The record of every run on real data, and the only sanctioned way to make one.

run_variant saves a run's Result, appends one hash-chained line to research/artifacts/variants.jsonl, and answers a
repeat from that record without running it again. The line is what makes a run count, so the multiple-testing correction
reads from here. RESEARCH_ROOT moves all of it for tests, except PREREGISTRATION.md, as in data.py.

Where the rules leave a choice open, these are the readings taken:

- seq starts at 0. prev_sha256 is of the previous line's bytes without their newline, the way data.py hashes its ledger.
- The chain can't see its own last line changed or removed, because no later line is there to disagree, so dropping the
  worst runs from the end of the log would pass. Each append therefore also writes research/artifacts/variants.tip, with
  the line count and the last line's hash, and checking the chain includes it. A crash between the append and the tip
  leaves the tip one line behind, and that alone isn't a fault. So does a crash in the very first append, which leaves
  one line and no tip, but two or more lines with no tip means the tip was lost. By then the line's file is in place, so
  a line past the tip with no file wasn't written here. Neither the chain nor the tip stops a forger who rewrites them
  on purpose: they catch edits, losses and an old copy of the log put back on its own.
- The tip sits beside the log, so an old copy of both, or both removed, would pass them, and the trial count would fall
  with no sign. But each run also leaves a returns file. So two or more files that no line names count as lost lines,
  as a broken chain does, and one is what a crash between the file's rename and the append leaves. That leaves one lost
  run unseen, and an old copy of the whole artifacts folder can't be told from the real one without something outside
  it.
- A re-run replaces its file, because the path has no version in it. So only the latest line naming a path can match
  that path's file. An older line has lost its file, as if it didn't exist, and verify_chain doesn't call that a fault,
  or every re-run after a bug fix would break the chain. load_result refuses an older line rather than hand back another
  run's numbers. The runs behind a line only stay readable if each has its own variant_id, kind and split.
- The returns path is fixed, so two names a disk can't tell apart would share one file, and each run would wreck the
  other's record. Macs ignore case and how an accent is encoded, so a name differing from an earlier one only in those
  ways raises ValueError before fn runs. That holds on every disk, so a log doesn't change meaning when it's moved.
- run_variant checks the chain's links, and an edited returns file isn't a broken link: it's a cache miss, so the run is
  done again and gets a new line, as the cache rule says. verify_chain is what reports the edit, until that re-run.
- kind="holdout" with split="dev" raises HoldoutLocked: a line called holdout that held development days could be read
  as a holdout result.
- Section 11 says each strategy sees the holdout once, with no retries with changed settings. "Checked the way
  load_panel checks it" alone would let one key open anything any number of times, and the preregistration outranks
  that. So a holdout line settles its (family, variant_id) for good, and these raise HoldoutAlreadyUsed before fn, which
  is what reads the holdout, can run. Other params are refused whatever the key. Other code, another cost model or
  another preregistration are refused too, because a setting can live in any of them, unless the key's ledger line says
  second_look, which is unlock's way of putting a further look on the record. This replaces "a code change re-runs it"
  for holdout lines only. A changed data file does re-run on the same key, and so do the same settings after a lost
  file, because the same inputs should give the same bytes. But nothing checked that they do, and fn isn't in any hash,
  so a setting in a closure could change, or a run could name another panel, and the new numbers would replace the old.
  So those re-runs are held to the bytes of the latest holdout line, and other numbers need second_look as well. They
  can only be refused once fn has run. A retry under a new id can't be told from a neighbour, so it gets a line and no
  refusal.
- A holdout run that fails has still read the holdout, and a line is only written for one that finishes. So
  research/artifacts/holdout_looks.jsonl gets a record before fn runs, outside the chain because the specification's
  log has one line per finished run, and it holds the same settings. It stands in for a holdout line where none exists:
  other params are refused whatever the key, and anything else needs second_look, even the same settings, because a fn
  can raise on numbers it doesn't like. Because it isn't in the log, a log wiped or put back doesn't forget a look.
- A key opens load_panel for any code in the process, and a line called a development trial mustn't rest on sealed rows.
  So a split="dev" run that reads them through load_panel raises HoldoutLocked and leaves a record in that same file,
  and a process that has read them can't record a development run at all, because rows closed over are out of fn's
  sight. A worker's reads don't reach this process's list, so load_panel also writes each read to
  research/artifacts/panel_reads.jsonl. A sealed read there by any process while a development fn runs refuses that run,
  because a worker of a worker can't be told from a stranger, and one by this process or a worker of it since this
  process started refuses development runs as it would in this process. A holdout result is the holdout as much as its
  rows are, so a process that reads one with load_result, or gets one back from run_variant, is marked the same way.
  Reports read them and record no development runs, so that costs them nothing. This is per RESEARCH_ROOT. It can't see
  np.load on a sealed file, a number from a holdout line's metrics, or one passed on by another process through a file.
- panels is what data_sha256 covers, so a panel left out could change with no re-run. A fn reading a panel through
  load_panel that isn't named raises ValueError, from this process or a worker of it. One read before the run and
  closed over is out of its sight, so with no RESEARCH_ROOT set, which is a real run, naming no panels at all raises
  too. A scratch root may name none, as tests with made-up Results do.
- prereg.sha256 and cost_model.sha256, and cost_model_revx.sha256 if it exists, are what "frozen" means. If one exists
  under the root, its file must be its last recorded hash, or run_variant raises ValueError before fn. An amendment goes
  in by adding its hash to the record. A scratch root with no records has nothing to hold its files to, but on the real
  root, with no RESEARCH_ROOT, a missing record is the freeze switched off, so prereg.sha256 and cost_model.sha256 must
  exist, and so must cost_model_revx.sha256 once that model does.
- A record moved aside for one run and put back would leave a line priced on a file the record never held. So
  verify_chain and trial_table also hold each line's prereg_sha256 and cost_model_sha256 to the records. A line's cost
  hash is over both model files, so with both there and an older version on either record, a line from an older pair
  can't be worked out from the records, and those lines aren't checked.
- The hashes are of the files on disk, and a process runs the code it imported. So an edit while a long driver runs
  would put the old code's numbers on the record under the new code's hash, and every later process would get them from
  the cache. A module this process has loaded from the hashed folders must still be what it was when first seen, or
  run_variant raises ValueError before fn. A first sight can't tell when it was imported, so one edited since this
  module was imported is refused too. A new process is the way on. An answer from the record runs no code, so it isn't
  held up, and a file no module was loaded from can change as the cache rule says.
- The same goes for a cost model and a named panel, which a driver loads once and closes over. Each must still be what
  this process first saw, either when it loaded it with CostModel.load or load_panel or when run_variant first hashed
  it, or run_variant raises ValueError before fn. A model loaded from another path, or built from a dict, isn't seen.
- A development run whose fn has started and then fails, by raising, by returning something else, or by a Result that
  can't be stored, leaves a line in research/artifacts/failed_runs.jsonl, outside the chain as holdout_looks.jsonl is,
  because the log holds finished runs only. Section 6 says nothing runs off the record on real data, and a fn can raise
  on numbers it doesn't like. failed_runs() reads them, for a report to set beside trial_count, which doesn't include
  them. A process killed outright leaves nothing.
- cost_model_sha256 covers cost_model.json and cost_model_revx.json. run_variant isn't told which track a run is on, and
  a stale result is worse than a needless re-run. Files that don't exist are left out, so tests can use a CostModel
  dict.
- code_sha256 reads under RESEARCH_ROOT like everything else, and includes subfolders of the two folders, so a
  subpackage can't change a result without changing the hash. data_sha256 includes each file's name, and a named panel
  with no file raises FileNotFoundError, because data that can't be hashed can't be on the record.
- Anything in the hashes changing while fn runs raises ValueError and records nothing: there would be no telling which
  version the run used.
- Non-finite metrics, infinity as well as NaN, are written as null, because JSON has neither.
- The returned Result is always the stored one, so a first run and a cached run can't differ. info goes through JSON, so
  a tuple in it comes back as a list, except info["delistings"], whose entries come back as the tuples the engines
  write. The file also holds the Result's name, which the specification's list leaves out and a Result needs.
- fn runs outside the lock, because a long run mustn't hold up the others. So two processes can run the same thing at
  once. The second to finish finds the first's line, and returns that without adding its own.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import types
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import numpy as np

from research.harness import data
from research.harness.data import (
    HOLDOUT_START_TS,
    HoldoutAlreadyUsed,
    HoldoutKey,
    HoldoutLocked,
    _key_opens,
    _ledger_path,
    _panel_path,
    _root,
)
from research.harness.metrics import summarise
from research.harness.result import _DAILY, Result

KINDS = ("trial", "sensitivity", "baseline", "null", "holdout")
_SPLITS = ("dev", "all")
_NO_PREVIOUS = "0" * 64
_COST_MODELS = ("cost_model.json", "cost_model_revx.json")
_HASHES = ("prereg_sha256", "cost_model_sha256", "code_sha256", "data_sha256")
_SETTINGS = ("prereg_sha256", "cost_model_sha256", "code_sha256")  # What a holdout retry can change besides its params.
_LINE_FIELDS = (
    "seq", "prev_sha256", "utc", "family", "variant_id", "params", "kind", "split", *_HASHES, "returns_sha256",
    "metrics",
)
_A_HASH = re.compile(r"[0-9a-f]{64}")
_STARTED = time.time()  # About when this process started, because a driver imports this before it does much else.
_code_in_memory: dict[str, str] = {}  # The hash of each loaded module's file when this process first saw it.
_holdout_results_read: list[str] = []  # The root, each time this process has had a holdout Result in its hands.
_chain_memo: dict = {}  # The last log this process walked to the end without a fault: path, bytes, lines and hashes.


class RegistryBypass(Exception):
    """run_variant was called with the chain broken. A new line would chain onto the damage and hide it."""


def _artifacts() -> Path:
    return _root() / "research" / "artifacts"


def _log_path() -> Path:
    return _artifacts() / "variants.jsonl"


def _tip_path() -> Path:
    return _artifacts() / "variants.tip"


def _looks_path() -> Path:
    return _artifacts() / "holdout_looks.jsonl"


def _failed_path() -> Path:
    return _artifacts() / "failed_runs.jsonl"


def _returns_path(line: dict) -> Path:
    file_name = f"{line['variant_id']}__{line['kind']}__{line['split']}.npz"
    return _artifacts() / "returns" / str(line["family"]) / file_name  # An edited line might not hold a string.


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _checked_name(label: str, value) -> None:
    # These become a folder and a file name, and "../x" must not reach outside the returns folder.
    if not isinstance(value, str) or not value or value.startswith(".") or set(value) & set("/\\\0"):
        raise ValueError(f"bad {label} {value!r}")


def _plain(value):
    """json.dumps calls this for what it can't write: numpy's scalars and arrays are fine, and anything else isn't."""
    if isinstance(value, (np.generic, np.ndarray)):
        return value.tolist()
    raise TypeError(f"{type(value).__name__} can't go in the record: {value!r}")


def _finite(value):
    """The same structure with every NaN and infinity as None."""
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _code_sha256() -> str:
    research = _root() / "research"
    files = sorted([*(research / "harness").rglob("*.py"), *(research / "strategies").rglob("*.py")])
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _cost_model_sha256() -> str:
    digest = hashlib.sha256()
    for name in _COST_MODELS:
        path = _artifacts() / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _data_sha256(split: str, panels) -> str:
    digest = hashlib.sha256()
    for name in sorted(set(panels)):
        for part in ("dev", "sealed") if split == "all" else ("dev",):
            digest.update(f"{part}/{name}\n".encode())
            digest.update(_panel_path(part, name).read_bytes())
    return digest.hexdigest()


def _run_hashes(split: str, panels) -> dict:
    return {
        "prereg_sha256": _sha256(data._PREREGISTRATION.read_bytes()),
        "cost_model_sha256": _cost_model_sha256(),
        "code_sha256": _code_sha256(),
        "data_sha256": _data_sha256(split, panels),
    }


def _stored_bytes(result: Result) -> bytes:
    arrays = {
        "name": np.array(result.name, dtype=np.str_),
        "ts": result.ts,
        **{key: getattr(result, key) for key in _DAILY},
        "weights": result.weights,
        "assets": np.array(result.assets, dtype=np.str_),
        "info": np.array(json.dumps(result.info, sort_keys=True, default=_plain), dtype=np.str_),
    }
    buffer = io.BytesIO()
    np.savez_compressed(buffer, allow_pickle=False, **arrays)
    return buffer.getvalue()


def _stored_result(content: bytes) -> Result:
    with np.load(io.BytesIO(content), allow_pickle=False) as stored:
        info = json.loads(stored["info"].item())
        if "delistings" in info:
            info["delistings"] = [tuple(event) for event in info["delistings"]]
        return Result(
            stored["name"].item(), stored["ts"], *(stored[key] for key in _DAILY), stored["weights"],
            stored["assets"].tolist(), info,
        )


def _line_name(number: int) -> str:
    return f"variants.jsonl line {number} (seq {number - 1})"


class _Chain(NamedTuple):
    lines: list[dict]  # Every line up to the first bad one.
    last_sha256: str  # The hash of the last of those lines, or the zeros if there are none.
    problem: str | None  # What's wrong with the first bad line, naming it. None if the whole log is sound.


def _tip_problem(hashes: list[str], lines: list[dict]) -> str | None:
    """The check for what the chain can't see."""
    try:
        count, last = _tip_path().read_text().split()
        count = int(count)
    except FileNotFoundError:
        # Every append writes one, so only a crash in the very first can leave none, and then there's one line.
        if len(hashes) > 1:
            return f"{_line_name(2)} has no tip: {_tip_path().name} is gone"
        count, last = 0, None
    except ValueError:
        return f"{_tip_path().name} can't be read"
    if count > len(hashes):
        return f"{_line_name(len(hashes) + 1)} is gone: the tip counts {count} lines, and the log has {len(hashes)}"
    if count and hashes[count - 1] != last:
        return f"{_line_name(count)} was changed: its hash isn't the one on the tip"
    if len(hashes) > count + 1:
        return f"{_line_name(count + 2)} wasn't written by run_variant: the tip counts only {count} lines"
    if len(hashes) == count + 1 and not _returns_path(lines[-1]).exists():
        return f"{_line_name(count + 1)} wasn't written by run_variant: it's past the tip, and it has no returns file"
    return None


def _disk_form(name: str) -> str:
    """A name as a disk ignoring case and accent encoding sees it: Unicode's canonical caseless form."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


def _lost_lines_problem(lines: list[dict]) -> str | None:
    """Two or more returns files that no line names. See the top of this file."""
    folder = _artifacts() / "returns"
    if not folder.is_dir():
        return None
    named = {f"{line['family']}/{line['variant_id']}__{line['kind']}__{line['split']}" for line in lines}
    files = (
        f"{family.name}/{entry.name[:-4]}"
        for family in os.scandir(folder) if family.is_dir()
        for entry in os.scandir(family) if entry.name.endswith(".npz")
    )
    strays = [name for name in files if name not in named]
    if len(strays) >= 2:
        # Only now, because this runs on every read of the log, and it's a good deal slower than comparing as written.
        on_disk = {_disk_form(name) for name in named}
        strays = sorted(name for name in strays if _disk_form(name) not in on_disk)
    if len(strays) < 2:
        return None
    # With no log there's no lock, so another process's first lines can go in while this one is looking.
    if not lines and _log_path().exists() and _log_path().stat().st_size:
        return None
    return (
        f"{_line_name(len(lines) + 1)} is gone: {len(strays)} returns files have no line, such as {strays[0]}, so "
        f"lines were lost or an old copy of the log was put back"
    )


def _read_chain(raw: bytes) -> _Chain:
    """Walks the log's bytes from the top, and stops at the first line that breaks a link.

    Walking the same bytes always gives the same lines, so where the log starts with bytes this process has already
    walked to the end without a fault, only what follows them is walked. Any other log, edited, cut short or at another
    path, is walked from the top. The tip and the returns files are checked on every read, as before.
    """
    memo = _chain_memo
    if memo and memo["path"] == str(_log_path()) and raw.startswith(memo["raw"]):
        done, lines, hashes, previous = len(memo["raw"]), [*memo["lines"]], [*memo["hashes"]], memo["previous"]
    else:
        done, lines, hashes, previous = 0, [], [], _NO_PREVIOUS
    pieces = raw[done:].split(b"\n")
    torn = pieces.pop()  # A finished file ends in a newline, so this is empty.
    if torn:
        pieces.append(torn)
    for number, piece in enumerate(pieces, start=len(lines) + 1):
        try:
            line = json.loads(piece)
        except ValueError:
            line = None
        if not isinstance(line, dict) or not set(_LINE_FIELDS) <= line.keys():
            return _Chain(lines, previous, f"{_line_name(number)} isn't a whole record")
        if line["seq"] != number - 1:
            return _Chain(lines, previous, f"{_line_name(number)} says seq {line['seq']}: a line is missing or moved")
        if line["prev_sha256"] != previous:
            if number == 1:
                return _Chain(lines, previous, f"{_line_name(1)} doesn't start the chain from 64 zeros")
            return _Chain(lines, previous, (
                f"{_line_name(number - 1)} was changed: its hash isn't the prev_sha256 on line {number}"
            ))
        lines.append(line)
        previous = _sha256(piece)
        hashes.append(previous)
    if torn:
        # The torn piece parsed, or the walk would have stopped at it, so it's the last of lines.
        return _Chain(lines, previous, f"{_line_name(len(lines))} has no newline, so it was never finished")
    _chain_memo.update(path=str(_log_path()), raw=raw, lines=[*lines], hashes=[*hashes], previous=previous)
    return _Chain(lines, previous, _tip_problem(hashes, lines) or _lost_lines_problem(lines))


@contextmanager
def _log_held_still():
    """The log's bytes, under a shared lock, because a writer holds the exclusive one for its rename and its append."""
    try:
        log = _log_path().open("rb")
    except FileNotFoundError:
        yield b""
        return
    with log:
        fcntl.flock(log, fcntl.LOCK_SH)
        yield log.read()


def _params_text(params) -> str:
    # As text, because Python calls 1, 1.0 and True equal, and a run given one isn't always a run given another.
    return json.dumps(params, sort_keys=True)


def _same_run(line: dict, wanted: dict) -> bool:
    if any(line[key] != wanted[key] for key in ("family", "variant_id", "kind", "split", *_HASHES)):
        return False
    return _params_text(line["params"]) == _params_text(wanted["params"])


def _cached(lines: list[dict], wanted: dict) -> Result | None:
    """The stored Result of the latest line for this same run whose file still matches it."""
    for line in reversed(lines):
        if _same_run(line, wanted):
            try:
                content = _returns_path(line).read_bytes()
            except FileNotFoundError:
                continue
            # Hashed and read from one copy of the bytes, so a rename in between can't pair a hash with another file.
            if _sha256(content) == line["returns_sha256"]:
                return _stored_result(content)
    return None


def _name_on_disk(line: dict) -> str:
    return _disk_form(f"{line['family']}/{line['variant_id']}__{line['kind']}__{line['split']}")


def _says_second_look(key: HoldoutKey) -> bool:
    """Whether this key's ledger line says second_look, which is how unlock records a look after the first."""
    for raw in _ledger_path().read_bytes().splitlines():
        if _sha256(raw) == key.line_sha256:
            return json.loads(raw).get("second_look") is True
    return False


def _check_allowed(lines: list[dict], wanted: dict, key, returns_sha256=None) -> None:
    """What the record itself rules out. Run before fn, and again under the lock in case another run got in first.

    Under the lock the run's numbers are known too, so they're held to the latest holdout line's.
    """
    place = (wanted["family"], wanted["variant_id"])
    name = f"{wanted['family']}/{wanted['variant_id']}"
    name_on_disk = _name_on_disk(wanted)
    latest_look = None
    for line in lines:
        same_id = (line["family"], line["variant_id"]) == place
        if not same_id and _name_on_disk(line) == name_on_disk:
            raise ValueError(
                f"{name} would share a returns file with {line['family']}/{line['variant_id']}: many disks can't tell "
                f"those names apart, so give it a name that differs by more than case or accent encoding"
            )
        if same_id and line["kind"] == wanted["kind"] == "holdout":
            if _params_text(line["params"]) != _params_text(wanted["params"]):
                raise HoldoutAlreadyUsed(
                    f"{name} saw the holdout with params {line['params']}, and section 11 allows no retries with "
                    f"changed settings"
                )
            changed = [setting for setting in _SETTINGS if line[setting] != wanted[setting]]
            if changed and not _says_second_look(key):
                raise HoldoutAlreadyUsed(
                    f"{name} has seen the holdout, and {changed} changed since. Section 11 says each strategy sees it "
                    f"once, so looking again needs a key from unlock(second_look=True), which the ledger records"
                )
            latest_look = line
    other_numbers = latest_look and returns_sha256 and latest_look["returns_sha256"] != returns_sha256
    if other_numbers and not _says_second_look(key):
        raise HoldoutAlreadyUsed(
            f"{name} has seen the holdout, and the same settings have given other numbers. Section 11 says each "
            f"strategy sees it once, so these need a key from unlock(second_look=True), which the ledger records"
        )


def _write_look(looks, wanted: dict) -> None:
    line = {**wanted, "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    looks.write(json.dumps(line, sort_keys=True, default=_plain).encode() + b"\n")
    looks.flush()
    os.fsync(looks.fileno())


def _claim_look(lines: list[dict], wanted: dict, key) -> None:
    """Records a holdout run before fn, which is what reads the holdout, or raises if the record already rules it out.
    """
    name_on_disk = _name_on_disk(wanted)
    name = f"{wanted['family']}/{wanted['variant_id']}"
    finished = any(line["kind"] == "holdout" and _name_on_disk(line) == name_on_disk for line in lines)
    _looks_path().parent.mkdir(parents=True, exist_ok=True)
    with _looks_path().open("a+b") as looks:
        # Held across the check and the append, so two processes can't both be first. A line that won't parse raises.
        fcntl.flock(looks, fcntl.LOCK_EX)
        looks.seek(0)
        earlier = [look for look in map(json.loads, looks.read().splitlines()) if _name_on_disk(look) == name_on_disk]
        if any(_params_text(look["params"]) != _params_text(wanted["params"]) for look in earlier):
            raise HoldoutAlreadyUsed(
                f"{name} has looked at the holdout with other params, and section 11 allows no retries with changed "
                f"settings"
            )
        if earlier and not finished and not _says_second_look(key):
            raise HoldoutAlreadyUsed(
                f"{name} has looked at the holdout and left no result, and nothing can tell what that look saw, so "
                f"looking again needs a key from unlock(second_look=True), which the ledger records"
            )
        _write_look(looks, wanted)


def _note_sealed_rows_read(wanted: dict) -> None:
    _looks_path().parent.mkdir(parents=True, exist_ok=True)
    with _looks_path().open("ab") as looks:
        fcntl.flock(looks, fcntl.LOCK_EX)
        _write_look(looks, wanted)


def _panels_read_since(start: int) -> list[tuple[str, str]]:
    """(panel, split) for each load under this root after that many loads in this process."""
    root = str(_root())
    return [(name, split) for where, name, split in data._panels_read[start:] if where == root]


def _reads_size() -> int:
    try:
        return data._reads_path().stat().st_size
    except FileNotFoundError:
        return 0


def _reads_on_disk(start: int = 0) -> list[dict]:
    """Every load any process made under this root, from that byte of panel_reads.jsonl on."""
    try:
        with data._reads_path().open("rb") as reads:
            reads.seek(start)
            return [json.loads(line) for line in reads.read().splitlines()]
    except FileNotFoundError:
        return []


def _ours(read: dict) -> bool:
    """A load by this process or a worker it started, since it started. A pid can be reused, a start time can't."""
    return os.getpid() in (read["pid"], read["ppid"]) and read["t"] >= _STARTED


def _holdout_seen_since(reads_start: int, results_start: int, disk_start: int | None) -> bool:
    """Whether sealed rows or a holdout result have come into reach since these points: in this process, in a worker,
    or with disk_start None, by this process or its workers on disk at any time."""
    root = str(_root())
    if any(part == "all" for _, part in _panels_read_since(reads_start)):
        return True
    if root in _holdout_results_read[results_start:]:
        return True
    if disk_start is None:
        return any(read["split"] == "all" and _ours(read) for read in _reads_on_disk())
    return any(read["split"] == "all" for read in _reads_on_disk(disk_start))


def _note_holdout_result() -> None:
    _holdout_results_read.append(str(_root()))


def _check_files_in_memory(split: str, panels) -> None:
    """A cost model or named panel this process has seen must still be what it saw, or a line's hash isn't of what ran.
    """
    paths = [_artifacts() / name for name in _COST_MODELS]
    parts = ("dev", "sealed") if split == "all" else ("dev",)
    paths += [_panel_path(part, name) for name in sorted(set(panels)) for part in parts]
    for path in paths:
        try:
            digest = _sha256(path.read_bytes())
        except FileNotFoundError:
            digest = None
        where = str(path.resolve())
        first_seen = data._files_seen.get(where)
        if first_seen is None and digest is not None:
            data._files_seen[where] = digest
        elif first_seen is not None and first_seen != digest:
            raise ValueError(
                f"{path.name} changed after this process first saw it, so what it loaded isn't the file the line would "
                f"name. Start a new process"
            )


def _note_failed_run(wanted: dict, error: BaseException) -> None:
    line = {**wanted, "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "error": type(error).__name__}
    _failed_path().parent.mkdir(parents=True, exist_ok=True)
    with _failed_path().open("ab") as failed:
        fcntl.flock(failed, fcntl.LOCK_EX)
        failed.write(json.dumps(line, sort_keys=True, default=_plain).encode() + b"\n")
        failed.flush()
        os.fsync(failed.fileno())


def failed_runs() -> list[dict]:
    """Every development run whose fn started and that left no line. See the top of this file."""
    try:
        return [json.loads(line) for line in _failed_path().read_bytes().splitlines()]
    except FileNotFoundError:
        return []


def _check_code_in_memory() -> None:
    """A module this process has loaded must still be what's on disk, or a line's code hash isn't of the code that ran.
    """
    research = os.path.realpath(_root() / "research")
    folders = (os.path.join(research, "harness", ""), os.path.join(research, "strategies", ""))
    for module in list(sys.modules.values()):
        where = module.__dict__.get("__file__") if isinstance(module, types.ModuleType) else None
        if not isinstance(where, str) or not where.endswith(".py"):
            continue
        path = os.path.realpath(where)
        if not path.startswith(folders):
            continue
        file_name = os.path.basename(path)
        try:
            with open(path, "rb") as source:
                digest = _sha256(source.read())
            edited = os.stat(path).st_mtime
        except FileNotFoundError:
            raise ValueError(f"{file_name} has gone since this process imported it, so start a new one") from None
        first_seen = _code_in_memory.get(path)
        if first_seen is None and edited > _STARTED or first_seen not in (None, digest):
            raise ValueError(
                f"{file_name} changed after this process imported it, so the code that would run isn't the code the "
                f"line would name. Start a new process"
            )
        _code_in_memory[path] = digest


def _recorded_hashes(path: Path) -> list[str] | None:
    """The hashes in a freeze record, oldest first, or None if there's no such file. Its other lines are dates."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    return [word for word in (line.split()[0] for line in text.splitlines() if line.split()) if _A_HASH.fullmatch(word)]


def _check_frozen(prereg_sha256: str) -> None:
    """A file whose record says it's frozen must be the last version on that record. See the top of this file."""
    real = not os.environ.get("RESEARCH_ROOT")
    recorded = _recorded_hashes(_artifacts() / "prereg.sha256")
    if recorded is None and real:
        raise ValueError("prereg.sha256 is missing, so nothing says PREREGISTRATION.md is frozen")
    if recorded is not None and recorded[-1:] != [prereg_sha256]:
        raise ValueError(
            "PREREGISTRATION.md isn't the last version recorded in prereg.sha256, so it isn't frozen. An amendment "
            "goes in by adding its hash there"
        )
    for name in _COST_MODELS:
        record = _artifacts() / f"{Path(name).stem}.sha256"
        recorded = _recorded_hashes(record)
        path = _artifacts() / name
        if recorded is None and real and (path.exists() or name == _COST_MODELS[0]):
            raise ValueError(f"{record.name} is missing, so nothing says {name} is frozen")
        if recorded is not None:
            if recorded[-1:] != [_sha256(path.read_bytes()) if path.exists() else None]:
                raise ValueError(f"{name} isn't what {record.name} recorded, so it isn't the frozen file")


def _frozen_problem(lines: list[dict]) -> str | None:
    """The first line whose preregistration or cost model hash isn't on the records. See the top of this file."""
    prereg = _recorded_hashes(_artifacts() / "prereg.sha256")
    records = [_recorded_hashes(_artifacts() / f"{Path(name).stem}.sha256") for name in _COST_MODELS]
    files = [_artifacts() / name for name in _COST_MODELS]
    models = None
    both_with_history = all(path.exists() for path in files) and any(record and len(record) > 1 for record in records)
    if any(record is not None for record in records) and not both_with_history:
        # A hash of one file alone is that file's, and the pair as it stands now counts if each file is its record's.
        models = {digest for record in records if record for digest in record}
        current = [record is None or not path.exists() or _sha256(path.read_bytes()) == record[-1]
                   for record, path in zip(records, files)]
        if all(current):
            models.add(_cost_model_sha256())
    for number, line in enumerate(lines, start=1):
        if prereg is not None and line["prereg_sha256"] not in prereg:
            return f"{_line_name(number)}: its prereg_sha256 isn't on prereg.sha256, so it ran on an unfrozen file"
        if models is not None and line["cost_model_sha256"] not in models:
            return f"{_line_name(number)}: its cost_model_sha256 isn't any frozen cost model's on the records"
    return None


def _write_tip(count: int, last_sha256: str) -> None:
    path = _tip_path()
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temp.open("w") as out:
        out.write(f"{count} {last_sha256}\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def _record(wanted: dict, result: Result, key) -> Result:
    """Saves the file and appends the line under one lock, so a line and its file can't be from different runs."""
    content = _stored_bytes(result)
    metrics = _finite(summarise(result))
    path = _returns_path(wanted)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        with _log_path().open("a+b") as log:
            # Held across the read and the append, so two processes can't take the same seq.
            fcntl.flock(log, fcntl.LOCK_EX)
            log.seek(0)
            chain = _read_chain(log.read())
            if chain.problem:
                raise RegistryBypass(chain.problem)
            already = _cached(chain.lines, wanted)
            if already is not None:
                return already
            _check_allowed(chain.lines, wanted, key, _sha256(content))
            os.replace(temp, path)
            line = json.dumps({
                **wanted,
                "seq": len(chain.lines),
                "prev_sha256": chain.last_sha256,
                "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "returns_sha256": _sha256(content),
                "metrics": metrics,
            }, sort_keys=True, allow_nan=False, default=_plain).encode()
            log.write(line + b"\n")
            log.flush()
            os.fsync(log.fileno())
            _write_tip(len(chain.lines) + 1, _sha256(line))
    finally:
        temp.unlink(missing_ok=True)
    return _stored_result(content)


def _check_holdout_access(kind: str, split: str, key) -> None:
    if split == "all":
        # Checked against the ledger at every use, the way load_panel does it.
        if not _key_opens(key):
            raise HoldoutLocked("split='all' needs a key from unlock()")
        if kind != "holdout":
            raise HoldoutLocked(f"split='all' is only for kind='holdout', not {kind!r}")
    elif kind == "holdout":
        raise HoldoutLocked("kind='holdout' is only for split='all'")


def run_variant(family, variant_id, params, fn, *, kind="trial", split="dev", panels=(), key=None) -> Result:
    """Runs fn() on the record, or returns the stored Result if this same run is already on it."""
    _checked_name("family", family)
    _checked_name("variant_id", variant_id)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
    if split not in _SPLITS:
        raise ValueError(f"split must be one of {_SPLITS}, not {split!r}")
    _check_holdout_access(kind, split, key)
    if not panels and not os.environ.get("RESEARCH_ROOT"):
        raise ValueError(f"{variant_id}: a run on the real project must name the panels it rests on, for data_sha256")
    # A NaN parameter would never equal itself, so it could never be found again.
    params = json.loads(json.dumps(params, sort_keys=True, allow_nan=False, default=_plain))
    hashes = _run_hashes(split, panels)
    _check_frozen(hashes["prereg_sha256"])
    wanted = {"family": family, "variant_id": variant_id, "params": params, "kind": kind, "split": split, **hashes}

    with _log_held_still() as raw:
        chain = _read_chain(raw)
        if chain.problem:
            raise RegistryBypass(chain.problem)
        stored = _cached(chain.lines, wanted)
    if stored is not None:
        if split == "all":
            _note_holdout_result()
        return stored
    _check_allowed(chain.lines, wanted, key)
    _check_code_in_memory()
    _check_files_in_memory(split, panels)
    if split == "dev" and _holdout_seen_since(0, 0, None):
        raise HoldoutLocked(f"{variant_id}: this process has read the holdout, so it can't record a development run")
    if kind == "holdout":
        _claim_look(chain.lines, wanted, key)
    try:
        result = _run_and_record(fn, wanted, hashes, key, panels)
    except BaseException as error:
        if split == "dev":
            _note_failed_run(wanted, error)
        raise
    if split == "all":
        _note_holdout_result()
    return result


def _run_and_record(fn, wanted: dict, hashes: dict, key, panels) -> Result:
    """run_variant from the moment fn runs, so anything that fails from here is a run that happened."""
    variant_id, split = wanted["variant_id"], wanted["split"]
    reads_before, results_before, disk_before = len(data._panels_read), len(_holdout_results_read), _reads_size()
    try:
        result = fn()
    finally:
        looked = split == "dev" and _holdout_seen_since(reads_before, results_before, disk_before)
        if looked:
            _note_sealed_rows_read(wanted)
    if looked:
        raise HoldoutLocked(f"{variant_id}: split='dev', but fn read sealed rows or a holdout result")
    read = {name for name, _ in _panels_read_since(reads_before)}
    read |= {entry["panel"] for entry in _reads_on_disk(disk_before) if _ours(entry)}
    unnamed = sorted(read - set(panels))
    if unnamed:
        raise ValueError(f"{variant_id}: fn read {unnamed} without naming them in panels, so data_sha256 misses them")
    if not isinstance(result, Result):
        raise ValueError(f"{variant_id}: fn must return a Result, not {type(result).__name__}")
    if split == "dev" and result.ts.size and result.ts[-1] >= HOLDOUT_START_TS:
        raise ValueError(f"{variant_id}: split='dev', but it has a row at or after the holdout's start")
    if _run_hashes(split, panels) != hashes:
        raise ValueError(f"{variant_id}: the code, data, cost model or preregistration changed during the run")
    _check_code_in_memory()
    _check_files_in_memory(split, panels)
    return _record(wanted, result, key)


def verify_chain() -> None:
    """Raises ValueError naming the first bad line: a broken link, or a returns file that isn't what its line says."""
    with _log_held_still() as raw:
        chain = _read_chain(raw)
        owner = {_returns_path(line): number for number, line in enumerate(chain.lines, start=1)}
        for number, line in enumerate(chain.lines, start=1):
            path = _returns_path(line)
            # A file belongs to the latest line naming it, and a re-run's replacing it isn't damage to the older line.
            if owner[path] == number and path.exists() and _sha256(path.read_bytes()) != line["returns_sha256"]:
                raise ValueError(f"{_line_name(number)}: {path.name} isn't the file this line recorded")
    problem = chain.problem or _frozen_problem(chain.lines)
    if problem:
        raise ValueError(problem)


def trial_table() -> list[dict]:
    """The latest line for each distinct (family, variant_id) with kind "trial", in the order of those lines.

    A re-run after a bug fix replaces its older line here, and that line stays in the log. Section 11's plus or minus
    20% runs count as trials, so they must be logged with kind="trial" under their own variant_id, as
    research/STRATEGY_SPEC.md says. A broken log raises ValueError, because a lost line would be an undercount.
    """
    with _log_held_still() as raw:
        chain = _read_chain(raw)
    problem = chain.problem or _frozen_problem(chain.lines)
    if problem:
        raise ValueError(problem)
    latest = {}
    for line in chain.lines:
        if line["kind"] == "trial":
            place = (line["family"], line["variant_id"])
            latest.pop(place, None)  # So the table stays in the order of the lines it holds.
            latest[place] = line
    return list(latest.values())


def trial_count() -> int:
    return len(trial_table())


def load_result(line: dict) -> Result:
    """Reads a line's stored Result back. ValueError if the file there isn't the one the line recorded.

    A holdout line's Result marks this process as having read the holdout. See the top of this file.
    """
    if line.get("split") == "all" or line.get("kind") == "holdout":
        _note_holdout_result()
    path = _returns_path(line)
    content = path.read_bytes()
    if _sha256(content) != line["returns_sha256"]:
        raise ValueError(f"{path.name} isn't the file seq {line.get('seq')} recorded: edited, or replaced by a re-run")
    return _stored_result(content)
