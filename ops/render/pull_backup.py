"""python3 ops/render/pull_backup.py pull|page ...: GitHub's side of the paper run on Render (RUNBOOK 13.7).

  pull URL OUT_DIR --seed-db FILE --seed-sha SHA [--previous MANIFEST]
  page OUT_DIR URL [--final]

Since the cutover (RUNBOOK 13.3) the paper run lives on the service's disk at URL, and its supervisor backs the database
up every 6 hours. pull downloads the latest backup from URL/state/ and writes its files (paper_systematic.db,
heartbeat_systematic.json if it has one, and manifest.json) into OUT_DIR, but only once all of these hold:
- each file is the one the manifest describes (its sha256), and the database passes SQLite's integrity check;
- it holds the run Render was seeded with: its start record is the manifest's and FILE's, the database at the tag
  render-seed, and the manifest names SHA, that commit, as the seed;
- it goes forwards: its newest check (heartbeat) is later than GitHub's last before the move, and no older than the
  copy saved last time (MANIFEST), and the backup is under 26 hours old.
The Render backup workflow (.github/workflows/render-backup.yml) then saves the files on the paper-state branch in
data/render/, never in data/ itself.

page writes the GitHub Pages site into OUT_DIR. By default that's a small page saying the paper test has moved, which
links the service's page, compare.md, decisions.jsonl and its latest backup's manifest, and takes the reader there.
--final copies the service's own page instead, so github.io keeps the end of the test once the service is gone
(RUNBOOK 13.8): status.json, index.html, and compare.md and decisions.jsonl unless status.json's files object has null
for one, as it does when the service's build couldn't make it. That one is left out with a ::warning:: line giving
status.json's reason, and the rest is still copied.

It uses the standard library only and never runs duo. It reads the databases through read-only, immutable
connections, which can't change them or create anything beside them. It never follows a redirect, as the service never
sends one, and reads no more of an answer than the service's file could be (MAX_BYTES). On any failure it prints a
GitHub ::error:: line that says what to do next, writes nothing and exits 1.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import json
import re
import sqlite3
import string
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

MANIFEST, DATABASE, HEARTBEAT = "manifest.json", "paper_systematic.db", "heartbeat_systematic.json"
PAGE_FILES = ("index.html", "status.json", "compare.md", "decisions.jsonl")
# The most of each file that's read. Whatever answers at the URL could send without end, into the runner's memory until
# the job timed out. The service's own files are far smaller (the database was 192,512 bytes on 7 November 2026, and
# each of the page's is tens of KB), so a bigger answer isn't the service's file: it's refused as soon as more has come.
MAX_BYTES = {MANIFEST: 1_000_000, HEARTBEAT: 1_000_000, DATABASE: 200_000_000, **dict.fromkeys(PAGE_FILES, 20_000_000)}
# The supervisor backs up every 6 hours, and the workflow runs four times a day, if hours late: a backup over a day old
# means the service's backups have stopped.
MAX_AGE_HOURS = 26
# A request that can't connect, or gets a server error from the service or Render in front of it, is tried again: a
# blip shouldn't fail the run and email you.
TRIES = 3
PAUSE_SECONDS = 10
TIMEOUT_SECONDS = 30
USER_AGENT = "duo-paper render-backup"
REFRESH_SECONDS = 10  # How long the moved page shows before it takes the reader to the service.

MOVED_PAGE = string.Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="$refresh; url=$url/">
<title>The paper test has moved</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.5 system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem; }
</style>
</head>
<body>
<h1>The paper test has moved</h1>
<p>The paper test now runs on Render, which rebuilds its status page every 5 minutes. This page takes you there in
$refresh seconds: <a href="$url/">$url</a>.</p>
<ul>
<li><a href="$url/">The status page</a></li>
<li><a href="$url/compare.md">compare.md</a>, the full report, order by order</li>
<li><a href="$url/decisions.jsonl">decisions.jsonl</a>, every decision, one per line</li>
<li><a href="$url/state/manifest.json">manifest.json</a>, the service's latest backup</li>
</ul>
<p>GitHub keeps a copy of the run from those backups, on the paper-state branch in data/render/.</p>
</body>
</html>
""")


class Refused(Exception):
    """What went wrong, in words that say what to do next. Nothing has been written."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """The service never redirects, so an answer that does isn't the service's, and nothing has checked where it points.
    The redirect comes back as the HTTPError it is."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirects)


