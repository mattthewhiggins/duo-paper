"""python -m duo watchdog: is the bot alive, and is the Mac on mains power? (audit finding 77)

The bot writes data/heartbeat_systematic.json on every loop. This reads it, asks macOS for the power source with
`pmset -g batt`, and posts a macOS notification with `osascript -e 'display notification ...'` when the heartbeat is
stale or missing, the runner is halted, it has been degraded (placing no new orders) for longer than
degraded_alert_minutes, today's decision is overdue, or the Mac is running on battery. An exchange outage keeps the
loop beating, so only the degraded and overdue checks would show it. It prints what it found, problems first, and exits
0 if all is well, 1 if not.

On the Mac it's meant to run every 5 minutes from launchd (ops/com.duo.watchdog.plist), outside the bot's own process,
so it still speaks when the bot can't. Elsewhere it runs on Linux, with no pmset or osascript, so the power isn't
checked and its exit code is the alert. On GitHub Actions it runs at the end of every run, and a failure fails the run,
which GitHub emails about. On Render, the service's supervisor (ops/render/serve.py) runs it after each rebuild of the
page, beside the loop, and its pass or fail, with its first line, is part of the service's /ok (RUNBOOK 13.4). It reads
and notifies, and changes nothing.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path


def power_source(run=subprocess.run) -> tuple[str, str]:
    """("ac", text), ("battery", text) or ("unknown", text), from pmset -g batt. ("none", text) where there's no pmset
    at all: not a Mac, such as GitHub's machines."""
    try:
        result = run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=10)
        text = result.stdout or ""
    except FileNotFoundError:
        return "none", "no pmset here, so this isn't a Mac"
    except (OSError, subprocess.SubprocessError) as error:
        return "unknown", f"pmset failed: {error}"
    if "'Battery Power'" in text:
        return "battery", text.strip()
    if "'AC Power'" in text:
        return "ac", text.strip()
    return "unknown", text.strip()


def battery_percent(text: str) -> int | None:
    match = re.search(r"(\d{1,3})%", text)
    return int(match.group(1)) if match else None


def heartbeat_age(path: Path, now: float) -> tuple[float | None, dict | None]:
    try:
        payload = json.loads(Path(path).read_text())
        return now - float(payload["ts"]), payload
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def _quoted(text: str) -> str:
    """For AppleScript's string literal. The text only ever comes from this module, but quote it properly anyway."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str, run=subprocess.run) -> None:
    script = f"display notification {_quoted(message)} with title {_quoted(title)}"
    try:
        run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def check(heartbeat_path: Path, stale_minutes: int, now: float | None = None, run=subprocess.run,
          notify_fn=None, degraded_minutes: float | None = None) -> tuple[bool, list[str]]:
    """Returns (all well, lines to print), and notifies about anything wrong."""
    now = time.time() if now is None else now
    notify_fn = notify_fn or (lambda title, message: notify(title, message, run))
    lines, problems = [], []
    age, payload = heartbeat_age(heartbeat_path, now)
    if age is None:
        problems.append(f"The bot is down: no readable heartbeat at {heartbeat_path}.")
    elif age > stale_minutes * 60:
        problems.append(f"The bot is down: its last heartbeat was {age / 60:.0f} minutes ago "
                        f"({payload.get('utc', 'unknown time')}).")
    else:
        lines.append(f"Bot alive: heartbeat {age:.0f}s ago, status {payload.get('status')}.")
        for halt in payload.get("halts") or []:
            lines.append(f"  {halt}")
        if payload.get("status") == "halted":
            problems.append("The bot is running but halted: " + "; ".join(payload.get("halts") or []))
        limit = degraded_minutes if degraded_minutes is not None else payload.get("degraded_alert_minutes") or 30
        since = payload.get("degraded_since")
        if payload.get("status") == "degraded" and since is not None:
            minutes = (now - float(since)) / 60
            if minutes > float(limit):
                problems.append(f"The bot is running but has placed no new orders for {minutes:.0f} minutes: "
                                + "; ".join(payload.get("halts") or ["degraded"]))
        overdue = payload.get("decision_overdue_minutes")
        if overdue is not None and float(overdue) > 0:
            problems.append(f"Today's daily decision is {float(overdue):.0f} minutes overdue (last decided bar: "
                            f"{payload.get('last_decision_bar') or 'none'}).")
    source, text = power_source(run)
    percent = battery_percent(text)
    if source == "battery":
        problems.append(f"The Mac is on battery{f' at {percent}%' if percent is not None else ''}. When it runs out, "
                        f"the bot stops with no alert.")
    elif source == "ac":
        lines.append("Power: on mains.")
    elif source == "none":
        lines.append("Power: not checked, as this machine isn't a Mac.")
    else:
        lines.append("Power: couldn't tell from pmset.")
    for problem in problems:
        notify_fn("Duo watchdog", problem)
    return not problems, problems + lines
