#!/bin/zsh
# Sets everything up on first run, then starts Duo and keeps the Mac awake while it runs.
set -e
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "First run: installing dependencies..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  # requirements-research.txt is numpy, which the systematic runner needs.
  .venv/bin/pip install --quiet -r requirements.txt -r requirements-research.txt
fi

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
  else  # The public repository has no .env.example.
    print "# One setting per line, such as ANTHROPIC_API_KEY=... (README, \"Start it\")." > .env
  fi
  chmod 600 .env
  echo "Created .env. The agents need ANTHROPIC_API_KEY in it; the systematic runner doesn't. Run this again."
  exit 0
fi

# A run on battery stops with no alert when the battery runs out (audit finding 77).
if /usr/bin/pmset -g batt 2>/dev/null | grep -q "'Battery Power'"; then
  echo "WARNING: this Mac is on battery. When the battery runs out the bot stops, and nothing is checked or traded."
  echo "         Plug it in, and see README, \"Keeping it running\", for the watchdog that tells you when it's down."
fi

# -i stops idle sleep, -s stops system sleep while on mains power. Closing the lid can still sleep the Mac.
exec caffeinate -i -s .venv/bin/python -m duo run