def fetch(url: str, limit: int) -> bytes:
    """url's body, of at most limit bytes. One that can't connect, gets a server error or is cut short is tried TRIES
    times in all; Refused says why it couldn't be had."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    problem = ""
    for attempt in range(TRIES):
        if attempt:
            time.sleep(PAUSE_SECONDS)
        try:
            with OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise Refused(f"{url} answered with more than {limit:,} bytes, far more than the service's file "
                                  f"would be, so it wasn't read further")
                if response.length:  # Fewer bytes came than its Content-Length said: it was cut short on the way.
                    raise http.client.IncompleteRead(data, response.length)
                return data
        except urllib.error.HTTPError as error:
            problem = f"{url} answered {error.code} {error.reason}"
            if error.code < 500:  # The answer won't change on a second try, a redirect's included.
                break
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            problem = f"{url} didn't answer ({getattr(error, 'reason', None) or error})"
    raise Refused(problem)


def download(url: str, limit: int) -> bytes:
    """url's body, from the service's state/: Refused, if it can't be had, says where to look."""
    try:
        return fetch(url, limit)
    except Refused as error:
        raise Refused(f"Couldn't download the service's backup: {error}. Its /supervisor.json says whether it's "
                      f"running and when it last backed up (RUNBOOK 13.7); run this again once it has.") from None


def utc(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(at))


def number(value) -> float | None:
    """value if it's a JSON number, else None (true and false aren't numbers here)."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def read_only(path: Path) -> sqlite3.Connection:
    """A connection that can't write to path. Immutable, as nothing else touches these files: it then creates no -wal
    or -shm beside them, so what's saved is exactly what was downloaded."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def start_sha256(connection: sqlite3.Connection) -> str | None:
    """The sha256 of the run's start record (state key sys_start) exactly as stored, or None if there's none."""
    row = connection.execute("SELECT value FROM state WHERE key = 'sys_start'").fetchone()
    if row is None:
        return None
    return hashlib.sha256(row[0] if isinstance(row[0], bytes) else str(row[0]).encode("utf-8")).hexdigest()


def seed_start(path: Path) -> str:
    """The start record's sha256 in the database Render was seeded with."""
    if not path.is_file():
        raise Refused(f"The seed database, {path}, isn't there, so the backup can't be checked against it.")
    try:
        with closing(read_only(path)) as connection:
            start = start_sha256(connection)
    except sqlite3.Error as error:
        raise Refused(f"The seed database, {path}, couldn't be read ({error}).") from None
    if start is None:
        raise Refused(f"The seed database, {path}, holds no paper run (it has no start record). render-seed must be "
                      f"the commit Render was seeded from (RUNBOOK 13.3).")
    return start


def checked(data: bytes) -> tuple[str | None, float | None]:
    """(start record's sha256, newest check's time) in the database data, once it passes SQLite's integrity check."""
    with tempfile.TemporaryDirectory() as temporary:
        copy = Path(temporary) / DATABASE
        copy.write_bytes(data)
        with closing(read_only(copy)) as connection:
            try:
                problems = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            except sqlite3.Error as error:  # A page damaged badly enough is an error, not a row.
                problems = [str(error)]
            if problems != ["ok"]:
                raise Refused(f"The backup's database fails SQLite's integrity check ({'; '.join(problems[:3])}), so "
                              f"it isn't saved. Take a new backup on the service (RUNBOOK 13.7) and run this again.")
            try:
                newest = connection.execute("SELECT MAX(ts) FROM sys_heartbeats").fetchone()[0]
                return start_sha256(connection), number(newest)
            except sqlite3.Error as error:
                raise Refused(f"The backup's database isn't a paper run's ({error}), so it isn't saved.") from None


