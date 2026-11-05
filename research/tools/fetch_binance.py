"""Downloads Binance public spot klines and stores them through the sealed store.

    .venv/bin/python -I research/tools/fetch_binance.py

Only the public archive at data.binance.vision is used, and it needs no account. It still holds
assets that were later delisted, which is why this source was chosen (PREREGISTRATION.md section 3).

Panels written: spot_1d (every kept USDT pair, daily) and majors_15m (BTC and ETH, 15 minutes).
Raw zips are cached under data/research/raw/binance/ and never extracted, so a rerun downloads only
what is new. Everything downloaded is untrusted data: values must match a numeric pattern, and no
text from a file reaches the log or the manifest. The run reports counts and dates, never a price.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import math
import os
import random
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import numpy as np

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))  # -I takes the current folder off the import path.

from research.harness.data import CORE_FIELDS, END_TS, HOLDOUT_START_TS, START_TS, write_panel  # noqa: E402

LIST_HOST = "s3-ap-northeast-1.amazonaws.com"
BUCKET = "data.binance.vision"
FILE_HOST = "data.binance.vision"
WORKERS = 8
ATTEMPTS = 8  # With the backoff in fetch(), a request gets two to three minutes before it counts as failed.
GIVE_UP_AFTER = 40  # Requests that used up every attempt. By then the network is the problem, so stop waiting.
CHECKSUM_TRIES = 3
MAX_DOWNLOAD_BYTES = 64 * 2**20
MAX_CSV_BYTES = 64 * 2**20  # A real month of 15m bars is under 1 MB. Far more than that isn't a kline file.
REJECT_SAMPLES = 200
LONG_GAP_DAYS = 7
PANELS = {"spot_1d": ("1d", 86_400), "majors_15m": ("15m", 900)}
MAJORS = ("BTC", "ETH")
QUOTE = "USDT"

# Exact asset names, never patterns: JUP and SYRUP end in UP, AUDIO starts with AUD, and T + USDT reads
# like TUSD + T, yet all of them are real assets. "why" goes into the manifest.
EXCLUDED = {
    "bnb": {
        "why": "Preregistration section 3 excludes BNB by name.",
        "names": {"BNB"},
    },
    "fiat": {
        "why": "A government currency, not a crypto asset.",
        "names": {"AUD", "EUR", "GBP"},
    },
    "stablecoin": {
        "why": "Pegged to a fiat currency. UST and USTC stay here though the peg broke in 2022, because a peg is"
        " what they were.",
        "names": {
            "AEUR", "BFUSD", "BKRW", "BUSD", "DAI", "EURI", "FDUSD", "KGST", "PAX", "RLUSD", "RWUSD", "SUSD", "TUSD",
            "U", "USD1", "USDC", "USDE", "USDP", "USDS", "USDSB", "USDSOLD", "UST", "USTC", "XUSD",
        },
    },
    "wrapped_or_pegged": {
        "why": "Wraps or tracks another crypto asset 1:1 (BETH, BNSOL, WBETH, WBTC, WNXM), or is pegged to gold"
        " (PAXG, XAUT).",
        "names": {"BETH", "BNSOL", "PAXG", "WBETH", "WBTC", "WNXM", "XAUT"},
    },
    "tokenised_stock": {
        "why": "Binance bStocks: tokens backed 1:1 by a share or ETF, first listed in June 2026. Treated as wrapped"
        " or pegged tokens, because they track a share price and not a crypto market. The preregistration doesn't"
        " name them.",
        "names": {
            "AAOIB", "AAPLB", "ADBEB", "AGPUB", "ALABB", "AMATB", "AMCB", "AMDB", "AMZNB", "APPB", "ARMB", "ASMLB",
            "ASTSB", "AVGOB", "AXTIB", "BABAB", "BBB", "BEB", "BMNRB", "BNCB", "BOTB", "BRKBB", "BSPB", "CBRSB",
            "COHRB", "COINB", "CRCLB", "CRDOB", "CRMB", "CRWDB", "CRWVB", "CYPHB", "DELLB", "DJTB", "DRAMB", "EWYB",
            "FLNCB", "FWDIB", "GEVB", "GLWB", "GMEB", "GOOGLB", "GPROB", "GSB", "HIMSB", "HOODB", "HPEB", "IBMB",
            "INTCB", "INTWB", "IRENB", "JPMB", "KORUB", "KSTRB", "LITEB", "LLYB", "MDBB", "METAB", "MRNAB", "MRVLB",
            "MSFTB", "MSTRB", "MUB", "MUUB", "MVLLB", "NBISB", "NFLXB", "NOKB", "NVDAB", "ORCLB", "PDDB", "PENGB",
            "PLTRB", "PYPLB", "QCOMB", "QNTB", "QQQB", "RDDTB", "RKLBB", "SECZB", "SHAZB", "SKDDB", "SKHYB", "SKUUB",
            "SMCIB", "SMHB", "SNDKB", "SNXXB", "SOXLB", "SOXSB", "SPCXB", "SPYB", "SQQQB", "STXB", "TMFB", "TQQQB",
            "TSLAB", "TSMB", "USARB", "USDEB", "UVXYB", "VSTB", "WDCB", "WENB", "ZMB",
        },
    },
    "leveraged": {
        "why": "A leveraged token: Binance's BLVTs (UP, DOWN) or the older 3x tokens (BULL, BEAR). BULL and BEAR on"
        " their own are the 3x BTC tokens.",
        "names": {
            "1INCHDOWN", "1INCHUP", "AAVEDOWN", "AAVEUP", "ADADOWN", "ADAUP", "BCHDOWN", "BCHUP", "BEAR", "BNBBEAR",
            "BNBBULL", "BNBDOWN", "BNBUP", "BTCDOWN", "BTCUP", "BULL", "DOTDOWN", "DOTUP", "EOSBEAR", "EOSBULL",
            "EOSDOWN", "EOSUP", "ETHBEAR", "ETHBULL", "ETHDOWN", "ETHUP", "FILDOWN", "FILUP", "LINKDOWN", "LINKUP",
            "LTCDOWN", "LTCUP", "SUSHIDOWN", "SUSHIUP", "SXPDOWN", "SXPUP", "TRXDOWN", "TRXUP", "UNIDOWN", "UNIUP",
            "XLMDOWN", "XLMUP", "XRPBEAR", "XRPBULL", "XRPDOWN", "XRPUP", "XTZDOWN", "XTZUP", "YFIDOWN", "YFIUP",
        },
    },
}

WHY_KEPT = (
    "A USDT pair on none of the exclusion lists. Delisted pairs are kept, because keeping them is the reason for using"
    " this archive. Fan tokens, memecoins and renamed assets are crypto assets and stay."
)
# Kept on purpose, though the name looks like something excluded above. Listed so the choice is on record.
KEPT_LOOKALIKES = {
    "The asset itself, kept.": {"BTC", "ETH", "SOL"},
    "Ends in UP by chance: Jupiter and Maple's SYRUP are ordinary tokens, not leveraged ones.": {"JUP", "SYRUP"},
    "Ends in B by chance: a crypto asset, listed long before the first bStocks of June 2026.": {
        "AMB", "ARB", "BB", "CKB", "DGB", "MOB", "PHB", "SHIB", "TRB", "VIB", "YB",
    },
    "Starts with W by chance: none of these wraps another token.": {
        "W", "WAL", "WAN", "WAVES", "WAXP", "WCT", "WIF", "WIN", "WING", "WLD", "WLFI", "WOO", "WRX", "WTC",
    },
    "Holds the letters AUD, US or SOL by chance, and isn't pegged to anything.": {"AUDIO", "CETUS", "DUSK", "GENIUS", "RESOLV", "SUSHI"},
    "A project's governance token, not its stablecoin: USUAL's stablecoin is USD0.": {"USUAL"},
    "The old FXS governance token, renamed in January 2026, as the file dates show. Not the stablecoin.": {"FRAX"},
    "Holds BTC, ETH or SOL by chance or as branding, and doesn't track that asset 1:1.": {"BTCST", "ETHFI", "SOLV"},
}
_REVIEWED = set().union(*KEPT_LOOKALIKES.values())
# A kept name that matches and isn't in the list above gets flagged in the manifest, for a person to look at.
_SUSPICIOUS = re.compile(r".*(UP|DOWN|BULL|BEAR|B)|.*(USD|US|EUR|AUD|GBP|BTC|ETH|BNB|SOL).*|W.*")

# For whoever reads the manifest later.
NOTES = {
    "checks_on_holdout_rows": "Numeric format, finite, price above 0, volume not negative, timestamps and duplicates. OHLC order is"
    " checked on dev rows only, because preregistration section 4 limits holdout checks to rows, gaps and timestamps.",
    "bars_closing_early": "A bar can end early when trading halted or a pair was delisted. Those rows are real, so they are kept and counted.",
    "not_on_bar_grid": "Rows whose open time is off the bar grid are rejected, not moved, and leave a gap. Binance's restart after the"
    " February 2018 outage produced these.",
    "long_gaps": "A long gap can mean the name now belongs to another asset, as LUNA did in May 2022. Panels hold what the archive shows"
    " under one name, so whatever builds signals must not read a return across such a gap.",
    "raw_cache": "data/research/raw/binance holds the original zips, holdout months included. Research code reads load_panel, never these.",
}

ROOT = Path(os.environ.get("RESEARCH_ROOT") or PROJECT)
RAW = ROOT / "data" / "research" / "raw" / "binance"
DIGESTS = RAW / "verified_sha256.tsv"
MANIFEST = ROOT / "data" / "research" / "manifest_binance.json"

_NUMBER = rb"([-+]?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)"
_WHOLE = rb"([0-9]{1,19})"  # Bounded, because int() refuses thousands of digits and that shouldn't stop a run.
# open_time, open, high, low, close, volume, close_time, quote_volume, trades, taker base, taker quote, ignore.
_ROW = re.compile(b",".join((_WHOLE, _NUMBER, _NUMBER, _NUMBER, _NUMBER, _NUMBER, _WHOLE, _NUMBER, _WHOLE, _NUMBER, _NUMBER, _NUMBER)))
_HEADER = re.compile(rb"open[ _]?time,open,high,low,close,volume,close[ _]?time,.*", re.IGNORECASE)
_CHECKSUM = re.compile(rb"([0-9a-fA-F]{64})\s+\*?\S+\s*")
_SYMBOL = re.compile(r"[^\W_]+")  # Letters and digits only, so a listed name can't climb out of the cache folder.
_DIGEST_LINE = re.compile(r"([^\t\n]+)\t([0-9a-f]{64})")

_local = threading.local()
_lock = threading.Lock()
_http_trouble: Counter = Counter()
_give_up = threading.Event()


class Unreachable(Exception):
    """Every attempt at a request failed. A rerun can fix this, so it stops the panels being written."""


@dataclass(frozen=True)
class ArchiveFile:
    symbol: str
    interval: str
    kind: str  # "monthly" or "daily".
    period: str  # "2024-07" or "2024-07-16".
    start_ts: int
    end_ts: int

    @property
    def name(self) -> str:
        return f"{self.symbol}-{self.interval}-{self.period}.zip"

    @property
    def key(self) -> str:
        return f"data/spot/{self.kind}/klines/{self.symbol}/{self.interval}/{self.name}"

    @property
    def cache_path(self) -> Path:
        return RAW / self.kind / self.symbol / self.interval / self.name


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {message}", flush=True)


def utc_text(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _note_trouble(kind: str) -> None:
    with _lock:
        _http_trouble[kind] += 1
        if _http_trouble["requests_given_up"] >= GIVE_UP_AFTER:
            _give_up.set()


def fetch(host: str, path: str) -> bytes | None:
    """GET with retry and backoff, on one kept-alive connection per thread. None means a 404."""
    if not hasattr(_local, "connections"):
        _local.connections = {}
    for attempt in range(ATTEMPTS):
        if _give_up.is_set():
            break
        wait = min(60, 2**attempt) * random.uniform(0.5, 1)
        try:
            connection = _local.connections.get(host)
            if connection is None:
                connection = _local.connections[host] = http.client.HTTPSConnection(host, timeout=60)
            connection.request("GET", path, headers={"User-Agent": "duo-research-fetch"})
            response = connection.getresponse()
            body = response.read(MAX_DOWNLOAD_BYTES + 1)
            if response.status == 200 and len(body) <= MAX_DOWNLOAD_BYTES:
                return body
            if response.status == 404:
                return None
            _note_trouble(f"status_{response.status}")
            retry_after = response.getheader("Retry-After", "")
            if retry_after.isdigit():
                wait = min(300, int(retry_after))
        except (OSError, http.client.HTTPException):
            _note_trouble("connection_errors")  # Includes a kept-alive connection the server has closed.
        connection = _local.connections.pop(host, None)
        if connection is not None:
            connection.close()
        time.sleep(wait)
    _note_trouble("requests_given_up")
    raise Unreachable(path)


def list_bucket(prefix: str, delimiter: str = "") -> tuple[list[str], list[str]]:
    """Every key and folder under a prefix, following the marker from page to page."""
    keys, folders, marker = [], [], ""
    while True:
        query = urlencode({"prefix": prefix, "delimiter": delimiter, "marker": marker})
        body = fetch(LIST_HOST, f"/{BUCKET}?{query}")
        try:
            page = ET.fromstring(body or b"")
        except ET.ParseError as error:
            raise Unreachable(f"unreadable listing for {ascii(prefix)}") from error
        page_keys = [item.text or "" for item in page.findall("{*}Contents/{*}Key")]
        page_folders = [item.text or "" for item in page.findall("{*}CommonPrefixes/{*}Prefix")]
        keys += page_keys
        folders += page_folders
        if page.findtext("{*}IsTruncated") != "true":
            return keys, folders
        following = page.findtext("{*}NextMarker") or max(page_keys + page_folders, default="")
        if following <= marker:
            raise Unreachable(f"listing for {ascii(prefix)} stopped moving")
        marker = following


def discover_symbols() -> tuple[list[str], list[str]]:
    """Every symbol in the monthly or daily folders, and any name that isn't letters and digits."""
    names = set()
    for kind in ("monthly", "daily"):
        prefix = f"data/spot/{kind}/klines/"
        names |= {folder[len(prefix) :].rstrip("/") for folder in list_bucket(prefix, "/")[1]}
    good = sorted(name for name in names if _SYMBOL.fullmatch(name))
    bad = sorted(ascii(name)[:80] for name in names if not _SYMBOL.fullmatch(name))
    return good, bad


