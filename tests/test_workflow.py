"""The GitHub Actions workflow (.github/workflows/paper.yml) against the code it runs, and the command line as typed.

The workflow, the command line and the status page were written apart, so these check where they meet: every
python -m duo command the workflow runs is one the command line takes, the exit code it reads as "no paper state" is the
one tick returns, the state folder it restores, ticks in and saves is the same one, the page it uploads is the folder
the site command writes, its schedule is the one the page counts runs against, and every action it uses is pinned to a
commit. There's no YAML parser here (PyYAML isn't a dependency), so the file is read as text.

Then each command that reads a paper run is run through main() as typed, on a run that tick_once made with the
stand-ins: a name in main() that shadows a command's function would only show there. No network, and .env is never read:
load_env is patched out, and tick and probe, which would reach the exchanges, aren't run this way.
"""
import io
import json
import re
import shlex
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