def pull(url: str, out: Path, seed_db: Path, seed_sha: str, previous: Path | None = None,
         now: float | None = None) -> tuple[dict, float]:
    """Downloads the service's latest backup and, once it checks out, writes its files into out. Returns (its
    manifest, its newest check's time). Raises Refused, having written nothing, if anything is wrong."""
    now = time.time() if now is None else now
    base = url.rstrip("/") + "/state/"
    raw = download(base + MANIFEST, MAX_BYTES[MANIFEST])
    try:
        manifest = json.loads(raw)
    except ValueError as error:
        raise Refused(f"The backup's manifest.json isn't JSON ({error}).") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise Refused("The backup's manifest.json isn't one this reads (schema 1). Publish this script to match the "
                      "service's code.")
    seed = manifest.get("seed") if isinstance(manifest.get("seed"), dict) else {}
    if seed.get("sha") != seed_sha:
        raise Refused(f"The service was seeded from {seed.get('sha') or 'nothing'}, not from {seed_sha}, the commit "
                      f"the tag render-seed marks. It doesn't hold the run GitHub handed over, so its backup isn't "
                      f"saved. Look at the service's seed marker in /supervisor.json (RUNBOOK 13.3).")
    taken_at = number(manifest.get("taken_at"))
    if taken_at is None:
        raise Refused("The backup's manifest.json doesn't say when it was taken.")
    if now - taken_at > MAX_AGE_HOURS * 3600:
        raise Refused(f"The service's latest backup was taken at {utc(taken_at)}, {(now - taken_at) / 3600:.0f} hours "
                      f"ago, so its backups have stopped. backup.error in its /supervisor.json says why "
                      f"(RUNBOOK 13.7).")

    files = {DATABASE: download(base + DATABASE, MAX_BYTES[DATABASE])}
    if manifest.get("heartbeat") is not None:
        files[HEARTBEAT] = download(base + HEARTBEAT, MAX_BYTES[HEARTBEAT])
    for name, data in files.items():
        listed = manifest.get("db" if name == DATABASE else "heartbeat")
        listed = listed if isinstance(listed, dict) else {}
        if hashlib.sha256(data).hexdigest() != listed.get("sha256"):
            raise Refused(f"The {name} downloaded ({len(data)} bytes) isn't the one the manifest describes "
                          f"({listed.get('size')} bytes, sha256 {listed.get('sha256')}). A new backup may have landed "
                          f"while this ran: run it again.")

    start, newest = checked(files[DATABASE])
    if start is None:
        raise Refused("The backup's database holds no paper run (it has no start record), so it isn't saved.")
    if start != manifest.get("start_sha256"):
        raise Refused(f"The backup's start record (sha256 {start}) isn't the one its manifest names "
                      f"({manifest.get('start_sha256')}), so it isn't saved. Run this again after the next backup.")
    if start != seed_start(seed_db):
        raise Refused("The backup holds a different paper run from the one Render was seeded with (the database at "
                      "render-seed), so it isn't saved. Nothing may replace the run on the service: look at its seed "
                      "marker and logs (RUNBOOK 13.3).")
    last_cron_at = number(seed.get("last_cron_at"))
    if last_cron_at is None:
        raise Refused("The backup's seed marker doesn't say when GitHub's last check was (last_cron_at), so the "
                      "backup can't be shown to come after it.")
    if newest is None or newest <= last_cron_at:
        # The supervisor's first backup is one of these: it's taken as the seed finishes, before the loop's first check.
        # It takes the next by itself once the loop has checked.
        raise Refused(f"The backup's newest check ({utc(newest) if newest else 'none'}) isn't later than GitHub's last "
                      f"({utc(last_cron_at)}): the loop on Render hasn't checked anything since the move, or hadn't "
                      f"when this backup was taken. The supervisor backs up by itself within about 5 minutes of the "
                      f"loop's first check (backup.at in its /supervisor.json), so run this again after that, or take "
                      f"one sooner with python3 ops/render/ctl.py backup in its Shell. If the service's page shows no "
                      f"check since the move, its /supervisor.json says why (RUNBOOK 13.7).")
    if previous is not None:
        try:
            floor = number(json.loads(previous.read_text()).get("newest_heartbeat_at"))
        except (OSError, ValueError, AttributeError) as error:
            raise Refused(f"The last copy's manifest, {previous}, couldn't be read ({error}).") from None
        if floor is not None and newest < floor:
            raise Refused(f"The backup's newest check ({utc(newest)}) is older than the copy already saved "
                          f"({utc(floor)}), and saving it would go backwards. Is the service running from an older "
                          f"database? Look at its /supervisor.json and logs (RUNBOOK 13.7).")

    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    if HEARTBEAT not in files:
        (out / HEARTBEAT).unlink(missing_ok=True)
    (out / MANIFEST).write_bytes(raw)  # As the service wrote it, and last: there's no manifest without its files.
    return manifest, newest


def moved_page(url: str) -> str:
    """The page that says the paper test has moved to url, loading nothing from anywhere."""
    return MOVED_PAGE.substitute(url=html.escape(url.rstrip("/"), quote=True), refresh=REFRESH_SECONDS)