def classify(symbols: list[str]) -> dict:
    """Splits USDT pairs into kept and excluded. A delisted pair is treated like any other."""
    category_of = {asset: category for category, entry in EXCLUDED.items() for asset in entry["names"]}
    kept, excluded, other_quotes, unreviewed = {}, {}, 0, []
    for symbol in symbols:
        asset = symbol[: -len(QUOTE)]
        if not symbol.endswith(QUOTE) or not asset:
            other_quotes += 1
        elif asset in category_of:
            excluded[asset] = category_of[asset]
        else:
            kept[asset] = symbol
            if _SUSPICIOUS.fullmatch(asset) and asset not in _REVIEWED:
                unreviewed.append(asset)
    return {"kept": kept, "excluded": excluded, "other_quotes": other_quotes, "unreviewed": unreviewed}


def _utc_ts(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def _month_span(period: str) -> tuple[int, int]:
    year, month = int(period[:4]), int(period[5:7])
    return _utc_ts(year, month, 1), _utc_ts(year + month // 12, month % 12 + 1, 1)


def _day_span(period: str) -> tuple[int, int]:
    start = _utc_ts(int(period[:4]), int(period[5:7]), int(period[8:10]))
    return start, start + 86_400


def plan_symbol(symbol: str, interval: str) -> dict:
    """Which files to use for one symbol: all monthly files, and daily files for months that have none.

    Reads the whole daily folder and not only the newest month, so a month missing from the middle
    of the monthly files gets filled from daily files too.
    """
    pattern = re.compile(rf"{re.escape(symbol)}-{re.escape(interval)}-([0-9]{{4}}-[0-9]{{2}}(?:-[0-9]{{2}})?)\.zip(\.CHECKSUM)?")
    found: dict[str, dict] = {"monthly": {}, "daily": {}}
    unrecognised = 0
    for kind, length, span in (("monthly", 7, _month_span), ("daily", 10, _day_span)):
        prefix = f"data/spot/{kind}/klines/{symbol}/{interval}/"
        for key in list_bucket(prefix)[0]:
            match = pattern.fullmatch(key[len(prefix) :])
            try:
                if match is None or len(match[1]) != length:
                    raise ValueError
                span(match[1])  # Refuses a date that doesn't exist, such as 2024-13.
            except ValueError:
                unrecognised += 1
                continue
            found[kind].setdefault(match[1], set()).add("checksum" if match[2] else "zip")

    files, no_checksum, after_end, daily_not_needed = [], [], 0, 0
    months_with_file = {period for period, parts in found["monthly"].items() if "zip" in parts}
    for kind, span in (("monthly", _month_span), ("daily", _day_span)):
        for period, parts in sorted(found[kind].items()):
            if "zip" not in parts:
                continue
            start, end = span(period)
            if end > END_TS:
                after_end += 1
            elif kind == "daily" and period[:7] in months_with_file:
                daily_not_needed += 1
            elif "checksum" not in parts:
                no_checksum.append(f"{kind}/{symbol}-{interval}-{period}.zip")
            else:
                files.append(ArchiveFile(symbol, interval, kind, period, start, end))
    return {
        "files": files, "no_checksum": no_checksum, "after_end": after_end,
        "daily_not_needed": daily_not_needed, "unrecognised": unrecognised,
    }


def plan_or_flag(symbol: str, interval: str) -> dict | None:
    try:
        return plan_symbol(symbol, interval)
    except Unreachable:
        return None


def read_digests() -> dict[str, str]:
    """Checksums that have already matched a cached zip. A torn last line won't parse and gets fetched again."""
    try:
        lines = DIGESTS.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    matches = (_DIGEST_LINE.fullmatch(line) for line in lines)
    return {match[1]: match[2] for match in matches if match}


def _note_digest(relative: str, digest: str, digests: dict[str, str]) -> None:
    with _lock:
        digests[relative] = digest
        with DIGESTS.open("a", encoding="utf-8") as out:
            out.write(f"{relative}\t{digest}\n")


def fetch_digest(file: ArchiveFile) -> str | None:
    body = fetch(FILE_HOST, "/" + quote(file.key + ".CHECKSUM"))
    match = _CHECKSUM.fullmatch(body) if body else None
    return match[1].decode().lower() if match else None


def verified_zip(file: ArchiveFile, digests: dict[str, str], counts: Counter) -> bytes | None:
    """The zip's bytes, checked against its .CHECKSUM, from the cache if possible. None if it can't be had."""
    relative = str(file.cache_path.relative_to(RAW))
    if file.cache_path.exists():
        data = file.cache_path.read_bytes()
        # No digest on file means the last run stopped between saving the zip and noting its checksum.
        digest = digests.get(relative) or fetch_digest(file)
        if digest and hashlib.sha256(data).hexdigest() == digest:
            counts["files_from_cache"] += 1
            if relative not in digests:
                _note_digest(relative, digest, digests)
            return data
        counts["cached_copies_replaced"] += 1

    for _ in range(CHECKSUM_TRIES):
        data = fetch(FILE_HOST, "/" + quote(file.key))
        if data is None:
            counts["files_listed_but_missing"] += 1
            return None
        digest = fetch_digest(file)  # Fetched fresh each try, in case it was the checksum that was wrong.
        if digest is None:
            counts["checksums_unreadable"] += 1
            return None
        if hashlib.sha256(data).hexdigest() == digest:
            file.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp = file.cache_path.with_name(f"{file.cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            temp.write_bytes(data)
            os.replace(temp, file.cache_path)  # Atomic, so a crash can't leave half a zip under the real name.
            _note_digest(relative, digest, digests)
            counts["files_downloaded"] += 1
            counts["bytes_downloaded"] += len(data)
            return data
        counts["checksum_failures"] += 1
    counts["files_failing_checksum"] += 1
    return None


def csv_bytes(data: bytes) -> bytes | None:
    """The one CSV inside a zip, read in memory. Nothing here touches the disk."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) != 1:
                return None
            with archive.open(members[0]) as member:
                raw = member.read(MAX_CSV_BYTES + 1)  # Bounded, so a zip bomb stops here and not when memory runs out.
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError, OSError, ValueError):
        return None
    return None if len(raw) > MAX_CSV_BYTES else raw


def to_seconds(stamp: int) -> tuple[int, int] | None:
    """Whole seconds and units per second, judged by size. Spot went from ms to microseconds in 2025."""
    for limit, unit in ((10**11, 1), (10**14, 1_000), (10**17, 1_000_000)):
        if stamp < limit:
            return stamp // unit, unit
    return None


def _split_of(ts: int | None) -> str:
    if ts is None:
        return "unknown"
    return "dev" if ts < HOLDOUT_START_TS else "holdout"


def check_times(open_stamp: int, close_stamp: int, file: ArchiveFile, seconds: int) -> tuple[str | None, int | None]:
    """Why a row's timestamps get it rejected, if they do, and its bar-open time in seconds."""
    opened, closed = to_seconds(open_stamp), to_seconds(close_stamp)
    if opened is None or closed is None:
        return "timestamp_out_of_range", None
    ts, unit = opened
    if open_stamp % unit or ts % seconds:
        return "not_on_bar_grid", ts
    if not file.start_ts <= ts < file.end_ts:
        return "outside_file_period", ts
    # A bar can end early when trading halted or the pair was delisted, and that row is real: dropping it would
    # lose the last and often the worst day of a dead asset. Ending after the next bar opens would be look-ahead.
    if close_stamp > (ts + seconds) * closed[1]:
        return "closes_after_bar_end", ts
    return None, ts


def parse_rows(raw: bytes, file: ArchiveFile, seconds: int) -> tuple[list[tuple], Counter, list[dict]]:
    """Rows as (ts, open, high, low, close, volume, quote_volume), plus what was refused and why."""
    rows, counts, samples = [], Counter(), []

    def reject(reason: str, ts: int | None, line_number: int) -> None:
        counts[f"rejected.{reason}.{_split_of(ts)}"] += 1
        if len(samples) < REJECT_SAMPLES:
            # Never the row's text: that is untrusted, and on a holdout row it could show prices.
            samples.append({"file": file.name, "line": line_number, "reason": reason, "split": _split_of(ts)})

    for number, line in enumerate(raw.removeprefix(b"\xef\xbb\xbf").splitlines(), 1):
        counts["lines_read"] += 1
        line = line.strip()
        if not line:
            counts["blank_lines"] += 1
            continue
        if number == 1 and _HEADER.fullmatch(line):
            counts["header_rows"] += 1
            continue
        match = _ROW.fullmatch(line)
        if match is None:
            reject("wrong_field_count" if line.count(b",") != 11 else "not_numeric", None, number)
            continue
        reason, ts = check_times(int(match[1]), int(match[7]), file, seconds)
        if reason:
            reject(reason, ts, number)
            continue
        values = [float(match[index]) for index in (2, 3, 4, 5, 6, 8, 10, 11, 12)]
        bar_open, high, low, bar_close, volume, quote_volume = values[:6]
        if not all(map(math.isfinite, values)):
            reject("not_finite", ts, number)
        elif min(bar_open, high, low, bar_close) <= 0:
            reject("price_not_positive", ts, number)
        elif min(volume, quote_volume) < 0:
            reject("negative_volume", ts, number)
        # Dev only: section 4 of the preregistration limits holdout checks to rows, gaps and timestamps.
        elif ts < HOLDOUT_START_TS and not (low <= min(bar_open, bar_close) and max(bar_open, bar_close) <= high):
            reject("ohlc_out_of_order", ts, number)
        else:
            rows.append((ts, bar_open, high, low, bar_close, volume, quote_volume))
            close_stamp = int(match[7])
            if close_stamp + 1 < (ts + seconds) * to_seconds(close_stamp)[1]:
                counts[f"bars_closing_early_{_split_of(ts)}"] += 1
    return rows, counts, samples


def load_file(file: ArchiveFile, seconds: int, digests: dict[str, str]) -> dict:
    """One file, from cache or network to parsed rows. Runs in a worker thread."""
    result = {"file": file, "rows": [], "counts": Counter(), "samples": [], "problem": None}
    try:
        data = verified_zip(file, digests, result["counts"])
    except Unreachable:
        return {**result, "problem": "unreachable"}
    if data is None:
        return {**result, "problem": "not_verified"}
    result["counts"]["checksums_verified"] += 1
    raw = csv_bytes(data)
    if raw is None:
        result["counts"]["unreadable_zips"] += 1
        return {**result, "problem": "unreadable_zip"}
    result["rows"], row_counts, result["samples"] = parse_rows(raw, file, seconds)
    result["counts"].update(row_counts)
    return result


def ranges_and_gaps(ts: np.ndarray, assets: list[str], close: np.ndarray, volume: np.ndarray, seconds: int) -> dict:
    """Date ranges, row counts and gaps. Uses only whether a bar exists and whether anything traded."""
    has_bar = ~np.isnan(close)
    in_dev = ts < HOLDOUT_START_TS
    per_asset, long_gaps = {}, []
    for column, asset in enumerate(assets):
        rows = np.flatnonzero(has_bar[:, column])
        if rows.size == 0:
            continue
        missing = np.flatnonzero(~has_bar[rows[0] : rows[-1] + 1, column]) + rows[0]
        steps = np.diff(rows)
        per_asset[asset] = {
            "first_bar": utc_text(int(ts[rows[0]])),
            "last_bar": utc_text(int(ts[rows[-1]])),
            "bars_dev": int(in_dev[rows].sum()),
            "bars_holdout": int((~in_dev[rows]).sum()),
            "gap_bars_dev": int(in_dev[missing].sum()),
            "gap_bars_holdout": int((~in_dev[missing]).sum()),
            "longest_gap_bars": int(steps.max(initial=1)) - 1,
        }
        for step in np.flatnonzero(steps * seconds > LONG_GAP_DAYS * 86_400):
            long_gaps.append({
                "asset": asset,
                "last_bar_before": utc_text(int(ts[rows[step]])),
                "first_bar_after": utc_text(int(ts[rows[step + 1]])),
                "days": round(int(steps[step]) * seconds / 86_400, 2),
            })
    ended = [info["last_bar"] for info in per_asset.values()]
    last_row = utc_text(int(ts[-1]))
    boundary = utc_text(HOLDOUT_START_TS)
    no_bars = ~has_bar.any(axis=1)
    return {
        "first_row": utc_text(int(ts[0])),
        "last_row": last_row,
        "rows_dev": int(in_dev.sum()),
        "rows_holdout": int((~in_dev).sum()),
        "assets_with_bars": len(per_asset),
        "assets_with_bars_dev": int(has_bar[in_dev].any(axis=0).sum()),
        "assets_with_bars_holdout": int(has_bar[~in_dev].any(axis=0).sum()),
        "assets_ending_before_holdout": sum(last < boundary for last in ended),
        "assets_ending_during_holdout": sum(boundary <= last < last_row for last in ended),
        "assets_with_a_bar_on_last_row": sum(last == last_row for last in ended),
        "bars_dev": int(has_bar[in_dev].sum()),
        "bars_holdout": int(has_bar[~in_dev].sum()),
        "rows_with_no_bars_dev": int(no_bars[in_dev].sum()),
        "rows_with_no_bars_holdout": int(no_bars[~in_dev].sum()),
        "gap_bars_dev": sum(info["gap_bars_dev"] for info in per_asset.values()),
        "gap_bars_holdout": sum(info["gap_bars_holdout"] for info in per_asset.values()),
        "zero_volume_bars_dev": int((volume[in_dev] == 0).sum()),
        "zero_volume_bars_holdout": int((volume[~in_dev] == 0).sum()),
        f"gaps_over_{LONG_GAP_DAYS}_days": long_gaps,
        "per_asset": per_asset,
    }


def _rejected_summary(counts: Counter) -> dict:
    by_reason: dict[str, dict[str, int]] = {}
    for key, value in counts.items():
        if key.startswith("rejected."):
            _, reason, split = key.split(".")
            by_reason.setdefault(reason, {})[split] = value
    total = sum(sum(splits.values()) for splits in by_reason.values())
    return {"total": total, "by_reason": dict(sorted(by_reason.items()))}


def build_panel(name: str, symbols: dict[str, str], digests: dict[str, str], pool: ThreadPoolExecutor) -> dict:
    """Plans, downloads and parses one panel, then stores it unless something was unreachable."""
    interval, seconds = PANELS[name]
    log(f"{name}: listing files for {len(symbols)} symbols")
    assets = sorted(symbols)
    plans = list(pool.map(lambda asset: plan_or_flag(symbols[asset], interval), assets))
    problems: dict[str, list[str]] = {}
    if failed := [asset for asset, plan in zip(assets, plans) if plan is None]:
        problems["listing_unreachable"] = failed
    usable = [plan for plan in plans if plan is not None]
    files = [file for plan in usable for file in plan["files"]]
    no_checksum = [path for plan in usable for path in plan["no_checksum"]]
    if no_checksum:
        problems["no_checksum_published"] = no_checksum
    counts: Counter = Counter({
        "monthly_files_planned": sum(file.kind == "monthly" for file in files),
        "daily_files_planned": sum(file.kind == "daily" for file in files),
        "files_after_end_skipped": sum(plan["after_end"] for plan in usable),
        "daily_files_not_needed": sum(plan["daily_not_needed"] for plan in usable),
        "unrecognised_keys": sum(plan["unrecognised"] for plan in usable),
        "checksums_not_published": len(no_checksum),
    })
    log(f"{name}: {len(files)} files planned")

    column_of = {symbols[asset]: column for column, asset in enumerate(assets)}
    grid_start = min((file.start_ts for file in files), default=START_TS)
    rows = (END_TS - grid_start) // seconds
    grid = {key: np.full((rows, len(assets)), np.nan) for key in CORE_FIELDS}
    samples: list[dict] = []
    started = last_note = time.time()
    # map keeps file order, so "the first row wins" on a duplicate gives the same answer on every run.
    for done, result in enumerate(pool.map(lambda file: load_file(file, seconds, digests), files), 1):
        file = result["file"]
        counts.update(result["counts"])
        if result["problem"]:
            problems.setdefault(result["problem"], []).append(f"{file.kind}/{file.name}")
        samples += result["samples"][: REJECT_SAMPLES - len(samples)]
        column = column_of[file.symbol]
        for ts, *values in result["rows"]:
            row = (ts - grid_start) // seconds
            if not np.isnan(grid["close"][row, column]):
                counts[f"rejected.duplicate_bar.{_split_of(ts)}"] += 1
                continue
            for key, value in zip(CORE_FIELDS, values):
                grid[key][row, column] = value
            counts["bars_kept"] += 1
        if time.time() - last_note >= 60 or done == len(files):
            last_note = time.time()
            with _lock:
                retries = _http_trouble.total()
            log(
                f"{name}: {done}/{len(files)} files, {counts['files_downloaded']} downloaded,"
                f" {counts['files_from_cache']} from cache, {counts['checksum_failures']} checksum failures,"
                f" {len(problems.get('unreachable', []))} unreachable, {retries} http retries,"
                f" {time.time() - started:.0f}s"
            )

    rejected = _rejected_summary(counts)
    accounted = counts["bars_kept"] + rejected["total"] + counts["header_rows"] + counts["blank_lines"]
    if accounted != counts["lines_read"]:  # Nothing may vanish without a count.
        raise RuntimeError(f"{name}: read {counts['lines_read']} lines but accounted for {accounted}")

    has_bars = ~np.isnan(grid["close"]).all(axis=1)
    first_used = int(np.argmax(has_bars))
    ts = grid_start + first_used * seconds + np.arange(rows - first_used, dtype=np.int64) * seconds
    grid = {key: block[first_used:] for key, block in grid.items()}
    in_period = ts >= START_TS
    has_any = ~np.isnan(grid["close"]).all(axis=0)
    columns = np.flatnonzero(has_any).tolist()
    report = {
        "interval": interval,
        "symbols_requested": len(symbols),
        "symbols_without_bars": [asset for asset, found in zip(assets, has_any) if not found],
        "counts": {key: value for key, value in sorted(counts.items()) if not key.startswith("rejected.")},
        "rejected_rows": rejected,
        "rejected_samples": samples,
        "files_not_used": problems,
        "bars_before_period_start_dropped": int((~np.isnan(grid["close"][~in_period])).sum()),
        **ranges_and_gaps(ts[in_period], assets, grid["close"][in_period], grid["volume"][in_period], seconds),
    }
    if problems.get("unreachable") or problems.get("listing_unreachable"):
        report["stored"] = None  # A partial panel in the store would look whole to everything downstream.
        return report
    # Meta goes in the dev file too, so it says nothing about the holdout, and no run time keeps it repeatable.
    meta = {
        "source": "data.binance.vision public spot klines", "interval": interval, "quote": QUOTE,
        "tool": "research/tools/fetch_binance.py",
    }
    fields = {key: block[:, columns] for key, block in grid.items()}
    report["stored"] = write_panel(name, ts, [assets[column] for column in columns], fields, meta)
    log(f"{name}: stored {report['stored']}")
    return report


def write_manifest(manifest: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    temp = MANIFEST.with_name(f"{MANIFEST.name}.{os.getpid()}.tmp")
    # ASCII, because agents read this file and some names in it came from a download.
    temp.write_text(json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=True) + "\n", encoding="ascii")
    os.replace(temp, MANIFEST)


def main() -> int:
    started = time.time()
    started_utc = utc_text(int(started))
    RAW.mkdir(parents=True, exist_ok=True)
    digests = read_digests()
    log(f"{len(digests)} verified checksums already on file")
    symbols, bad_names = discover_symbols()
    split = classify(symbols)
    kept, excluded = split["kept"], split["excluded"]
    log(f"{len(symbols)} symbols in the archive, {len(kept)} USDT pairs kept, {len(excluded)} excluded")
    if split["unreviewed"]:
        log(f"{len(split['unreviewed'])} kept names look like an excluded kind and aren't on the reviewed list")
    with ThreadPoolExecutor(WORKERS) as pool:
        panels = {
            "spot_1d": build_panel("spot_1d", kept, digests, pool),
            "majors_15m": build_panel("majors_15m", {asset: kept[asset] for asset in MAJORS}, digests, pool),
        }
    complete = all(panel["stored"] is not None for panel in panels.values())
    parts = [panel["counts"] for panel in panels.values()]
    write_manifest({
        "run": {
            "started_utc": started_utc,
            "finished_utc": utc_text(int(time.time())),
            "elapsed_seconds": round(time.time() - started),
            "status": "complete" if complete else "incomplete: some requests failed, so nothing was stored. Rerun.",
            "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "source": f"https://{FILE_HOST}, listed through https://{LIST_HOST}/{BUCKET}",
            "period": {"start": utc_text(START_TS), "holdout_start": utc_text(HOLDOUT_START_TS), "end_exclusive": utc_text(END_TS)},
            "http_retries_by_cause": dict(sorted(_http_trouble.items())),
        },
        "notes": NOTES,
        "totals": {
            "symbols_in_archive": len(symbols) + len(bad_names),
            "usdt_pairs": len(kept) + len(excluded),
            "kept": len(kept),
            "excluded": len(excluded),
            "files_verified": sum(part.get("checksums_verified", 0) for part in parts),
            "files_downloaded": sum(part.get("files_downloaded", 0) for part in parts),
            "files_from_cache": sum(part.get("files_from_cache", 0) for part in parts),
            "checksum_failures": sum(part.get("checksum_failures", 0) for part in parts),
            "files_not_used": sum(len(paths) for panel in panels.values() for paths in panel["files_not_used"].values()),
            "lines_read": sum(part.get("lines_read", 0) for part in parts),
            "header_rows": sum(part.get("header_rows", 0) for part in parts),
            "bars_kept": sum(part.get("bars_kept", 0) for part in parts),
            "rejected_rows": sum(panel["rejected_rows"]["total"] for panel in panels.values()),
        },
        "symbols": {
            "names_refused": bad_names,
            "other_quote_pairs": split["other_quotes"],
            "kept_assets": sorted(kept),
            "why_kept": WHY_KEPT,
            "excluded_assets": {
                category: sorted(asset for asset, found in excluded.items() if found == category) for category in EXCLUDED
            },
            "why_excluded": {category: entry["why"] for category, entry in EXCLUDED.items()},
            "kept_lookalikes": {why: sorted(names) for why, names in KEPT_LOOKALIKES.items()},
            "kept_lookalikes_unreviewed": split["unreviewed"],
        },
        "panels": panels,
    })
    log(f"manifest written, {'complete' if complete else 'INCOMPLETE'}, {time.time() - started:.0f}s in all")
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
