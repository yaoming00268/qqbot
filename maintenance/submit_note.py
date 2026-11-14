"""Submit one note using an explicitly supplied, current console context."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-file", required=True, help="Current QQBot console context file")
    parser.add_argument("--text-file", required=True, help="UTF-8 note file; never overwritten")
    parser.add_argument("--kind", choices=("observation", "plot", "reflection", "preference"), required=True)
    parser.add_argument("--evidence", action="append", required=True, help="Actual observation ID; repeat for each ID")
    parser.add_argument("--session-id", default="", help="Required when the context has no bound session")
    args = parser.parse_args(argv)
    from console_tools.cli import main as cli_main
    command = ["sandbox", "note", "--context-file", args.context_file,
               "--kind", args.kind, "--text-file", args.text_file]
    if args.session_id:
        command.extend(["--session-id", args.session_id])
    for evidence in args.evidence:
        command.extend(["--evidence", evidence])
    try:
        cli_main(command)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
