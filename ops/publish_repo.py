"""python ops/publish_repo.py TARGET_DIR: copies what the public repository needs into TARGET_DIR, after checking it.

The public repository (mattthewhiggins/duo-paper on GitHub) runs the paper test on GitHub Actions
(.github/workflows/paper.yml). It gets the files on WHITELIST and nothing else: no data/, no .env, no .venv, and none of
the research write-ups, results or audit, which are kept privately.

The copy is made in a temporary folder and checked there first. If any file is called .env, isn't UTF-8 text, or holds
a path into a Mac's home folders (under /Users, or with dashes for slashes, as Claude names its session folders), an
email address or something that looks like a key, it stops, naming the file and line, and TARGET_DIR is left as it was.
So does a file on the list that's a link, since what it points to would be published. The only addresses allowed are
GitHub's users.noreply.github.com ones and those at the names reserved for examples (example.com, .test and the like),
which can't be anyone's.

Otherwise TARGET_DIR ends up with exactly those files plus a .gitignore: files there that are no longer on the list are
deleted, and only .git is left alone. So that a typo can't empty some other folder, TARGET_DIR must be new, empty or a
git clone, and outside this project.

It never runs git. Committing and pushing TARGET_DIR is up to you.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

WHITELIST = (
    "duo/*.py", "duo/research_breaks.json", "duo/static/*.html",
    # Every .py under harness/ and strategies/, imported or not: each decision records a hash of all of them, and it
    # matches the research's own runs only with none missing.
    "research/__init__.py", "research/harness/**/*.py", "research/strategies/**/*.py",
    "research/tools/__init__.py", "research/tools/fetch_binance.py",
    "research/artifacts/cost_model.json", "research/artifacts/cost_model.sha256",
    "tests/__init__.py", "tests/helpers.py", "tests/sys_helpers.py", "tests/test_*.py",
    "config.toml", "requirements.txt", "requirements-research.txt", "requirements-paper.txt",
    "README.md", "RUNBOOK.md", "start.sh",
    "ops/actions/*.sh", "ops/publish_repo.py", ".github/workflows/paper.yml",
)

GITIGNORE = "data/\n.env\n.venv/\n__pycache__/\n*.pyc\n_site/\nstate/\n"

# The patterns are written so that they don't match this file, which is published too.
USERS_PATH = re.compile(r"/Users(?=/)\S*")
FLAT_USERS_PATH = re.compile(r"-Users-[A-Za-z0-9._]+-")  # The same path with dashes, as in a Claude session folder.
EMAIL = re.compile(r"[\w.%+-]+@([\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,})")
# GitHub's no-reply addresses, and the names reserved for examples and tests (RFC 2606).
ALLOWED_DOMAINS = ("users.noreply.github.com", "example.com", "example.net", "example.org")
RESERVED_ENDINGS = (".example", ".test", ".invalid", ".localhost")
KEYS = (
    re.compile(r"sk-ant-[\w-]{12,}"),  # Anthropic
    re.compile(r"\b(?:KRAKEN_API_KEY|KRAKEN_API_SECRET|ANTHROPIC_API_KEY|DUO_DASHBOARD_TOKEN)['\"]?\s*[:=]\s*['\"]?"
               r"[\w+/-]{12,}"),  # One of this project's settings with a value, as in .env
    re.compile(r"\bgh[pousr]_\w{30,}|\bgithub_pat_\w{30,}"),  # GitHub
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class NotPublished(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


def whitelisted(root: Path, patterns=WHITELIST) -> list[str]:
    """The files to publish, relative to root. A pattern that matches nothing is a problem: the copy would be broken.
    So is a link, or a file reached through one: what it points to, which could be outside the project, would be
    published in its place."""
    files, problems = set(), []
    real_root = root.resolve()
    for pattern in patterns:
        matched = list(root.glob(pattern))
        links = [path for path in matched if path.is_symlink() or not path.resolve().is_relative_to(real_root)]
        problems += [f"{path.relative_to(root).as_posix()} is a link, or inside one, so what it points to would be "
                     f"published" for path in links]
        found = {path.relative_to(root).as_posix() for path in matched if path.is_file() and path not in links}
        if not found and not links:
            problems.append(f"nothing in {root.name} matches {pattern}")
        files |= found
    if problems:
        raise NotPublished(problems)
    return sorted(files)


def scan(folder: Path) -> list[str]:
    """What must never be published, as 'file:line: what' lines. Keys are shown cut short, so they aren't repeated."""
    problems = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        name = path.relative_to(folder).as_posix()
        if path.name == ".env":
            problems.append(f"{name}: a .env file, which holds keys")
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as error:  # UTF-16, say, would hide an address from every check below.
            problems.append(f"{name}: not UTF-8 text ({error.reason} at byte {error.start}), so it couldn't be checked")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for match in USERS_PATH.finditer(line):
                problems.append(f"{name}:{number}: a path on this Mac, {match.group()}")
            for match in FLAT_USERS_PATH.finditer(line):
                problems.append(f"{name}:{number}: a path on this Mac, written with dashes, {match.group()}")
            for match in EMAIL.finditer(line):
                domain = match.group(1).lower()
                if domain not in ALLOWED_DOMAINS and not domain.endswith(RESERVED_ENDINGS):
                    problems.append(f"{name}:{number}: an email address, {match.group()}")
            for pattern in KEYS:
                for match in pattern.finditer(line):
                    problems.append(f"{name}:{number}: what looks like a key, {match.group()[:12]}...")
    return problems


