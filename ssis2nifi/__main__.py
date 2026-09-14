"""ssis2nifi -- read a real SSIS package and say what it does.

Exit codes are part of the contract, because this runs in CI:

    0   everything in the package has a conversion rule
    3   parsed, but something needs a human (unsupported component, refused component)
    4   refused: not a genuine .dtsx, or malformed beyond use

3 is deliberately non-zero.  A pipeline must not be able to ship a
half-translated package by accident, and "it printed a warning" is not a gate.
"""

from __future__ import annotations

import argparse
import json
import sys

from .catalog.support import annotate
from .dtsx.parse import NotADtsxPackage, parse_file
from .report import graph

EXIT_OK, EXIT_REVIEW, EXIT_REFUSED = 0, 3, 4


def _cmd_analyze(args: argparse.Namespace) -> int:
    try:
        pkg = annotate(parse_file(args.package))
    except NotADtsxPackage as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if args.json:
        print(json.dumps(pkg.to_dict(), indent=2, default=str))
    else:
        print(graph.render(pkg, show_graph=not args.no_graph))

    return EXIT_OK if pkg.coverage.all_recognised else EXIT_REVIEW


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ssis2nifi", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    an = sub.add_parser("analyze", help="parse a .dtsx and report what it contains")
    an.add_argument("package")
    an.add_argument("--json", action="store_true", help="emit the IR instead of the report")
    an.add_argument("--no-graph", action="store_true", help="summary only, no component tree")
    an.set_defaults(func=_cmd_analyze)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
