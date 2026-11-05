"""The GitHub Actions workflow (.github/workflows/paper.yml) against the code it runs, and the command line as typed.

The workflow, the command line and the status page were written apart, so these check where they meet: every
python -m duo command the workflow runs is one the command line takes, the exit code it reads as "no paper state" is the
one tick returns, the state folder it restores, ticks in and saves is the same one, the page it uploads is the folder
the site command writes, its schedule is the one the page counts runs against, and every action it uses is pinned to a
commit. There's no YAML parser here (PyYAML isn't a dependency), so the file is read as text.

The Tick and Save steps' own shell is run as the runner runs it, with bash -e, and with stand-ins for timeout, python
and save_state.sh that only say how they were called.

Then each command that reads a paper run is run through main() as typed, on a run that tick_once made with the
stand-ins: a name in main() that shadows a command's function would only show there. No network, and .env is never read:
load_env is patched out, and tick and probe, which would reach the exchanges, aren't run this way.
"""
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from duo import __main__ as cli
from duo import config as config_module
from duo import site

from .sys_helpers import D0, DAY, FakeBinance, PathMarket, flat, make_sys_config, rows_close_before, series

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "paper.yml"
REQUIREMENTS = WORKFLOW.parents[2] / "requirements-paper.txt"


def step(text: str, name: str) -> str:
    """The run job's step called name, up to the blank line after it."""
    return re.search(rf"(?ms)^      - name: {re.escape(name)}\n(.*?)\n\n", text)[1]


def script(step_text: str) -> str:
    """A step's run:, as the runner writes it to a file."""
    run = re.search(r"(?m)^        run: (.*)\n?", step_text)
    if run[1] != "|":
        return run[1] + "\n"
    return "".join(line[10:] + "\n" for line in step_text[run.end():].splitlines())