def check_target(target: Path, root: Path) -> None:
    if target == root or root in target.parents or target in root.parents:
        raise NotPublished([f"{target} overlaps this project, and old files there would be deleted. "
                            f"Use a folder outside it, such as a clone of the public repository."])
    if target.exists() and not target.is_dir():
        raise NotPublished([f"{target} isn't a folder."])
    if target.is_dir() and any(target.iterdir()) and not (target / ".git").exists():
        raise NotPublished([f"{target} has files in it but isn't a git clone, so it may not be the public repository, "
                            f"and anything not on the list would be deleted. Use a clone of it or a new folder."])


def sync(stage: Path, target: Path) -> list[str]:
    """Makes target hold exactly stage's files, leaving .git alone. Returns the files it deleted."""
    wanted = {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()}
    removed = []
    for folder, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files + [d for d in dirs if (Path(folder) / d).is_symlink()]:
            path = Path(folder) / name
            relative = path.relative_to(target).as_posix()
            if name != ".git" and relative not in wanted:
                path.unlink()
                removed.append(relative)
    for folder, _, _ in sorted(os.walk(target), key=lambda entry: -len(entry[0])):
        path = Path(folder)
        if path != target and ".git" not in path.relative_to(target).parts and not any(path.iterdir()):
            path.rmdir()
    for relative in sorted(wanted):
        source, destination = stage / relative, target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Replaced, never written over: copying onto a link would write wherever it points, and a read-only file (the
        # frozen cost model is read-only in the project) couldn't be written over by the next publish.
        if destination.is_symlink() or destination.exists():
            destination.unlink()
        shutil.copyfile(source, destination)
        # git keeps only whether a file is executable, so that's all that's carried over: the scripts stay executable,
        # and everything else is read-write, as a clone of the repository has it.
        destination.chmod(0o755 if source.stat().st_mode & stat.S_IXUSR else 0o644)
    return sorted(removed)


def publish(root: Path, target: Path, patterns=WHITELIST) -> tuple[list[str], list[str]]:
    """Returns (files published, files deleted from target). Raises NotPublished, with target untouched, if anything
    is wrong."""
    root, target = root.resolve(), target.resolve()
    check_target(target, root)
    files = whitelisted(root, patterns)
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary)
        for relative in files:
            (stage / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / relative, stage / relative)
        (stage / ".gitignore").write_text(GITIGNORE)
        problems = scan(stage)
        if problems:
            raise NotPublished(problems)
        target.mkdir(parents=True, exist_ok=True)
        removed = sync(stage, target)
    return files + [".gitignore"], removed


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="publish_repo.py", description="Copies what the public repository needs into "
                                     "TARGET_DIR, after checking it holds no keys, emails or paths on this Mac.")
    parser.add_argument("target", metavar="TARGET_DIR", help="a clone of the public repository, or a new folder")
    args = parser.parse_args(argv)
    try:
        files, removed = publish(ROOT, Path(args.target))
    except NotPublished as error:
        sys.exit("Not published. Nothing in the target folder was changed:\n- " + "\n- ".join(error.problems))
    print(f"Copied {len(files)} files into {args.target}, and checked them: no links, all UTF-8 text, no .env file, "
          f"no path under /Users (with slashes or dashes), no one's email address and nothing that looks like a key.")
    if removed:
        print(f"Deleted {len(removed)} that are no longer published: {', '.join(removed)}")
    print("Nothing was committed. Look it over with git status there, then commit and push it yourself.")


if __name__ == "__main__":
    main()
