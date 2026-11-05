"""The only way research code reads market data, and the code that keeps the holdout locked.

Each panel is stored as two compressed numpy files, split at the holdout boundary:

    data/research/dev/<name>.npz      rows before 2024-07-17
    data/research/sealed/<name>.npz   rows from 2024-07-17 to 2026-10-31

load_panel(name) reads only the dev file. The sealed file needs a key from unlock(), and unlock()
writes a line to research/artifacts/holdout_ledger.jsonl before it returns one. That line holds a
hash of a secret only the key has, so reading the ledger gives nobody a key, and a look needs an
unlock that's on the record. A key can be handed to a worker process, but it isn't kept anywhere.

RESEARCH_ROOT stands in for the project root, for tests. It moves the data and the ledger together,
so a key from a scratch ledger can't open the real sealed files, and a sealed file reached through a
link out of the root is refused for that reason. So is one under the project's data folder reached
from a root that holds the project, because that link stays inside the root. PREREGISTRATION.md
doesn't move.

None of this stops code that opens a sealed file with np.load, or a copy or a hard link of one, which
no path can tell from another file. It's there so that what goes through the loader is on the record.

Each load also leaves a line in research/artifacts/panel_reads.jsonl, because the list in _panels_read
is one per process, and a worker's reads would otherwise never reach the process that recorded the
run. And it keeps the hash of each file it read, for the registry to compare with the file later.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def _utc(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


START_TS = _utc(2017, 9, 1)
HOLDOUT_START_TS = _utc(2024, 7, 17)
END_TS = _utc(2026, 11, 1)  # The instant 2026-10-31 ends. Exclusive: a bar opening here is outside the period.

CORE_FIELDS = ("open", "high", "low", "close", "volume", "quote_volume")
_RESERVED = {"ts", "assets", "meta", "file", "allow_pickle"}  # Taken by the file format or by numpy's savez.
_PROJECT = Path(__file__).resolve().parents[2]
_PREREGISTRATION = _PROJECT / "research" / "PREREGISTRATION.md"
_panels_read: list[tuple[str, str, str]] = []  # (root, panel, split) for each load, for the registry to see what ran.
_files_seen: dict[str, str] = {}  # The hash of each panel or cost model file when this process first read it.


class HoldoutLocked(Exception):
    """Holdout data was asked for without a valid key."""


class HoldoutAlreadyUsed(Exception):
    """This spec has already seen the holdout."""


def _root() -> Path:
    return Path(os.environ.get("RESEARCH_ROOT") or _PROJECT)


def _panel_path(split: str, name: str) -> Path:
    # The name becomes a file name, and "../sealed/x" must not reach the sealed folder through the dev path.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError(f"bad panel name {name!r}")
    return _root() / "data" / "research" / split / f"{name}.npz"


def _ledger_path() -> Path:
    return _root() / "research" / "artifacts" / "holdout_ledger.jsonl"


def _reads_path() -> Path:
    return _root() / "research" / "artifacts" / "panel_reads.jsonl"


def _note_read(name: str, split: str) -> None:
    _panels_read.append((str(_root()), name, split))
    line = {"panel": name, "split": split, "pid": os.getpid(), "ppid": os.getppid(), "t": time.time()}
    _reads_path().parent.mkdir(parents=True, exist_ok=True)
    # One unbuffered write to a file opened for appending, so lines from several processes can't run together.
    with _reads_path().open("ab", buffering=0) as out:
        out.write(json.dumps(line, sort_keys=True).encode() + b"\n")


def _remember_file(path, content: bytes) -> None:
    """Keeps a file's hash the first time this process reads it: what it loaded from that file is that version."""
    _files_seen.setdefault(str(Path(path).resolve()), hashlib.sha256(content).hexdigest())


def _checked_ts(ts) -> np.ndarray:
    ts = np.asarray(ts)
    # Rounding a float could move a bar across the holdout boundary, so floats are refused.
    if ts.size and ts.dtype.kind not in "iu":
        raise ValueError(f"ts must be whole seconds, not {ts.dtype}")
    if ts.ndim != 1 or np.any(np.diff(ts.astype(np.int64)) <= 0):
        raise ValueError("ts must be one-dimensional and strictly increasing")
    return ts.astype(np.int64)


def _checked_block(values, shape: tuple[int, int], label: str) -> np.ndarray:
    block = np.asarray(values, dtype=np.float64)
    if block.shape != shape:
        raise ValueError(f"{label} has shape {block.shape}, expected {shape}")
    return block


