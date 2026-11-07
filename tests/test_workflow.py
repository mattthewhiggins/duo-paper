"""The GitHub Actions workflow (.github/workflows/paper.yml) against the code it runs, and the command line as typed.

The workflow, the command line and the status page were written apart, so these check where they meet: every
python -m duo command the workflow runs is one the command line takes, the exit code it reads as "no paper state" is the
one tick returns, the state folder it restores, ticks in and saves is the same one, the page it uploads is the folder
the site command writes, its schedule is the one the page counts runs against, and every action it uses is pinned to a
commit. There's no YAML parser here (PyYAML isn't a dependency), so the file is read as text.

The Tick and Save steps' own shell is run as the runner runs it, with bash -e, and with stand-ins for timeout, python
and save_state.sh that only say how they were called.

The Render backup workflow (.github/workflows/render-backup.yml), which copies the run from Render once the cutover has
disabled Paper run, is read the same way: it must queue with Paper run, use its actions at the same commits, never run
the bot or start a run, and keep Render's files out of data/ itself. Its steps' shell runs with stand-ins for gh,
python3 and curl.

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
BACKUP = WORKFLOW.parent / "render-backup.yml"


def step(text: str, name: str) -> str:
    """The run job's step called name, up to the blank line after it."""
    return re.search(rf"(?ms)^      - name: {re.escape(name)}\n(.*?)\n\n", text)[1]


def script(step_text: str) -> str:
    """A step's run:, as the runner writes it to a file."""
    run = re.search(r"(?m)^        run: (.*)\n?", step_text)
    if run[1] != "|":
        return run[1] + "\n"
    return "".join(line[10:] + "\n" for line in step_text[run.end():].splitlines())


def job(text: str, name: str) -> str:
    """The job called name, up to the next job (or the comment above it) or the end of the file."""
    return re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", text)[1]


def mapping(text: str, key: str, indent: int = 4) -> dict:
    """The block under key, at that indent, as {name: value}, without its comments."""
    lines = re.search(rf"(?m)^{' ' * indent}{re.escape(key)}:\n((?:{' ' * (indent + 2)}\S.*\n)+)", text)[1]
    return dict(line.split("#")[0].strip().split(": ", 1) for line in lines.splitlines())


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


