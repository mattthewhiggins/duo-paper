#!/usr/bin/env bash
# Saves the paper run's state at the end of a GitHub Actions job (.github/workflows/paper.yml).
#
#   ops/actions/save_state.sh DIR      (run from the repository's own checkout; DIR is restore_state.sh's worktree)
#
# Commits DIR's data/ (and .gitattributes) on the paper-state branch and pushes it, touching no other branch. The branch
# keeps one commit per UTC day, a daily snapshot: a save on the same UTC day as the last commit amends that commit, and
# the first save of a new day adds one. When nothing has changed, nothing is committed.
#
# DUO_STATE_TODAY (YYYY-MM-DD) stands in for today's UTC date, and DUO_STATE_PROC for /proc. They're for the tests only.
set -euo pipefail

dir=${1:?usage: save_state.sh DIR}
branch=paper-state
today=${DUO_STATE_TODAY:-$(date -u +%Y-%m-%d)}

# Only ever commit on the state branch: a wrong DIR (the checkout itself, say) must not put state on main.
if [ "$(git -C "$dir" symbolic-ref -q HEAD || true)" != "refs/heads/$branch" ]; then
  echo "::error::$dir isn't a checkout of the $branch branch, so nothing was saved."
  exit 1
fi

# The database runs in WAL mode, so its latest writes can still be in the -wal file. Fold them into the database file
# so that file holds everything. If something is still using it, it may be half-written, so don't save it. The
# checkpoint only says busy when something has held a lock on it for over 5 s (sqlite3's default timeout), not when
# another process merely has it open, as a tick still running would. So that's looked for too.
for db in "$dir"/data/*.db; do
  [ -f "$db" ] || continue
  python3 - "$db" <<'PY'
import glob
import os
import sqlite3
import sys
from pathlib import Path

path = sys.argv[1]
connection = sqlite3.connect(path)
busy = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
connection.close()
if busy:
    sys.exit(f"{path} is still in use somewhere, so it may be half-written. Not saving it.")
# Linux lists each process's open files as links in /proc/PID/fd. Elsewhere (macOS) there's no /proc, and nothing is
# found: the workflow runs on Linux.
names = {os.path.realpath(path), os.path.realpath(path + "-wal")}
holders = set()
for link in glob.glob(os.environ.get("DUO_STATE_PROC", "/proc") + "/[0-9]*/fd/*"):
    pid = link.split("/")[-3]
    try:
        if pid != str(os.getpid()) and os.readlink(link) in names:
            holders.add(pid)
    except OSError:
        pass  # That process, or that file of it, has closed since.
if holders:
    sys.exit(f"{path} is still open in process {', '.join(sorted(holders, key=int))}, so it may be half-written. "
             f"Not saving it.")
# Everything is in the database file now. Some SQLite builds (macOS's) keep an empty -wal and the -shm index after the
# last close; others delete them. Delete them here, so the saved state is the same whichever machine saved it.
wal = Path(path + "-wal")
if wal.exists() and wal.stat().st_size == 0:
    wal.unlink()
    Path(path + "-shm").unlink(missing_ok=True)
PY
done

# -A: a file that's gone (an old -wal, say) leaves the branch too. -f: a global gitignore elsewhere could list *.db.
for path in .gitattributes data; do
  if [ -e "$dir/$path" ] || [ -n "$(git -C "$dir" ls-files -- "$path")" ]; then
    git -C "$dir" add -A -f -- "$path"
  fi
done
if git -C "$dir" diff --cached --quiet; then
  echo "The paper state hasn't changed, so there's nothing to save."
  exit 0
fi

bot=(-c "user.name=github-actions[bot]" -c "user.email=41898282+github-actions[bot]@users.noreply.github.com")
message="Paper state, $today (UTC)"
body="Last saved $(date -u '+%Y-%m-%d %H:%M') UTC${GITHUB_RUN_ID:+ by run $GITHUB_RUN_ID}."
last_day=$(TZ=UTC git -C "$dir" log -1 --format=%cd --date=format-local:%Y-%m-%d 2>/dev/null || true)
if [ "$last_day" = "$today" ]; then
  git -C "$dir" "${bot[@]}" commit --quiet --amend -m "$message" -m "$body"
  how="updated today's commit"
else
  git -C "$dir" "${bot[@]}" commit --quiet -m "$message" -m "$body"
  how="added a commit for $today"
fi
new=$(git -C "$dir" rev-parse HEAD)

# The lease: paper-state on GitHub must still be where restore_state.sh found it (or still not exist, for a new run).
# If another job saved in between, this one fails rather than write over that job's state.
expected=$(git rev-parse --quiet --verify "refs/remotes/origin/$branch" || true)
push() {
  git push --quiet --force-with-lease="refs/heads/$branch:$expected" origin "refs/heads/$branch:refs/heads/$branch"
}
if ! push; then
  # Try once more, unless the push landed after all (its reply was lost) or the branch moved, when a retry can only
  # fail again.
  tip=$(git ls-remote origin "refs/heads/$branch" | cut -f1) || tip=unknown
  if [ "$tip" = "$new" ]; then
    echo "The push went through after all."
  elif [ "$tip" != unknown ] && [ "$tip" != "$expected" ]; then
    echo "::error::$branch on GitHub moved while this job ran (did another run save in between?), so this job's" \
      "state wasn't saved over it."
    exit 1
  else
    echo "The push failed. Trying once more in 5 seconds."
    sleep 5
    push
  fi
fi
echo "Saved the paper state on $branch: $how, $(git -C "$dir" rev-parse --short HEAD)."
