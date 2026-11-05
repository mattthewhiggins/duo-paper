#!/usr/bin/env bash
# Restores the paper run's state at the start of a GitHub Actions job (.github/workflows/paper.yml).
#
#   ops/actions/restore_state.sh DIR ACTION      (run from the repository's own checkout)
#
# The state is data/: the SQLite database, its -wal and -shm if any, and heartbeat_systematic.json. It lives on its own
# branch, paper-state, so main only ever holds code. DIR becomes a git worktree of this checkout, so the fetch uses the
# credentials actions/checkout set up, and the checkout itself doesn't change.
#
# Writes two outputs to $GITHUB_OUTPUT (or prints them, outside Actions):
#   started=true   paper-state was found and checked out at DIR, or ACTION is start and DIR is a new, empty branch
#   started=false  there's no paper-state branch and ACTION isn't start: the paper run hasn't been started
#   new=true       a new paper run starts in DIR: ACTION is start, and DIR is that new branch (nothing of it is on
#                  GitHub yet) or a paper-state with no database. The workflow keeps a new run only if its start works.
#
# A branch whose data/ has no database is checked out as it is, and nothing is created in its place: the tick refuses
# to run without one, so a lost database fails the job instead of quietly starting a new paper run. Only start begins
# one there.
set -euo pipefail

usage="usage: restore_state.sh DIR ACTION"
dir=${1:?$usage}
action=${2:?$usage}
branch=paper-state
output=${GITHUB_OUTPUT:-/dev/stdout}

if [ -e "$dir" ]; then
  echo "::error::$dir already exists. The state is restored into a new folder, so nothing left over can mix into it."
  exit 1
fi

# --exit-code makes a missing branch exit 2. Anything else is a network or permission problem, which must fail the job
# rather than pass for a paper run that hasn't been started.
found=0
git ls-remote --quiet --exit-code --heads origin "refs/heads/$branch" >/dev/null || found=$?

if [ "$found" -eq 0 ]; then
  # Depth 2, so the tip's parent is here too. save_state.sh amends the tip during the day, and amending a tip whose
  # parent a shallow fetch cut off would make a commit with no parent: the push would then wipe the daily history.
  git fetch --quiet --no-tags --depth=2 origin "+refs/heads/$branch:refs/remotes/origin/$branch"
  git worktree add --quiet -B "$branch" "$dir" "refs/remotes/origin/$branch"
  echo "Restored the paper state from $branch, $(TZ=UTC git -C "$dir" log -1 \
    --format='commit %h saved %cd' --date=format-local:'%Y-%m-%d %H:%M UTC')."
  new=false
  if [ ! -f "$dir/data/paper_systematic.db" ] && [ "$action" = start ]; then
    # As on a new branch, a start that fails here must not become the paper run.
    echo "$branch has no data/paper_systematic.db, so this starts a new paper run in $dir."
    new=true
  elif [ ! -f "$dir/data/paper_systematic.db" ]; then
    echo "::warning::$branch has no data/paper_systematic.db, so the tick will refuse to run rather than start a new" \
      "paper run on its own. If the run's database is really gone, run the workflow with action start to begin" \
      "a new one."
  fi
  echo "started=true" >> "$output"
  echo "new=$new" >> "$output"
elif [ "$found" -eq 2 ] && [ "$action" = start ]; then
  # An orphan branch: no history shared with main, and nothing on it but the state.
  git worktree add --quiet --detach --no-checkout "$dir"
  git -C "$dir" switch --quiet --orphan "$branch"
  mkdir -p "$dir/data"
  printf '*.db binary\n*.db-wal binary\n*.db-shm binary\n' > "$dir/.gitattributes"
  echo "There's no $branch branch yet, so this starts a new paper run in $dir."
  echo "started=true" >> "$output"
  echo "new=true" >> "$output"
elif [ "$found" -eq 2 ]; then
  echo "::notice title=Paper run not started::There's no $branch branch, so the paper run hasn't been started and" \
    "this run did nothing. To start it, run the Paper run workflow once with action start."
  echo "started=false" >> "$output"
  echo "new=false" >> "$output"
else
  echo "::error::Couldn't look for the $branch branch on GitHub (git ls-remote exited $found). Nothing was restored."
  exit 1
fi