class RenderBackup(unittest.TestCase):
    """The Render backup workflow. It saves the service's backups on paper-state once the cutover has disabled Paper
    run, so it must never be a second writer beside Paper run, start a run, or leave a database where it would tick."""

    def setUp(self):
        self.text = BACKUP.read_text()
        self.backup, self.pages = job(self.text, "backup"), job(self.text, "pages")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        tools = self.tmp / "bin"
        tools.mkdir()
        # gh prints Paper run's state, or fails as it would offline. curl writes the body where -o says and prints the
        # status code, 000 when nothing answered, as curl does. python3 only says how it was called.
        curl = ('while [ $# -gt 0 ]; do\n'
                '  if [ "$1" = -o ]; then shift; [ -z "$OK_BODY" ] || echo "$OK_BODY" > "$1"; fi\n'
                '  shift\n'
                'done\n'
                'printf %s "$OK_CODE"\n'
                '[ "$OK_CODE" != 000 ] || exit 7\n')
        for name, body in (("gh", '[ -z "$GH_EXIT" ] || exit "$GH_EXIT"\necho "$PAPER_STATE"\n'), ("curl", curl),
                           ("python3", 'exit "${PYTHON_EXIT:-0}"\n')):
            (tools / name).write_text(f'#!/bin/bash\necho "{name} $*" >> "$LOG"\n{body}')
            (tools / name).chmod(0o755)
        self.path = os.pathsep.join([str(tools), os.environ.get("PATH", "")])

    def run_step(self, name: str, **env) -> tuple[subprocess.CompletedProcess, list[str], dict]:
        """A step's shell, run as the runner runs it: its result, the stand-ins' calls and the outputs it set."""
        (self.tmp / "step.sh").write_text(script(step(self.text, name)))
        log, output = self.tmp / "calls.log", self.tmp / "output.txt"
        log.write_text("")
        output.write_text("")
        result = subprocess.run(["bash", "-e", "step.sh"], cwd=self.tmp, capture_output=True, text=True, timeout=60,
                                env={"PATH": self.path, "LOG": str(log), "GITHUB_OUTPUT": str(output), **env})
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
        return result, log.read_text().splitlines(), outputs

    def test_it_queues_with_paper_run_by_the_same_rules(self):
        # The same group, so the two never run at once. Without queue: max either could cancel the other's waiting run.
        block = r"(?m)^concurrency:\n((?:  .*\n)+)"
        self.assertEqual(re.search(block, self.text)[1], re.search(block, WORKFLOW.read_text())[1])

    def test_each_job_has_only_the_permissions_it_needs(self):
        self.assertRegex(self.text, r"(?m)^permissions: \{\}  #")
        self.assertEqual(re.findall(r"(?m)^  (\S+):$", self.text.split("\njobs:\n", 1)[1]), ["backup", "pages"])
        self.assertEqual(mapping(self.backup, "permissions"), {"contents": "write", "actions": "read"})
        self.assertEqual(mapping(self.pages, "permissions"),
                         {"contents": "read", "pages": "write", "id-token": "write"})
        self.assertEqual(mapping(self.pages, "environment")["name"], "github-pages")
        self.assertRegex(self.pages, r"(?m)^    needs: backup$")

    def test_a_job_that_checks_the_repository_out_may_read_it(self):
        # The checkout fetches with the job's own token, which has only what its permissions list (the top's are {}),
        # so a job that checks out needs contents, as GitHub's own Pages workflow gives its build.
        for name, text in (("backup", self.backup), ("pages", self.pages)):
            with self.subTest(job=name):
                self.assertIn("uses: actions/checkout@", text)
                self.assertIn(mapping(text, "permissions").get("contents"), ("read", "write"))

    def test_it_uses_paper_runs_actions_at_the_same_commits(self):
        pinned = dict(re.findall(r"(?m)uses: ([\w./-]+)@(\S+  # v[\d.]+)$", WORKFLOW.read_text()))
        used = re.findall(r"(?m)uses: ([\w./-]+)@(\S+  # v[\d.]+)$", self.text)
        self.assertEqual([name for name, _ in used], ["actions/checkout", "actions/checkout",
                                                      "actions/upload-pages-artifact", "actions/deploy-pages"])
        self.assertEqual(self.text.count("uses:"), len(used), "every action is pinned as Paper run pins it")
        for name, ref in used:
            self.assertEqual(ref, pinned[name], name)

    def test_it_runs_four_times_a_day_and_by_hand_with_the_page_to_choose(self):
        self.assertRegex(self.text, r"(?m)^name: Render backup$")
        self.assertEqual(re.findall(r"(?m)^\s+- cron: '([^']+)'$", self.text), ["23 3,9,15,21 * * *"])
        page = re.search(r"(?ms)^  workflow_dispatch:\n    inputs:\n      page:\n(.*?)\n\n", self.text)[1]
        self.assertIn("        type: choice\n", page)
        self.assertEqual([o.strip() for o in re.search(r"options: \[([^\]]+)\]", page)[1].split(",")],
                         ["moved", "final"])
        self.assertIn("        default: moved", page)
        self.assertEqual(mapping(self.text, "env", 0), {"RENDER_URL": "https://duo-paper.onrender.com", "TZ": "UTC"})

    def test_it_does_nothing_until_paper_run_is_disabled_by_hand(self):
        guard = step(self.text, "Check Paper run is disabled")
        self.assertIn('gh api "repos/$GITHUB_REPOSITORY/actions/workflows/paper.yml" --jq .state', guard)
        self.assertIn("GH_TOKEN: ${{ github.token }}", guard)
        self.assertIn("ran: ${{ steps.guard.outputs.ran }}", self.backup)
        # Straight after the checkout, and every step after it runs only once it has passed, as does the page's job.
        steps = [s for s in re.split(r"(?m)^(?=      - )", self.backup.split("    steps:\n", 1)[1]) if s.strip()]
        self.assertIn("id: guard", steps[1])
        for later in steps[2:]:
            self.assertRegex(later, r"(?m)^        if: (always\(\) && )?steps\.guard\.outputs\.ran == 'true'$")
        self.assertIn("needs.backup.outputs.ran == 'true'", re.search(r"(?m)^    if: (.*)$", self.pages)[1])

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_the_guard_passes_only_when_paper_run_was_disabled_by_hand(self):
        for state, ran in (("active", "false"), ("disabled_inactivity", "false"), ("disabled_manually", "true")):
            with self.subTest(state=state):
                result, calls, outputs = self.run_step("Check Paper run is disabled", PAPER_STATE=state,
                                                       GITHUB_REPOSITORY="someone/duo-paper")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls, ["gh api repos/someone/duo-paper/actions/workflows/paper.yml --jq .state"])
                self.assertEqual(outputs, {"ran": ran})
                self.assertEqual("::notice title=Paper run still runs the test::" in result.stdout, ran == "false")
        # Not knowing isn't a pass: a lookup that fails fails the job, and nothing after it runs.
        result, _, outputs = self.run_step("Check Paper run is disabled", GH_EXIT="1",
                                           GITHUB_REPOSITORY="someone/duo-paper")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outputs, {})

    def test_it_restores_and_saves_paper_state_but_never_runs_the_bot_or_starts_a_run(self):
        # restore_state.sh begins a new paper run only for start, and duo would write the database it reads.
        self.assertEqual(re.findall(r"restore_state\.sh (\S+) (\S+)", self.text), [("state", "backup")])
        self.assertEqual(re.findall(r"save_state\.sh (\S+)", self.text), ["state"])
        self.assertNotIn("python -m duo", self.text)
        self.assertNotRegex(self.text, r"-m duo\b|--first-run|--flatten")

    def test_renders_files_go_in_data_render_and_never_in_data_itself(self):
        # A Paper run that ran by mistake would tick data/paper_systematic.db, so there must be none.
        self.assertEqual(re.findall(r"state/data(?!/render\b)\S*", self.text), [])
        self.assertIn('git show "render-seed:data/paper_systematic.db" > "$RUNNER_TEMP/seed.db"',
                      step(self.text, "Fetch the seed"))

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_the_copy_holds_this_backup_and_nothing_older(self):
        backup = self.tmp / "runner" / "backup"
        backup.mkdir(parents=True)
        for name in ("paper_systematic.db", "manifest.json"):  # A backup with no heartbeat file.
            (backup / name).write_text(f"new {name}")
        data = self.tmp / "state" / "data"
        (data / "render").mkdir(parents=True)
        for name in ("paper_systematic.db", "manifest.json", "heartbeat_systematic.json"):
            (data / "render" / name).write_text(f"old {name}")
        (data / "MOVED_TO_RENDER.md").write_text("Moved.\n")
        result, _, _ = self.run_step("Copy it into the paper state", RUNNER_TEMP=str(self.tmp / "runner"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual({p.name: p.read_text() for p in (data / "render").iterdir()},
                         {"paper_systematic.db": "new paper_systematic.db", "manifest.json": "new manifest.json"})
        self.assertEqual(sorted(p.name for p in data.iterdir()), ["MOVED_TO_RENDER.md", "render"])

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_the_pull_is_checked_against_the_seed_and_the_last_copy(self):
        pull = step(self.text, "Pull the backup")
        self.assertIn("STARTED: ${{ steps.state.outputs.started }}", pull)
        self.assertIn("SEED_SHA: ${{ steps.seed.outputs.sha }}", pull)
        env = {"STARTED": "true", "SEED_SHA": "c" * 40, "RUNNER_TEMP": "/runner", "RENDER_URL": "https://render.test"}
        call = ("python3 ops/render/pull_backup.py pull https://render.test /runner/backup --seed-db /runner/seed.db "
                f"--seed-sha {'c' * 40}")
        result, calls, _ = self.run_step("Pull the backup", **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, [call])
        # Once a copy is saved, the next mustn't be older.
        (self.tmp / "state" / "data" / "render").mkdir(parents=True)
        (self.tmp / "state" / "data" / "render" / "manifest.json").write_text("{}")
        _, calls, _ = self.run_step("Pull the backup", **env)
        self.assertEqual(calls, [call + " --previous state/data/render/manifest.json"])
        result, _, _ = self.run_step("Pull the backup", PYTHON_EXIT="1", **env)
        self.assertEqual(result.returncode, 1)
        # With no paper-state there's nothing to save on, and the job doesn't make one.
        result, calls, _ = self.run_step("Pull the backup", **{**env, "STARTED": "false"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertIn("::error::There's no paper-state branch to save the backup on", result.stdout)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_the_moved_page_goes_up_only_after_a_pull_that_worked(self):
        # Else github.io could send readers to an address that didn't serve this run's backup. final runs whatever
        # the pull did: it copies the service's own page, at the end of the test.
        self.assertEqual(re.search(r"(?m)^    if: (.*)$", self.pages)[1],
                         "${{ !cancelled() && github.event_name == 'workflow_dispatch' && "
                         "needs.backup.outputs.ran == 'true' && (inputs.page == 'final' || "
                         "needs.backup.outputs.pulled == 'true') }}")
        self.assertEqual(mapping(self.backup, "outputs"),
                         {"ran": "${{ steps.guard.outputs.ran }}", "pulled": "${{ steps.pull.outputs.pulled }}"})
        self.assertRegex(step(self.text, "Pull the backup"), r"(?m)^        id: pull$")
        env = {"STARTED": "true", "SEED_SHA": "c" * 40, "RUNNER_TEMP": "/runner", "RENDER_URL": "https://render.test"}
        result, _, outputs = self.run_step("Pull the backup", PYTHON_EXIT="0", **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outputs, {"pulled": "true"})
        result, _, outputs = self.run_step("Pull the backup", PYTHON_EXIT="1", **env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(outputs, {})

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_a_bot_that_needs_you_fails_the_job_with_the_services_reason(self):
        env = {"RENDER_URL": "https://render.test", "RUNNER_TEMP": str(self.tmp)}
        result, calls, _ = self.run_step("Check the bot", OK_CODE="200", OK_BODY="ok", **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(calls[0], r"^curl .* https://render\.test/ok$")
        for code, body, says in (("503", "held", "https://render.test/ok answered 503: held."),
                                 ("000", "", "https://render.test/ok didn't answer, so the service may be down.")):
            with self.subTest(code=code):
                (self.tmp / "ok.txt").unlink(missing_ok=True)
                result, _, _ = self.run_step("Check the bot", OK_CODE=code, OK_BODY=body, **env)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("::error title=The bot needs you::" + says, result.stdout)
        # The body is whatever answers at RENDER_URL. A bare CR in it would end the line for the runner, and what came
        # after would run as a workflow command of this run's.
        (self.tmp / "ok.txt").unlink(missing_ok=True)
        result, _, _ = self.run_step("Check the bot", OK_CODE="503", OK_BODY="held\r::warning title=Injected::x 100%",
                                     **env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        commands = [line for line in re.split(r"[\r\n]", result.stdout) if line.startswith("::")]
        self.assertEqual(len(commands), 1, result.stdout)
        self.assertTrue(commands[0].startswith("::error title=The bot needs you::"), commands[0])
        self.assertIn("100%25", commands[0])
        # Last, and after a step that failed too, so the email says how the bot is whatever happened to its copy.
        self.assertEqual(re.findall(r"(?m)^      - name: (.+)$", self.backup)[-1], "Check the bot")
        self.assertIn("if: always() && steps.guard.outputs.ran == 'true'", step(self.text, "Check the bot"))

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_the_page_is_the_moved_one_unless_final_is_chosen_by_hand(self):
        for page, final in (("moved", ""), ("final", " --final")):
            with self.subTest(page=page):
                result, calls, _ = self.run_step("Build the page", PAGE=page, RENDER_URL="https://render.test")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls, [f"python3 ops/render/pull_backup.py page _site https://render.test{final}"])
        self.assertIn("PAGE: ${{ inputs.page }}", step(self.text, "Build the page"))
        self.assertEqual(re.search(r"(?m)^\s+path: (\S+)$", self.pages)[1], "_site")
        # A scheduled run never deploys, so it can't put the moved page back over the end-of-test copy.
        self.assertIn("github.event_name == 'workflow_dispatch'", re.search(r"(?m)^    if: (.*)$", self.pages)[1])


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
