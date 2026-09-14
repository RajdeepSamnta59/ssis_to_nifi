"""Turning raw SSIS properties into the handful of values a recipe needs.

This is converter knowledge, not parse knowledge, which is why it lives here
and not in `dtsx/`. "`SqlCommand` is a reference query, and the table it reads
is what `dbrecord-lookup-table-name` wants" is a statement about NiFi. The
parser's job was only to report that a property called `SqlCommand` exists and
what it contains.

Everything derived here is recorded on the component as `derived`, so the IR
shows both the raw property and the conclusion drawn from it. A reviewer can
therefore check the reasoning, not just the answer -- which is the difference
between an auditable tool and a black box.

WHERE SSIS PUTS THINGS, WHICH IS NOT WHERE YOU EXPECT
-----------------------------------------------------
A Lookup's join is NOT a component property. `JoinToReferenceColumn` at
component level is the empty string. The real join lives on the *input column*
(`CurrencyID` carries `JoinToReferenceColumn = CurrencyAlternateKey`), and the
returned columns are marked on *output columns* with `CopyFromReferenceColumn`.
Reading only component properties yields an empty join and a lookup that cannot
be generated -- silently, because the property exists and is simply blank.
"""

from __future__ import annotations

import re

from ..ir.model import Component, Package

# `select * from (select * from [dbo].[DimCurrency]) as refTable where ...`
# The table is the innermost FROM. Brackets are SQL Server quoting.
_FROM = re.compile(r"from\s+((?:\[[^\]]+\]|[\w]+)(?:\s*\.\s*(?:\[[^\]]+\]|[\w]+))*)", re.I)


def _unbracket(name: str) -> str:
    return "".join(part.strip().strip("[]") for part in name.split("."))


def _split_table(qualified: str) -> tuple[str, str]:
    """`[dbo].[DimCurrency]` -> ("dbo", "DimCurrency"); bare name -> ("", name)."""
    parts = [p.strip().strip("[]") for p in re.split(r"\.\s*", qualified.strip()) if p.strip()]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "", (parts[-1] if parts else "")


def _lookup(comp: Component) -> dict:
    props = comp.properties
    sql = props.get("SqlCommand", "")

    # Innermost FROM: the reference table. A filtered reference set is a real
    # semantic difference, since DatabaseRecordLookupService reads the whole
    # table -- so it is flagged rather than quietly dropped.
    tables = _FROM.findall(sql)
    schema, table = _split_table(tables[-1]) if tables else ("", "")
    filtered = bool(re.search(r"\bwhere\b", sql, re.I))

    joins = [
        (col.name, col.properties.get("JoinToReferenceColumn", ""))
        for port in comp.inputs
        for col in port.columns
        if col.properties.get("JoinToReferenceColumn")
    ]
    returns = [
        col.properties.get("CopyFromReferenceColumn", "")
        for port in comp.outputs
        for col in port.columns
        if col.properties.get("CopyFromReferenceColumn")
    ]

    # CacheType: 0 full, 1 partial, 2 none. NiFi has full or bounded, not partial.
    cache = {"0": "5000", "1": "5000", "2": "0"}.get(props.get("CacheType", "0"), "5000")

    return {
        "reference_schema": schema,
        "reference_table": f"{schema}.{table}" if schema else table,
        "reference_filtered": filtered,
        "input_column": joins[0][0] if joins else "",
        "join_column": joins[0][1] if joins else "",
        "composite_key": len(joins) > 1,
        "returns": returns,
        "cache_size": cache,
        "no_match_fails": props.get("NoMatchBehavior", "0") == "0",
    }


def _oledb_destination(comp: Component) -> dict:
    props = comp.properties
    schema, table = _split_table(props.get("OpenRowset", ""))
    access = props.get("AccessMode", "")
    fast_load = props.get("FastLoadOptions", "")
    return {
        "target_schema": schema,
        "target_table": table,
        "target_qualified": f"{schema}.{table}" if schema else table,
        # AccessMode 3/4 are the fast-load paths; anything else is row-at-a-time.
        "batch_size": "500" if access in {"3", "4"} else "1",
        "no_check_constraints": bool(fast_load) and "CHECK_CONSTRAINTS" not in fast_load,
        "columns": [
            col.name
            for port in comp.inputs
            for col in port.columns
            if col.name
        ],
    }


# `_x000D__x000A_` is SSIS's escaping for CR LF inside an XML attribute.
_ESCAPES = {
    "_x000D_": "\r", "_x000A_": "\n", "_x0009_": "\t",
    "_x003C_": "<", "_x003E_": ">", "_x007C_": "|", "_x0020_": " ",
}


def unescape(value: str) -> str:
    for token, char in _ESCAPES.items():
        value = value.replace(token, char)
    return value


def _flat_file_source(comp: Component, pkg: Package) -> dict:
    """Delimiters, which SSIS stores in two places that can disagree.

    The connection manager has a RowDelimiter, and each column has a
    ColumnDelimiter -- with the LAST column's delimiter actually being the row
    delimiter. In the corpus RowDelimiter is empty and the final column carries
    CR LF, so trusting RowDelimiter gives "" and a reader that treats the whole
    file as one row. NiFi's CSVReader takes a single value separator, so a file
    whose columns disagree cannot be read faithfully and is flagged.
    """
    cm_ref = next(iter(comp.connections.values()), "")
    cm = next((c for c in pkg.connections if c.ref_id == cm_ref), None)
    cm_props = cm.properties if cm else {}

    # Delimiters come from the CONNECTION MANAGER's FlatFileColumns, not from
    # the component's output columns -- the component only names the fields.
    delimiters = [
        unescape(col.get("ColumnDelimiter", ""))
        for col in (cm.columns if cm else [])
        if col.get("ColumnDelimiter")
    ]
    row_delim = unescape(cm_props.get("RowDelimiter", ""))
    inferred = False
    if not row_delim and delimiters:
        row_delim, inferred = delimiters[-1], True

    body = [d for d in delimiters[:-1]] if len(delimiters) > 1 else delimiters
    distinct = {d for d in body if d}

    header = unescape(cm_props.get("HeaderRowDelimiter", ""))
    qualifier = unescape(cm_props.get("TextQualifier", ""))

    return {
        "column_delimiter": next(iter(distinct), ","),
        "per_column_delimiters": len(distinct) > 1,
        "row_delimiter": row_delim,
        "row_delimiter_inferred": inferred,
        "has_header": "true" if header else "false",
        "charset": "windows-1252" if cm_props.get("CodePage") == "1252" else "UTF-8",
        # SSIS writes the literal "<none>" (escaped) when there is no qualifier.
        "text_qualifier": "" if qualifier in ("", "<none>") else qualifier,
    }


_DERIVERS = {
    "Microsoft.Lookup": lambda c, p: _lookup(c),
    "Microsoft.OLEDBDestination": lambda c, p: _oledb_destination(c),
    "Microsoft.FlatFileSource": _flat_file_source,
}


def derive(pkg: Package) -> Package:
    """Attach a `derived` block to every component we know how to read."""
    for df in pkg.dataflows:
        for comp in df.components:
            fn = _DERIVERS.get(comp.class_id)
            if fn:
                comp.derived = fn(comp, pkg)
    return pkg
