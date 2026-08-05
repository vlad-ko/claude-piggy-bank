"""Claude Piggy Bank's single entry point: `cpb <command> [options]` (#21).

Usage:
    python3 cpb.py ingest [--projects-dir DIR] [--db PATH] [--prune-missing]
    python3 cpb.py serve  [--db PATH] [--port 8377]
    python3 cpb.py --version

Before this, the tool was two scripts invoked by filename -- `python3
ingest.py`, `python3 serve.py` -- which names an implementation file rather
than the tool, and asks the reader to know which of the two files in the
directory is the one they want. #21's dev-stack requirement is one entry point
with subcommands.

**It composes the two scripts; it does not replace them.** Each keeps its own
`argparse` parser, so every documented flag keeps working with one definition
and one help text, and `python3 ingest.py` keeps working for anyone who has it
in a script. All this module does is choose a module, install the remaining
arguments as that module's `sys.argv`, and reproduce its exit status. There is
deliberately no flag here that a subcommand also owns: after the subcommand,
every token is the subcommand's, including `--help` and `--version`.

The exit status is the load-bearing part. `ingest.py` and `serve.py` refuse by
raising `SystemExit` -- with a message for a missing database, an empty
`CPB_DB`, a projects directory that does not exist, and with argparse's 2 for a
bad flag combination. A wrapper that returned 0 anyway, or dropped the message,
would report a run that measured nothing as a run that measured zero, which is
the failure this project exists to not commit. `_exit_status()` therefore
reproduces CPython's own handling of `SystemExit` exactly, and an exception
that is *not* `SystemExit` is left to propagate as a traceback: a defect is not
a status code.

The version lives here, in code, because that is what answers `--version`
without reading a file that a future single-file distribution may not carry
alongside it. `.claude-plugin/plugin.json` repeats it as a literal because
Claude Code's plugin loader reads that JSON without running any Python;
`tests/test_cpb.py` pins the two equal so they cannot drift.
"""

from __future__ import annotations

import importlib
import sys
from typing import NamedTuple, Optional, Sequence

# The version of CPB itself -- what a user names when they say which build
# produced a number. Distinct from ingest.py's SCHEMA_VERSION, which describes
# the database's shape and says nothing about the code that filled it.
VERSION = "0.1.0"

PROG = "cpb"

# argparse's convention, which both subcommands already exit with for a bad
# argument: 2 is a usage error, 1 is a run that started and failed.
USAGE_EXIT_STATUS = 2

HELP_FLAGS = ("-h", "--help")
VERSION_FLAGS = ("--version",)


class Command(NamedTuple):
    """A subcommand: the module that owns it, and one line for the usage text."""

    module: str
    summary: str


# The dispatch table is the single place a subcommand's module is named, and
# the source of the command list in `usage()` -- a command that can be
# dispatched is a command that is documented.
COMMANDS: dict[str, Command] = {
    "ingest": Command(
        module="ingest",
        summary="Read Claude Code's transcripts on this machine into SQLite.",
    ),
    "serve": Command(
        module="serve",
        summary="Serve the local report over an ingested database.",
    ),
}


def usage() -> str:
    """The usage text, with the command list generated from `COMMANDS`."""
    width = max(len(name) for name in COMMANDS)
    lines = [
        f"usage: {PROG} <command> [options]",
        "",
        "Claude Piggy Bank -- where this project's context and token spend went.",
        "",
        "commands:",
    ]
    lines += [
        f"  {name.ljust(width)}  {command.summary}" for name, command in COMMANDS.items()
    ]
    lines += [
        "",
        "options:",
        f"  {'-h, --help'.ljust(width + 10)}  Show this message and exit.",
        f"  {'--version'.ljust(width + 10)}  Show the CPB version and exit.",
        "",
        "Everything after the command is passed to it unchanged, so every flag",
        f"documented for that command works here too: `{PROG} <command> --help`.",
    ]
    return "\n".join(lines) + "\n"


def _exit_status(code: object) -> int:
    """Reproduce CPython's handling of a `SystemExit` raised by a subcommand.

    The interpreter treats `SystemExit(None)` as 0, an int as itself, and
    anything else as a message printed to stderr followed by status 1 -- which
    is the form both scripts use for their refusals ("DB not found: ... --
    run ingest.py first"). Catching the exception here takes that handling away
    from the interpreter, so it has to be done, and done identically: losing
    the message would leave a bare non-zero status with no reason attached.
    """
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def _dispatch(command: Command, prog: str, argv: Sequence[str]) -> int:
    """Run `command`'s module `main()` with `argv`, and return its exit status.

    The module is imported here rather than at the top of this file so that
    `--version` and a usage error answer without loading either script, and a
    subcommand is only paid for when it is the one being run.

    `sys.argv[0]` becomes "cpb <command>" because argparse renders it in the
    sub-parser's usage line and in its error messages; left alone it would tell
    the user to re-run something they did not type. It is restored afterwards
    -- including when the subcommand raises -- because in-process callers (the
    test suite, a future `cpb` that runs two commands) must not inherit it.
    """
    module = importlib.import_module(command.module)
    original_argv = sys.argv
    sys.argv = [prog, *argv]
    try:
        module.main()
    except SystemExit as exc:
        return _exit_status(exc.code)
    finally:
        sys.argv = original_argv
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Dispatch one subcommand; return the status the process should exit with."""
    args = list(sys.argv[1:] if argv is None else argv)

    if not args:
        # No command is a usage error, not a default action: neither reading
        # the whole transcript corpus nor opening a port is something to do
        # because nothing was asked for.
        print(usage(), end="", file=sys.stderr)
        return USAGE_EXIT_STATUS

    head, rest = args[0], args[1:]
    if head in HELP_FLAGS:
        print(usage(), end="")
        return 0
    if head in VERSION_FLAGS:
        print(f"{PROG} {VERSION}")
        return 0
    if head not in COMMANDS:
        print(f"{PROG}: unknown command: {head}", file=sys.stderr)
        print(usage(), end="", file=sys.stderr)
        return USAGE_EXIT_STATUS

    return _dispatch(COMMANDS[head], f"{PROG} {head}", rest)


if __name__ == "__main__":
    sys.exit(main())