def page(out: Path, url: str, final: bool = False) -> tuple[list[str], list[tuple[str, str]]]:
    """Writes the GitHub Pages site into out. Returns the names of the files written, and a warning, as (title,
    message), for each of the service's files the copy leaves out."""
    base = url.rstrip("/")
    warnings = []
    if not final:
        files = {"index.html": moved_page(base).encode("utf-8")}
    else:
        def get(name: str) -> bytes:
            try:
                return fetch(f"{base}/{name}", MAX_BYTES[name])
            except Refused as error:
                raise Refused(f"Couldn't copy the service's page: {error}. Nothing was written, and github.io keeps "
                              f"what it had. Run this again once the service answers.") from None

        raw = get("status.json")
        try:
            status = json.loads(raw)
        except ValueError as error:
            raise Refused(f"{base}/status.json isn't JSON ({error}), so what answered isn't the service's page. "
                          f"Nothing was written.") from None
        # status.json names the files the page links. One the service's build couldn't make is null there, beside the
        # reason, and the page says why instead of linking it, so the copy leaves it out too. With no files object all
        # four are copied, as before there was one. Only the names in PAGE_FILES are ever fetched or written.
        status = status if isinstance(status, dict) else {}
        named = status.get("files") if isinstance(status.get("files"), dict) else {}
        compare = status.get("compare") if isinstance(status.get("compare"), dict) else {}
        reasons = {"compare.md": compare.get("error"), "decisions.jsonl": named.get("decisions_error")}
        left_out = []
        for name, key in (("compare.md", "compare"), ("decisions.jsonl", "decisions")):
            value = named.get(key, name)
            if value is None:
                left_out.append(name)
            elif value != name:
                raise Refused(f"{base}/status.json has {json.dumps(value)[:80]} for {name}, not its name or null, so "
                              f"what answered isn't the service's page. Nothing was written.")
        files = {name: raw if name == "status.json" else get(name) for name in PAGE_FILES if name not in left_out}
        for name in left_out:
            why = f" ({reasons[name][:300]})" if isinstance(reasons[name], str) and reasons[name] else ""
            warnings.append((f"Page copied without {name}", f"status.json says the service's build couldn't make "
                             f"{name}{why}, so its page doesn't link one, and the copy has none either. The rest of "
                             f"the page was copied, and python -m duo compare and decisions still work on the run's "
                             f"copy in data/render/ (RUNBOOK 13.7)."))
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    return list(files), warnings


def annotate(title: str, message: str, kind: str = "error") -> str:
    """A GitHub ::error:: line, or another kind of annotation (::warning::). %, CR and LF are escaped, as workflow
    commands need."""
    escape = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{kind} title={title}::{escape}"


def full_sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise argparse.ArgumentTypeError("must be a full commit SHA, 40 hex characters")
    return value


def service_url(value: str) -> str:
    # The moved page puts it in links, so it must be a web address and nothing else.
    if not re.fullmatch(r"https?://[^\s\"'<>]+", value):
        raise argparse.ArgumentTypeError("must be the service's http:// or https:// address")
    return value


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="pull_backup.py", description="GitHub's side of the paper run on Render: "
                                     "its backup, and the GitHub Pages page.")
    commands = parser.add_subparsers(dest="command", required=True)
    pulling = commands.add_parser("pull", help="download, check and keep the service's latest backup")
    pulling.add_argument("url", metavar="URL", type=service_url,
                         help="the service, such as https://duo-paper.onrender.com")
    pulling.add_argument("out", metavar="OUT_DIR", type=Path, help="where the checked files go")
    pulling.add_argument("--seed-db", metavar="FILE", type=Path, required=True,
                         help="the database Render was seeded with: data/paper_systematic.db at the tag render-seed")
    pulling.add_argument("--seed-sha", metavar="SHA", type=full_sha, required=True,
                         help="the commit render-seed marks")
    pulling.add_argument("--previous", metavar="MANIFEST", type=Path,
                         help="the manifest of the copy saved last time; the new one mustn't be older")
    paging = commands.add_parser("page", help="write the GitHub Pages site")
    paging.add_argument("out", metavar="OUT_DIR", type=Path)
    paging.add_argument("url", metavar="URL", type=service_url)
    paging.add_argument("--final", action="store_true",
                        help="copy the service's own page, for the end of the test, instead of the moved page")
    args = parser.parse_args(argv)
    try:
        if args.command == "pull":
            manifest, newest = pull(args.url, args.out, args.seed_db, args.seed_sha, args.previous)
            print(f"The service's backup of {utc(manifest['taken_at'])} checks out: it holds the run seeded from "
                  f"{args.seed_sha[:7]}, and its newest check is at {utc(newest)}. Its files are in {args.out}.")
        else:
            written, warnings = page(args.out, args.url, args.final)
            for title, message in warnings:
                print(annotate(title, message, "warning"))
            what = "the service's page" if args.final else "the moved page"
            print(f"Wrote {what} into {args.out}: {', '.join(written)}.")
    except Refused as error:
        print(annotate("Render backup not saved" if args.command == "pull" else "Page not written", str(error)))
        sys.exit(1)


if __name__ == "__main__":
    main()
