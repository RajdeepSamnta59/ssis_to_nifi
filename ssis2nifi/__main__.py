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
import pathlib
import sys

from .catalog.derive import derive
from .catalog.support import annotate
from .dtsx.parse import NotADtsxPackage, parse_file
from .emit import flowdef
from .ir import schema
from .report import graph

EXIT_OK, EXIT_REVIEW, EXIT_REFUSED = 0, 3, 4


def _cmd_analyze(args: argparse.Namespace) -> int:
    try:
        pkg = derive(annotate(parse_file(args.package)))
    except NotADtsxPackage as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if args.json:
        print(json.dumps(pkg.to_dict(), indent=2, default=str))
    else:
        print(graph.render(pkg, show_graph=not args.no_graph))

    return EXIT_OK if pkg.coverage.all_recognised else EXIT_REVIEW


def _cmd_ir(args: argparse.Namespace) -> int:
    """Write the IR. This is the artifact a package owner reviews."""
    try:
        pkg = derive(annotate(parse_file(args.package)))
    except NotADtsxPackage as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    text = schema.dump(pkg)
    if args.out:
        pathlib.Path(args.out).write_text(text)
        print(f"wrote {args.out}  ({len(text.splitlines())} lines)", file=sys.stderr)
        print(f"  content  digest: {schema.content_digest(pkg)[:16]}", file=sys.stderr)
        print(f"  topology digest: {schema.topology_digest(pkg)[:16]}", file=sys.stderr)
    else:
        print(text, end="")
    return EXIT_OK if pkg.coverage.all_recognised else EXIT_REVIEW


def _cmd_convert(args: argparse.Namespace) -> int:
    """IR -> flow.json + secrets sidecar. Pure: no NiFi needed."""
    try:
        pkg = derive(annotate(parse_file(args.package)))
    except NotADtsxPackage as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        flow, secrets, notes = flowdef.build(
            pkg, flowdef.load_bindings(args.bindings), args.group_name
        )
    except flowdef.EmitError as exc:
        print(f"cannot generate: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    out = pathlib.Path(args.out or f"out/{pathlib.Path(args.package).stem}.flow.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(flow, indent=2))

    fc = flow["flowContents"]
    print(f"wrote {out}", file=sys.stderr)
    print(f"  {len(fc['processors'])} processors, {len(fc['connections'])} connections, "
          f"{len(fc['controllerServices'])} controller services", file=sys.stderr)

    if secrets:
        side = out.with_suffix("").with_suffix(".secrets.json")
        side.write_text(json.dumps(secrets, indent=2))
        print(f"  {len(secrets)} sensitive propert(ies) left null; see {side.name}", file=sys.stderr)

    for note in notes:
        print(f"  note: {note}", file=sys.stderr)
    return EXIT_OK if pkg.coverage.all_recognised else EXIT_REVIEW


def _cmd_verify(args: argparse.Namespace) -> int:
    """Import into a live NiFi and assert nothing is invalid. Non-destructive."""
    from .validate.live import verify

    bad = verify(args.flow, args.nifi, keep=args.keep, settle_seconds=args.settle)
    if not bad:
        print("valid: NiFi accepted the flow with no flow-level validation errors")
        return EXIT_OK
    print(f"{len(bad)} component(s) invalid:", file=sys.stderr)
    for comp in bad:
        print(f"  [{comp['type']}] {comp['name']}", file=sys.stderr)
        for err in comp["errors"]:
            print(f"      - {err}", file=sys.stderr)
    return EXIT_REFUSED


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ssis2nifi", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    an = sub.add_parser("analyze", help="parse a .dtsx and report what it contains")
    an.add_argument("package")
    an.add_argument("--json", action="store_true", help="emit the IR instead of the report")
    an.add_argument("--no-graph", action="store_true", help="summary only, no component tree")
    an.set_defaults(func=_cmd_analyze)

    ir = sub.add_parser("ir", help="write the intermediate representation as YAML")
    ir.add_argument("package")
    ir.add_argument("-o", "--out", help="file to write (default: stdout)")
    ir.set_defaults(func=_cmd_ir)

    cv = sub.add_parser("convert", help="generate a NiFi flow definition")
    cv.add_argument("package")
    cv.add_argument("-b", "--bindings", help="bindings YAML mapping connections to real targets")
    cv.add_argument("-o", "--out", help="flow.json to write (default: out/<pkg>.flow.json)")
    cv.add_argument("--group-name", help="process group name (default: the package name)")
    cv.set_defaults(func=_cmd_convert)

    vf = sub.add_parser("verify", help="import a generated flow into NiFi and check validity")
    vf.add_argument("flow")
    vf.add_argument("--nifi", default="http://localhost:8080", help="NiFi base URL")
    vf.add_argument("--keep", action="store_true", help="leave the group on the canvas")
    vf.add_argument("--settle", type=float, default=15.0, help="seconds to wait for validation")
    vf.set_defaults(func=_cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