def _read_only(array: np.ndarray) -> np.ndarray:
    # A view, so an array the caller passed in stays writable for the caller.
    view = array.view()
    view.flags.writeable = False
    return view


@dataclass(frozen=True, eq=False)  # eq=False because arrays have no usable == or hash.
class Panel:
    """T bars by A assets. Arrays are read-only, so one strategy can't edit the prices the next one sees."""

    name: str
    ts: np.ndarray  # Bar-open times in seconds, int64, strictly increasing.
    assets: tuple[str, ...]
    open: np.ndarray  # These six are T x A float64, NaN where an asset has no bar.
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    quote_volume: np.ndarray
    extras: dict = field(default_factory=dict)  # More named T x A arrays, such as funding.
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        ts = _checked_ts(self.ts)
        assets = tuple(self.assets)
        if len(set(assets)) != len(assets):
            raise ValueError(f"{self.name} has duplicate assets")
        shape = (ts.size, len(assets))
        object.__setattr__(self, "ts", _read_only(ts))
        object.__setattr__(self, "assets", assets)
        for key in CORE_FIELDS:
            object.__setattr__(self, key, _read_only(_checked_block(getattr(self, key), shape, key)))
        extras = {key: _read_only(_checked_block(value, shape, key)) for key, value in self.extras.items()}
        object.__setattr__(self, "extras", extras)


def _make_panel(name: str, ts, assets, fields: dict, meta: dict) -> Panel:
    extras = {key: value for key, value in fields.items() if key not in CORE_FIELDS}
    return Panel(name, ts, tuple(assets), *(fields[key] for key in CORE_FIELDS), extras=extras, meta=meta)


def _panel_fields(panel: Panel) -> dict:
    return {**{key: getattr(panel, key) for key in CORE_FIELDS}, **panel.extras}


def _name_order(assets: tuple[str, ...]) -> list[int]:
    """Column numbers in order of asset name."""
    return sorted(range(len(assets)), key=assets.__getitem__)


def _file_contents(panel: Panel, rows: np.ndarray, meta_json: str) -> dict:
    """One file's arrays: the given rows, and only the assets with data in those rows.

    Leaving the others out means the dev file doesn't reveal which assets listed during the holdout.
    """
    blocks = {key: values[rows] for key, values in _panel_fields(panel).items()}
    has_data = np.zeros(len(panel.assets), dtype=bool)
    for block in blocks.values():
        has_data |= ~np.isnan(block).all(axis=0)
    columns = [column for column in _name_order(panel.assets) if has_data[column]]
    return {
        "ts": panel.ts[rows],
        "assets": np.array([panel.assets[column] for column in columns], dtype=np.str_),
        "meta": np.array(meta_json, dtype=np.str_),
        **{key: block[:, columns] for key, block in blocks.items()},
    }


