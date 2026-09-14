"""Which SSIS components this tool can convert, and why it refuses the rest.

This lives in the catalogue layer, not the parser, and the split is deliberate.

The parser's job is to say what is IN the package -- that is a fact about SSIS
and nothing else, which is why `ssis2nifi/dtsx/` is not allowed to mention NiFi
(enforced by tests/unit/test_refid_and_layering.py). Whether a component can be
converted is a different kind of claim: it is about what NiFi can express, and
it changes as the catalogue grows. Keeping it here means the IR stays a neutral
description of the customer's package, and coverage is an annotation applied to
it rather than a property baked into it.

REFUSING IS A FEATURE. A component listed in REFUSED is one we understand well
enough to know that a faithful translation does not exist. Saying so is worth
more than emitting something plausible and wrong -- the whole reason a hand-run
AI translation was rejected is that it cannot tell you where it guessed.
"""

from __future__ import annotations

from ..ir.model import Coverage, Package

# componentClassID values with a conversion recipe.
SUPPORTED: set[str] = {
    "Microsoft.FlatFileSource",
    "Microsoft.FlatFileDestination",
    "Microsoft.OLEDBSource",
    "Microsoft.OLEDBDestination",
    "Microsoft.Lookup",
    "Microsoft.DerivedColumn",
    "Microsoft.ConditionalSplit",
}

# Components we recognise and deliberately refuse, with the reason in NiFi terms.
REFUSED: dict[str, str] = {
    "Microsoft.ManagedComponentHost":
        "Script Component: arbitrary .NET, requires human translation",
    "Microsoft.ScriptComponentHost":
        "Script Component: arbitrary .NET, requires human translation",
    "Microsoft.Sort":
        "Sort is blocking; NiFi has no streaming equivalent with the same semantics",
    "Microsoft.MergeJoin":
        "Merge Join needs sorted inputs; no faithful NiFi equivalent",
    "Microsoft.Aggregate":
        "Aggregate is blocking; semantics differ from a QueryRecord GROUP BY",
}

# Control-flow containers: orchestration, which NiFi has no concept of.
CONTAINER_NOTE = (
    "{kind} is orchestration; NiFi has no equivalent. Its inner tasks are converted, "
    "the looping is reported as execution order rather than generated."
)


def classify(class_id: str) -> tuple[str, str]:
    """(verdict, reason) for one componentClassID.

    verdict is supported | refused | unknown.
    """
    if class_id in SUPPORTED:
        return "supported", ""
    if class_id in REFUSED:
        return "refused", REFUSED[class_id]
    return "unknown", "no conversion rule; requires manual review"


def annotate(pkg: Package) -> Package:
    """Fill in coverage and the conversion diagnostics, in place.

    Called after parsing. Separating it keeps `parse_file()` a pure description
    of the package, so the same IR can be re-scored as the catalogue grows
    without re-reading the .dtsx.
    """
    cov = Coverage(total_components=sum(len(df.components) for df in pkg.dataflows))
    for df in pkg.dataflows:
        for comp in df.components:
            verdict, reason = classify(comp.class_id)
            if verdict == "supported":
                cov.recognised += 1
                continue
            cov.unsupported += 1
            pkg.diag(
                "error",
                "COMPONENT_REFUSED" if verdict == "refused" else "COMPONENT_UNKNOWN",
                f"{comp.class_id or comp.raw_class_id!r}: {reason}",
                node=comp.id,
            )

    for task in pkg.tasks:
        if task.kind == "container":
            pkg.diag("warn", "CONTROL_FLOW_CONTAINER",
                     CONTAINER_NOTE.format(kind=task.executable_type), node=task.id)

    pkg.coverage = cov
    return pkg
