"""The ERAYA x BRAINWAVE command line.

    python -m app.cli simulate          replay a full 402 flow offline
    python -m app.cli doctor            x402 conformance + ledger integrity
    python -m app.cli close_batch       close a session batch (DRY RUN by default)
    python -m app.cli seed_demo         populate the ledger with labelled demo data

WHY ARGPARSE AND NOT TYPER OR CLICK
-----------------------------------
`argparse` is standard library. The brief allowed either, and adding a CLI
framework would mean one more pinned version in `requirements.txt` for a
payment gateway to install at deploy time, in exchange for slightly prettier
`--help`. The commands here need subcommands, typed flags and a `--json` mode;
argparse does all three. Zero new dependencies is the better trade.

Everything the CLI touches is pure Python for the same reason the rest of the
project is: `x402.mechanisms.evm.signers` signs EIP-712 through `eth_account`,
so there is no Node, no npm and no TypeScript shim anywhere in this repository.

STRUCTURE
---------
Each command module exposes exactly two things:

    add_arguments(parser)    -> declare its flags
    run(args, printer) -> int -> do the work, return an exit code

so a command can be driven from a test without a subprocess:

    from app.cli import invoke
    exit_code, payload = invoke("doctor", ["--json"])
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from typing import Any

from app.cli._out import Printer

__all__ = ["main", "build_parser", "invoke", "COMMANDS"]

#: name -> (module, one-line help). Underscore names are canonical because the
#: brief names the files `close_batch.py` / `seed_demo.py`; the hyphenated
#: spellings are registered as aliases so muscle memory works either way.
COMMANDS: dict[str, tuple[str, str]] = {
    "simulate": (
        "app.cli.simulate",
        "replay a full x402 402 flow offline -- no network, no funds, real signatures",
    ),
    "doctor": (
        "app.cli.doctor",
        "x402 conformance and ledger integrity: requirements, receipts, _meta keys, nonces",
    ),
    "close_batch": (
        "app.cli.close_batch",
        "close a session batch and record settlement (DRY RUN unless --live)",
    ),
    "seed_demo": (
        "app.cli.seed_demo",
        "populate the ledger with demo data; every row carries is_demo=True",
    ),
}

ALIASES = {"close-batch": "close_batch", "seed-demo": "seed_demo"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description=(
            "ERAYA x BRAINWAVE -- MCP won the tool layer. This is its payment layer. "
            "Every command runs offline unless it says otherwise; nothing here deploys."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Start with:  python -m app.cli simulate\n"
            "It needs no server, no database rows, no .env and no funds."
        ),
    )
    _add_global_flags(parser)

    # The same two flags are attached to every subcommand as well, so both
    # `cli --json doctor` and `cli doctor --json` work. argparse normally makes
    # the second form clobber the first, because a subparser writes its own
    # defaults over the namespace the parent already filled in. `SUPPRESS` on
    # the subcommand copies stops that: the flag lands in the namespace only
    # when it was actually typed.
    common = argparse.ArgumentParser(add_help=False)
    _add_global_flags(common, suppress=True)

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, (module_path, help_text) in COMMANDS.items():
        aliases = [alias for alias, target in ALIASES.items() if target == name]
        module = importlib.import_module(module_path)
        sub = subparsers.add_parser(
            name,
            aliases=aliases,
            parents=[common],
            help=help_text,
            description=(module.__doc__ or help_text).strip(),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        module.add_arguments(sub)
        sub.set_defaults(_module=module_path, _command=name)
    return parser


def _add_global_flags(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    default = argparse.SUPPRESS if suppress else False
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=default,
        help="disable ANSI colour (also honours NO_COLOR)",
    )
    parser.add_argument(
        "--json",
        dest="json_mode",
        action="store_true",
        default=default,
        help="emit one JSON document instead of human output",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "_module", None):
        parser.print_help()
        return 2

    no_color = getattr(args, "no_color", False)
    json_mode = getattr(args, "json_mode", False)
    args.json_mode = json_mode  # normalise for the command modules
    printer = Printer(color=False if no_color else None, json_mode=json_mode)
    module = importlib.import_module(args._module)
    try:
        return int(module.run(args, printer))
    except KeyboardInterrupt:
        printer.raw()
        printer.warn("interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001 - the CLI is the top of the stack
        if args.json_mode:
            print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, indent=2))
        else:
            printer.raw()
            printer.fail(f"{type(exc).__name__}: {exc}")
            printer.detail("re-run with --json for a machine-readable error, or set DEBUG=true")
        if str(sys.argv[0]).endswith("pytest"):  # pragma: no cover - debugging aid
            raise
        return 1


def invoke(command: str, argv: list[str] | None = None) -> tuple[int, Any]:
    """Run a command in-process and capture its `--json` document.

    Used by the tests so the CLI is exercised through exactly the entry point a
    human uses, without paying for a subprocess per assertion.
    """
    import contextlib
    import io

    buffer = io.StringIO()
    full = [command, *(argv or [])]
    if "--json" not in full:
        full = ["--json", *full]
    else:
        full = [a for a in full if a != "--json"]
        full = ["--json", *full]
    with contextlib.redirect_stdout(buffer):
        code = main(full)
    text = buffer.getvalue().strip()
    try:
        return code, json.loads(text) if text else None
    except json.JSONDecodeError:
        return code, text