def _store(name: str, contents: dict) -> None:
    """Writes every file under a temp name and renames them last, so a failure leaves the old files whole."""
    temps = {}
    try:
        for split, arrays in contents.items():
            path = _panel_path(split, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            temps[path] = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            with temps[path].open("wb") as out:
                np.savez_compressed(out, allow_pickle=False, **arrays)
                out.flush()
                os.fsync(out.fileno())
        for path, temp in temps.items():
            os.replace(temp, path)
    finally:
        for temp in temps.values():
            temp.unlink(missing_ok=True)


def write_panel(name: str, ts, assets, fields: dict, meta: dict) -> dict:
    """Stores a panel as its dev and sealed files, and returns how many rows went where.

    fields maps names to T x A arrays. open, high, low, close, volume and quote_volume must all be
    there, or all be missing for a panel without prices, such as funding. Any other name is an extra.
    Rows outside [START_TS, END_TS) are dropped, and assets are stored in name order.
    meta must be JSON-safe. It goes in both files, so it must say nothing about holdout data.
    """
    for key in fields:
        if key in _RESERVED or not re.fullmatch(r"[A-Za-z0-9_]+", key):
            raise ValueError(f"bad field name {key!r}")
    given = [key for key in CORE_FIELDS if key in fields]
    if not given:
        fields = {**dict.fromkeys(CORE_FIELDS, np.full((len(ts), len(assets)), np.nan)), **fields}
    elif len(given) < len(CORE_FIELDS):
        raise ValueError(f"{name} has only {given} of {CORE_FIELDS}")
    panel = _make_panel(name, ts, assets, fields, {} if meta is None else meta)
    meta_json = json.dumps(panel.meta, sort_keys=True)
    for key, values in _panel_fields(panel).items():
        if np.isinf(values).any():
            raise ValueError(f"{name}: {key} has infinite values")

    ts = panel.ts
    # Binance archives stamp bars in milliseconds or microseconds. Dropping those as "past the end"
    # would lose real rows without a word.
    if ts.size and ts[-1] >= 10**11:
        raise ValueError(f"{name}: ts must be in seconds, but the last one is {ts[-1]}")
    in_period = (ts >= START_TS) & (ts < END_TS)
    if not in_period.any():
        raise ValueError(f"{name} has no rows inside the period")
    dev_rows = in_period & (ts < HOLDOUT_START_TS)
    sealed_rows = in_period & (ts >= HOLDOUT_START_TS)
    # A bar that opens before the boundary and closes after it would carry holdout prices into dev.
    if dev_rows.any() and ts.size > 1 and ts[dev_rows][-1] + np.diff(ts).min() > HOLDOUT_START_TS:
        raise ValueError(f"{name}: the last dev bar runs past the holdout boundary")

    # Both files are always rewritten, so a shorter rewrite can't leave old rows behind.
    _store(name, {
        "dev": _file_contents(panel, dev_rows, meta_json),
        "sealed": _file_contents(panel, sealed_rows, meta_json),
    })
    return {"dev_rows": int(dev_rows.sum()), "sealed_rows": int(sealed_rows.sum()), "dropped_rows": int((~in_period).sum())}


def _read_panel(split: str, name: str, low: int, high: int) -> Panel:
    """Loads one file. Timestamps are checked first, so a bad file's prices are never read."""
    path = _panel_path(split, name)
    content = path.read_bytes()
    _remember_file(path, content)
    with np.load(io.BytesIO(content), allow_pickle=False) as stored:
        ts = stored["ts"]
        if ts.size and (ts.min() < low or ts.max() >= high):
            # Raised rather than asserted, because python -O removes assert statements.
            raise AssertionError(f"{path} has rows outside [{low}, {high})")
        fields = {key: stored[key] for key in stored.files if key not in _RESERVED}
        return _make_panel(name, ts, stored["assets"].tolist(), fields, json.loads(stored["meta"].item()))


def _join(dev: Panel, sealed: Panel) -> Panel:
    assets = tuple(sorted({*dev.assets, *sealed.assets}))
    rows = dev.ts.size + sealed.ts.size
    joined = {}
    for key in sorted(_panel_fields(dev).keys() | _panel_fields(sealed).keys()):
        block = np.full((rows, len(assets)), np.nan)
        for part, part_rows in ((dev, slice(0, dev.ts.size)), (sealed, slice(dev.ts.size, rows))):
            values = _panel_fields(part).get(key)
            if values is not None:
                # An asset missing from a part stays NaN for those rows, and columns are matched by name.
                block[part_rows][:, [assets.index(asset) for asset in part.assets]] = values
        joined[key] = block
    return _make_panel(dev.name, np.concatenate([dev.ts, sealed.ts]), assets, joined, dev.meta)


def load_panel(name: str, split: str = "dev", key: HoldoutKey | None = None) -> Panel:
    """Loads a panel. split="dev" touches only the dev file. split="all" needs a key from unlock()."""
    if split == "dev":
        _panel_path("dev", name)  # A bad name raises before anything is written.
        _note_read(name, split)
        return _read_panel("dev", name, 0, HOLDOUT_START_TS)
    if split != "all":
        raise ValueError(f"split must be 'dev' or 'all', not {split!r}")
    # Checked against the ledger at every use, so a key can't outlive its line or come from elsewhere.
    if not _key_opens(key):
        raise HoldoutLocked("holdout data needs a key from unlock()")
    # This root's ledger is the only one that has heard of this look, so it can only be of this root's files. A root
    # holding the project passes the first test through a link, and the project's own ledger would never hear of it.
    sealed, root = _panel_path("sealed", name).resolve(), _root().resolve()
    if not sealed.is_relative_to(root):
        raise HoldoutLocked(f"the sealed file of {name} is outside {_root()}, whose ledger gave this key")
    if root != _PROJECT.resolve() and sealed.is_relative_to((_PROJECT / "data").resolve()):
        raise HoldoutLocked(f"the sealed file of {name} is the project's, and {_root()} has its own ledger")
    _note_read(name, split)
    return _join(_read_panel("dev", name, 0, HOLDOUT_START_TS), _read_panel("sealed", name, HOLDOUT_START_TS, END_TS))


def _ledger_has(line_sha256, secret) -> bool:
    """Whether the ledger has this line, and the line was written for this secret."""
    try:
        lines = _ledger_path().read_bytes().splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        if hashlib.sha256(line).hexdigest() == line_sha256:
            return json.loads(line).get("key_sha256") == hashlib.sha256(str(secret).encode()).hexdigest()
    return False


def _key_opens(key) -> bool:
    return isinstance(key, HoldoutKey) and _ledger_has(getattr(key, "line_sha256", None), getattr(key, "secret", ""))


@dataclass(frozen=True)
class HoldoutKey:
    """Names one ledger line, and holds the secret that line has the hash of. Only unlock() ever knows both."""

    line_sha256: str
    secret: str = field(default="", repr=False)

    def __post_init__(self):
        if not _ledger_has(self.line_sha256, self.secret):
            raise HoldoutLocked("no ledger line matches this key, and only unlock() writes one")


def unlock(reason: str, spec_path, *, second_look: bool = False) -> HoldoutKey:
    """Records this use of the holdout in the ledger, then returns the key for load_panel(split="all").

    A spec sees the holdout once. Looking again needs second_look=True, and the ledger records that too.
    """
    if not str(reason).strip():
        raise ValueError("unlock needs a reason")
    spec = Path(spec_path).read_bytes()
    if not spec:
        raise ValueError(f"{spec_path} is empty, so it can't be a spec")
    spec_sha256 = hashlib.sha256(spec).hexdigest()
    secret = secrets.token_hex(32)
    line = json.dumps({
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": str(reason),
        "spec_path": str(spec_path),
        "spec_sha256": spec_sha256,
        "prereg_sha256": hashlib.sha256(_PREREGISTRATION.read_bytes()).hexdigest(),
        "second_look": second_look is True,
        "key_sha256": hashlib.sha256(secret.encode()).hexdigest(),
    }).encode()

    ledger = _ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a+b") as out:
        # Held across the check and the append, so two processes can't both be first.
        fcntl.flock(out, fcntl.LOCK_EX)
        out.seek(0)
        # A line that won't parse raises here: an unreadable ledger must not hand out keys.
        used = any(json.loads(old)["spec_sha256"] == spec_sha256 for old in out.read().splitlines())
        if used and second_look is not True:
            raise HoldoutAlreadyUsed(f"spec {spec_sha256} has already seen the holdout")
        out.write(line + b"\n")
        out.flush()
        os.fsync(out.fileno())
    return HoldoutKey(hashlib.sha256(line).hexdigest(), secret)


def top_n_mask(panel: Panel, n: int, window: int = 30, min_history: int = 90) -> np.ndarray:
    """The point-in-time universe: a T x A bool array, True for the top n assets at each bar.

    At bar t, assets are ranked by median quote_volume over the `window` bars ending at t, ignoring
    gaps. To be ranked an asset needs a bar at t (a non-NaN quote_volume) and at least `min_history`
    bars so far. Equal medians go to the earlier name.

    window and min_history count bars, so they are days only on a daily panel. mask[t] uses bar t's
    own volume, so it is known at the close of t and can only be traded from the open of t+1.

    Every asset in the panel gets ranked, so the assets section 3 of the preregistration excludes
    (stablecoins, fiat, leveraged and wrapped tokens, BNB) must be kept out of the panel passed in.
    """
    if n < 1 or window < 1:
        raise ValueError(f"n and window must be at least 1, got n={n}, window={window}")
    volumes = panel.quote_volume
    rows, columns = volumes.shape
    has_bar = ~np.isnan(volumes)
    bars_so_far = np.cumsum(has_bar, axis=0)
    name_rank = np.empty(columns, dtype=np.int64)
    name_rank[_name_order(panel.assets)] = np.arange(columns)
    mask = np.zeros((rows, columns), dtype=bool)
    for t in range(rows):
        eligible = np.flatnonzero(has_bar[t] & (bars_so_far[t] >= min_history))
        if eligible.size:
            # Each eligible asset has a bar at t, so no slice here is all NaN.
            medians = np.nanmedian(volumes[max(0, t - window + 1) : t + 1, eligible], axis=0)
            ranked = eligible[np.lexsort((name_rank[eligible], -medians))]
            mask[t, ranked[:n]] = True
    return mask