class Workflow(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_every_duo_command_it_runs_is_on_the_command_line(self):
        commands = [shlex.split(c) for c in re.findall(r"python -m duo ([^;&|\n]+)", self.text)]
        self.assertEqual({c[0] for c in commands}, {"probe", "tick", "resume", "site", "watchdog"})
        for args in commands:
            with self.subTest(args=args):
                cli.command_line().parse_args(args)  # Exits with a usage error if a command or option isn't there.

    def test_every_action_you_can_pick_is_handled(self):
        options = [o.strip() for o in re.search(r"options: \[([^\]]+)\]", self.text)[1].split(",")]
        self.assertEqual(options, ["tick", "start", "resume", "flatten", "probe"])
        for action in options[:-1]:  # probe has a step of its own.
            self.assertRegex(self.text, rf"(?m)^\s+{action}\) python -m duo ")
        self.assertIn("if: env.ACTION == 'probe'", self.text)

    def test_no_paper_state_is_ticks_own_exit_code(self):
        self.assertEqual(int(re.search(r'"\$code" -eq (\d+)', self.text)[1]), cli.NO_RUN)

    def test_the_state_it_restores_is_where_tick_writes_and_what_it_saves(self):
        data_dir = re.search(r"DUO_DATA_DIR: \$\{\{ github\.workspace \}\}/(\S+)/data\b", self.text)[1]
        self.assertEqual(re.search(r"restore_state\.sh (\S+) ", self.text)[1], data_dir)
        self.assertEqual(re.search(r"save_state\.sh (\S+)", self.text)[1], data_dir)

    def test_the_page_it_uploads_is_the_one_site_writes(self):
        self.assertEqual(re.search(r"python -m duo site --out (\S+)", self.text)[1],
                         re.search(r"(?m)^\s+path: (\S+)$", self.text)[1])

    def test_its_schedule_is_the_one_the_page_counts(self):
        crons = tuple(re.findall(r"(?m)^\s+- cron: '([^']+)'$", self.text))
        self.assertEqual(crons, site.SCHEDULE)

    def test_every_action_it_uses_is_pinned_to_a_commit(self):
        used = re.findall(r"uses: ([\w./-]+)@(\S+)", self.text)
        self.assertEqual(len(used), 4)
        for name, ref in used:
            self.assertRegex(ref, r"^[0-9a-f]{40}$", name)

    def test_what_it_installs_is_pinned_by_hash(self):
        # numpy runs in the job that can push: a wheel added to the release later, or a changed cached one, mustn't.
        self.assertIn("pip install -r requirements-paper.txt", self.text)
        pins = [line for line in REQUIREMENTS.read_text().splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(pins), 1)
        self.assertRegex(pins[0], r"^numpy==[\d.]+ --hash=sha256:[0-9a-f]{64}$")

    def test_a_run_that_waits_its_turn_is_never_cancelled(self):
        # Without queue: max, GitHub cancels a waiting start, flatten or resume when another run queues behind it.
        block = re.search(r"(?m)^concurrency:\n((?:  .*\n)+)", self.text)[1]
        self.assertEqual(dict(line.strip().split(": ") for line in block.splitlines()),
                         {"group": "paper-run", "cancel-in-progress": "false", "queue": "max"})

    def test_python_s_own_timeout_runs_out_before_the_step_s_and_the_step_s_before_the_job_s(self):
        # The step's timeout signals only its bash, which would leave python writing while the state is saved.
        call = re.search(r"timeout --foreground --signal=INT --kill-after=(\d+)s (\d+)s python", self.text)
        self.assertTrue(call, "python has no timeout of its own")
        kill_after, seconds = map(int, call.groups())
        tick = int(re.search(r"(?m)^        timeout-minutes: (\d+)", step(self.text, "Tick"))[1])
        job = int(re.search(r"(?m)^  run:\n(?:    .*\n)*?    timeout-minutes: (\d+)", self.text)[1])
        self.assertLess(seconds + kill_after, tick * 60)
        self.assertLess(tick, job)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class Steps(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        tools = self.tmp / "bin"
        tools.mkdir()
        # timeout then runs the rest, as coreutils' does, or exits TIMED_OUT, as one that ran out does: 124 when SIGINT
        # stopped python, 137 when SIGKILL had to.
        for name, body in (("timeout", '[ -z "$TIMED_OUT" ] || exit "$TIMED_OUT"\n'
                                       'while [ $# -gt 0 ] && [ "$1" != python ]; do shift; done\nexec "$@"\n'),
                           ("python", 'exit "${PYTHON_EXIT:-0}"\n')):
            (tools / name).write_text(f'#!/bin/bash\necho "{name} $*" >> "$LOG"\n{body}')
            (tools / name).chmod(0o755)
        self.path = os.pathsep.join([str(tools), os.environ.get("PATH", "")])

    def run_step(self, name: str, **env) -> tuple[subprocess.CompletedProcess, list[str]]:
        (self.tmp / "step.sh").write_text(script(step(self.text, name)))
        log = self.tmp / "calls.log"
        log.write_text("")
        result = subprocess.run(["bash", "-e", "step.sh"], cwd=self.tmp, capture_output=True, text=True, timeout=60,
                                env={"PATH": self.path, "LOG": str(log), **env})
        return result, log.read_text().splitlines()

    def test_every_python_the_tick_runs_has_a_timeout_of_its_own(self):
        # SIGINT, so the tick closes its database on the way out. --foreground, so it comes once: by default timeout
        # signals its process group too, python included, and a second KeyboardInterrupt can land in the closing.
        timeout = "timeout --foreground --signal=INT --kill-after=20s 270s"
        for action, commands in (("tick", ["tick"]), ("start", ["tick --first-run"]), ("flatten", ["tick --flatten"]),
                                 ("resume", ["resume", "tick"])):
            with self.subTest(action=action):
                result, calls = self.run_step("Tick", ACTION=action)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls, [call for command in commands for call in (
                    f"{timeout} python -m duo {command}", f"python -m duo {command}")])

    def test_a_tick_that_runs_out_of_time_fails_and_no_paper_state_still_says_so(self):
        for code in (124, 137):
            with self.subTest(code=code):
                result, _ = self.run_step("Tick", ACTION="tick", TIMED_OUT=str(code))
                self.assertEqual(result.returncode, code)
                self.assertNotIn("No paper state", result.stdout)
        result, _ = self.run_step("Tick", ACTION="tick", PYTHON_EXIT=str(cli.NO_RUN))
        self.assertEqual(result.returncode, cli.NO_RUN)
        self.assertIn("::error title=No paper state::", result.stdout)

    def test_a_start_that_was_not_saved_says_to_run_start_again(self):
        # Its tick worked, but paper-state never got it: later runs would only say the paper run hasn't started.
        save = self.tmp / "ops" / "actions" / "save_state.sh"
        save.parent.mkdir(parents=True)
        save.write_text('exit "$SAVE_EXIT"\n')
        error = ("::error::The start wasn't saved, so the paper run hasn't begun. Run the workflow with action start "
                 "again.")
        for new, saved, fails, says in (("true", 1, True, True), ("false", 1, True, False), ("true", 0, False, False)):
            with self.subTest(new=new, save_exit=saved):
                result, _ = self.run_step("Save the paper state", NEW=new, SAVE_EXIT=str(saved))
                self.assertEqual(result.returncode != 0, fails, result.stdout + result.stderr)
                self.assertEqual(error in result.stdout, says, result.stdout)
        self.assertIn("NEW: ${{ steps.state.outputs.new }}", step(self.text, "Save the paper state"))


class CommandLine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = make_sys_config()
        last = D0 + 10 * DAY
        bars = {"BTC": series(20000.0, last, 710), "ETH": series(1500.0, last, 710)}
        prices = {a: rows_close_before(rows, D0) for a, rows in bars.items()}
        out = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=False) as environ, mock.patch.object(sys, "stdout", out):
            environ.pop("GITHUB_STEP_SUMMARY", None)
            for now, first in ((D0 + 300, True), (D0 + 1200, False)):
                code = cli.tick_once(cls.config, market=PathMarket(flat(prices), now), binance=FakeBinance(bars, now),
                                     now=now, first_run=first, sleep=lambda seconds: None)
                assert code == 0, out.getvalue()

    def main(self, *argv) -> tuple[int, str]:
        """main() with argv as typed, on this run's database. A Mac's power and notifications are left out."""
        out = io.TextIOWrapper(io.BytesIO(), write_through=True)  # main() reconfigures stdout: a StringIO can't be.
        with mock.patch.object(config_module, "load_env") as load_env, \
                mock.patch.object(config_module, "load", return_value=self.config), \
                mock.patch.object(sys, "argv", ["duo", *argv]), mock.patch.object(sys, "stdout", out), \
                mock.patch("duo.watchdog.power_source", return_value=("none", "not a Mac")), \
                mock.patch("duo.watchdog.notify"):
            try:
                cli.main()
                code = 0
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else 1
        load_env.assert_called_once()
        return code, out.buffer.getvalue().decode()

    def test_status(self):
        code, out = self.main("status")
        self.assertEqual(code, 0, out)
        self.assertIn("Strategy    F1__ENS__BTC_ETH__vol40", out)
        self.assertIn("Decision    bar of 2026-11-01", out)

    def test_decisions_and_compare(self):
        code, out = self.main("decisions", "--out", "-")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out.splitlines()[0])["bar_utc"], "2026-11-01 00:00:00 UTC")
        report = Path(tempfile.mkdtemp()) / "compare.md"
        code, out = self.main("compare", "--out", str(report))
        self.assertEqual(code, 0, out)
        self.assertTrue(report.read_text())

    def test_site(self):
        folder = Path(tempfile.mkdtemp()) / "site"
        code, out = self.main("site", "--out", str(folder))
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), f"Status page written to {folder}.", "and no part failed")
        self.assertEqual(json.loads((folder / "status.json").read_text())["heartbeat"]["schedule"], "cron")

    def test_watchdog_and_resume(self):
        code, out = self.main("watchdog")
        self.assertEqual(code, 1, "the stand-ins' heartbeat is from 2 November 2026, so it's long stale")
        self.assertIn("Power: not checked, as this machine isn't a Mac.", out)
        code, out = self.main("resume")
        self.assertEqual(code, 0, out)
        self.assertIn("isn't halted", out)


if __name__ == "__main__":
    unittest.main()
