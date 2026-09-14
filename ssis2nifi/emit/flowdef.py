"""IR + catalogue + bindings -> a NiFi flow definition.

The output is `flowContents` JSON: the same shape NiFi's own "Download flow
definition" produces, and the same shape
~/Desktop/NIFI-FLOW/nifi/flow/ecommerce_etl.flow.json has -- which is known to
import and run on 1.27.0.

WHY A FILE AND NOT REST CALLS
-----------------------------
A file is reviewable, diffable and committable. That is the whole difference
between this and asking a chat model to build a flow: a converter whose output
is seventy REST calls leaves nothing to audit. It also means generation is a
pure function, testable with no NiFi running, which is what makes a real test
suite possible.

DETERMINISM: uuid5, NEVER uuid4
-------------------------------
Every identifier is derived from the SSIS refId that produced it. Three things
follow, and all three are load-bearing:

  * the same package always produces byte-identical JSON, so golden-file tests
    work and a catalogue change shows up as a readable diff rather than 40
    churned UUIDs;
  * the refId IS the provenance key -- given a processor id on a running
    canvas you can compute which SSIS component it came from;
  * re-running the converter updates a flow in place instead of creating a
    second copy of everything.

SECRETS ARE NEVER WRITTEN HERE
------------------------------
A sensitive property is emitted as null (which is what NiFi's own export does)
and recorded in a sidecar naming the environment variable that supplies it.
The flow.json is therefore safe to commit.
"""

from __future__ import annotations

import pathlib
import re
import uuid
from typing import Any

import yaml

from ..ir.model import Package

# Stable namespace for uuid5. Changing this re-identifies every component in
# every flow ever generated, so it is a constant and not a setting.
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "ssis2nifi.local")

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "catalogue"

_TEMPLATE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


class EmitError(Exception):
    """The flow cannot be generated. Refuse rather than emit something broken."""


def _ident(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "|".join(parts)))


def load_catalogue() -> tuple[dict, dict]:
    bundles = yaml.safe_load((CATALOGUE / "bundles.yml").read_text())
    recipes: dict[str, dict] = {}
    for path in sorted((CATALOGUE / "components").glob("*.yml")):
        recipe = yaml.safe_load(path.read_text())
        recipes[recipe["component_class"]] = recipe
    return bundles, recipes


def load_bindings(path: str | None) -> dict:
    if not path:
        return {}
    return yaml.safe_load(pathlib.Path(path).read_text()).get("bindings", {})


def _lookup_path(context: dict, dotted: str) -> Any:
    """Resolve `derived.reference_table` against the context. Missing -> ''."""
    node: Any = context
    for part in dotted.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            node = getattr(node, part, None)
        if node is None:
            return ""
    return node


def _expand(value: Any, context: dict) -> Any:
    """Substitute {{ ... }} in a string. Non-strings pass through untouched."""
    if not isinstance(value, str):
        return value
    if value.startswith("@"):        # @service: / @secret: markers, resolved later
        return _TEMPLATE.sub(lambda m: str(_lookup_path(context, m.group(1))), value)
    return _TEMPLATE.sub(lambda m: str(_lookup_path(context, m.group(1))), value)


def _binding_for(comp, bindings: dict) -> tuple[str, dict]:
    """Which binding this component's connection resolves to."""
    for ref in comp.connections.values():
        for key, binding in bindings.items():
            if binding.get("for") == ref:
                return key, binding
    return "", {}


class _Builder:
    def __init__(self, pkg: Package, bindings: dict, group_name: str):
        self.pkg = pkg
        self.bindings = bindings
        self.bundles, self.recipes = load_catalogue()
        self.group_id = _ident("group", pkg.name)
        self.group_name = group_name
        self.services: dict[str, dict] = {}     # intern key -> service dict
        self.processors: list[dict] = []
        self.connections: list[dict] = []
        self.secrets: list[dict] = []
        self.notes: list[str] = []
        # (component id, recipe processor id) -> nifi identifier
        self.proc_ids: dict[tuple[str, str], str] = {}
        self.entry: dict[tuple[str, str], str] = {}   # (comp id, input name) -> proc id
        self.rel: dict[tuple[str, str], tuple[str, str]] = {}  # (comp, output) -> (proc, relationship)

    # -- small helpers ----------------------------------------------------

    def _bundle(self, name: str) -> dict:
        return self.bundles["bundles"][name]

    def _proc_type(self, kind: str) -> tuple[str, dict]:
        spec = self.bundles["processors"].get(kind)
        if not spec:
            raise EmitError(f"no processor type registered for {kind!r} in bundles.yml")
        return spec["type"], self._bundle(spec["bundle"])

    def _case_identifiers(self, props: dict, context: dict) -> None:
        """Fold database identifiers to the target's convention.

        `dbo.NewFactCurrencyRate` is the same table as `dbo.newfactcurrencyrate`
        on SQL Server and a different (missing) one on Postgres. The binding
        says which convention the target uses; without this a retargeted flow
        deploys cleanly and then fails at runtime with "table not found".
        """
        case = (context.get("binding") or {}).get("identifier_case", "preserve")
        if case not in ("lower", "upper"):
            return
        fold = str.lower if case == "lower" else str.upper
        for name in self.bundles.get("identifier_properties", []):
            if name in props and isinstance(props[name], str) and props[name]:
                props[name] = fold(props[name])

    def _apis(self, kind: str) -> list[dict]:
        """The interfaces this service implements.

        NiFi validates a processor's service reference against these, so a
        LookupRecord will not accept a service that does not declare
        LookupService. Omitting them makes a correct flow fail to import.
        """
        spec = self.bundles["controller_services"].get(kind, {})
        api_types = self.bundles.get("service_apis", {})
        bundle = self.bundles.get("service_api_bundle", {})
        return [
            {"type": api_types[name], "bundle": bundle}
            for name in spec.get("apis", [])
            if name in api_types
        ]

    def _svc_type(self, kind: str) -> tuple[str, dict]:
        spec = self.bundles["controller_services"].get(kind)
        if not spec:
            raise EmitError(f"no controller service registered for {kind!r} in bundles.yml")
        return spec["type"], self._bundle(spec["bundle"])

    # -- services ---------------------------------------------------------

    def _service(self, spec: dict, context: dict) -> str:
        """Create or reuse a controller service. The intern key does the reuse."""
        key = _expand(spec["key"], context)
        if key in self.services:
            return self.services[key]["identifier"]

        stype, bundle = self._svc_type(spec["kind"])
        ident = _ident("service", key)
        props: dict[str, Any] = {}
        for name, raw in (spec.get("properties") or {}).items():
            value = _expand(raw, context)
            if isinstance(value, str) and value.startswith("@secret:"):
                # Never written to the file. NiFi exports sensitive values as
                # null too, so an import sees no difference.
                self.secrets.append({
                    "service_key": key,
                    "service_name": _expand(spec.get("name", key), context),
                    "property": name,
                    "env_var": value[len("@secret:"):],
                })
                props[name] = None
            else:
                props[name] = value
        self._case_identifiers(props, context)

        self.services[key] = {
            "identifier": ident,
            "name": _expand(spec.get("name", key), context),
            "comments": f"generated by ssis2nifi for {key}",
            "type": stype,
            "bundle": bundle,
            "properties": props,
            "propertyDescriptors": {},
            "controllerServiceApis": self._apis(spec["kind"]),
            # Services import DISABLED and are enabled afterwards. NiFi cannot
            # enable a service whose referenced services are not yet present,
            # so import order would otherwise decide whether the flow works.
            "scheduledState": "DISABLED",
            "bulletinLevel": "WARN",
            "componentType": "CONTROLLER_SERVICE",
            "groupIdentifier": self.group_id,
        }
        return ident

    def _resolve_services(self) -> None:
        """Turn `@service:<key>` property values into real identifiers."""
        for holder in list(self.services.values()) + self.processors:
            for name, value in list(holder["properties"].items()):
                if isinstance(value, str) and value.startswith("@service:"):
                    key = value[len("@service:"):]
                    svc = self.services.get(key)
                    if svc is None:
                        raise EmitError(
                            f"{holder['name']!r} references service {key!r}, which no recipe created"
                        )
                    holder["properties"][name] = svc["identifier"]

    # -- processors -------------------------------------------------------

    def _processor(self, comp, spec: dict, context: dict, x: float, y: float) -> str:
        ptype, bundle = self._proc_type(spec["kind"])
        ident = _ident("processor", comp.ref_id, spec["id"])
        props = {n: _expand(v, context) for n, v in (spec.get("properties") or {}).items()}
        self._case_identifiers(props, context)

        self.processors.append({
            "identifier": ident,
            "name": _expand(spec.get("name", comp.name), context),
            # The canvas explains itself: which SSIS component, which rule.
            "comments": f"{comp.ref_id}\nrule: {context['rule_id']}",
            "type": ptype,
            "bundle": bundle,
            "position": {"x": x, "y": y},
            "properties": props,
            "propertyDescriptors": {},
            "style": {},
            "schedulingPeriod": spec.get("scheduling_period", "0 sec"),
            "schedulingStrategy": "TIMER_DRIVEN",
            "executionNode": "ALL",
            "penaltyDuration": "30 sec",
            "yieldDuration": "1 sec",
            "bulletinLevel": "WARN",
            "runDurationMillis": 0,
            "concurrentlySchedulableTaskCount": 1,
            "autoTerminatedRelationships": list(spec.get("auto_terminate", [])),
            "retriedRelationships": list(spec.get("retry_relationships", [])),
            "retryCount": 5 if spec.get("retry_relationships") else 0,
            "backoffMechanism": "PENALIZE_FLOWFILE",
            "maxBackoffPeriod": "30 secs",
            "componentType": "PROCESSOR",
            "groupIdentifier": self.group_id,
            "scheduledState": "ENABLED",
        })
        return ident

    # -- wiring -----------------------------------------------------------

    def _connect(self, src: str, src_name: str, rels: list[str], dst: str, dst_name: str) -> None:
        self.connections.append({
            "identifier": _ident("connection", src, "|".join(rels), dst),
            "name": "",
            "source": {"id": src, "type": "PROCESSOR", "groupId": self.group_id,
                       "name": src_name, "comments": ""},
            "destination": {"id": dst, "type": "PROCESSOR", "groupId": self.group_id,
                            "name": dst_name, "comments": ""},
            "selectedRelationships": rels,
            "labelIndex": 0,
            "zIndex": 0,
            "bends": [],
            "backPressureObjectThreshold": 10000,
            "backPressureDataSizeThreshold": "1 GB",
            "flowFileExpiration": "0 sec",
            "prioritizers": [],
            "loadBalanceStrategy": "DO_NOT_LOAD_BALANCE",
            "partitioningAttribute": "",
            "loadBalanceCompression": "DO_NOT_COMPRESS",
            "componentType": "CONNECTION",
            "groupIdentifier": self.group_id,
        })


def build(pkg: Package, bindings: dict, group_name: str | None = None) -> dict:
    """IR -> flow definition. Raises EmitError rather than emitting a broken flow."""
    b = _Builder(pkg, bindings, group_name or pkg.name)
    names: dict[str, str] = {}

    # --- one pass to create everything -----------------------------------
    COL, ROW = 420.0, 200.0
    for df in pkg.dataflows:
        for i, comp in enumerate(df.components):
            recipe = b.recipes.get(comp.class_id)
            if recipe is None:
                # Refused or unknown: catalog.support already said so loudly.
                b.notes.append(f"skipped {comp.name!r} ({comp.class_id}): no recipe")
                continue

            binding_id, binding = _binding_for(comp, bindings)
            for required in recipe.get("requires", []):
                if required.startswith("connections.") and not binding:
                    raise EmitError(
                        f"{comp.name!r} needs a binding for its connection "
                        f"({', '.join(comp.connections.values()) or 'none declared'}), "
                        f"but no entry in the bindings file matches"
                    )

            context = {
                "node": {"id": comp.id, "name": comp.name, "ref_id": comp.ref_id},
                "derived": comp.derived,
                "properties": comp.properties,
                "binding": binding,
                "binding_id": binding_id,
                "rule_id": recipe.get("rule_id", "?"),
            }

            # Recipe diagnostics: things the rule author knew could go wrong
            # with this component but which only a real value can decide.
            for diag in recipe.get("diagnostics", []):
                cond = diag.get("when", "")
                fired = False
                if "==" in cond:
                    lhs, _, rhs = cond.partition("==")
                    fired = str(_lookup_path(context, lhs.strip())) == rhs.strip().strip("'\"")
                elif cond:
                    fired = bool(_lookup_path(context, cond.strip()))
                if fired:
                    b.notes.append(
                        f"[{diag.get('severity', 'info')}] {diag.get('code')} "
                        f"({comp.name}): {' '.join(diag.get('message', '').split())}"
                    )

            for spec in recipe.get("services", []):
                b._service(spec, context)

            x, y = 400.0 + (i % 4) * COL, 200.0 + (i // 4) * ROW
            for j, spec in enumerate(recipe.get("processors", [])):
                if "when" in spec and not comp.derived.get(spec["when"].split(".")[-1]):
                    continue
                pid = b._processor(comp, spec, context, x, y + j * 120.0)
                b.proc_ids[(comp.id, spec["id"])] = pid
                names[pid] = comp.name

            for in_name, target in (recipe.get("inputs") or {}).items():
                b.entry[(comp.id, in_name)] = b.proc_ids[(comp.id, target)]
            for out_name, target in (recipe.get("outputs") or {}).items():
                b.rel[(comp.id, out_name)] = (
                    b.proc_ids[(comp.id, target["processor"])], target["relationship"]
                )

    # --- wire the edges the package declares ------------------------------
    for df in pkg.dataflows:
        for edge in df.edges:
            src = b.rel.get((edge.from_node, edge.from_output))
            dst = b.entry.get((edge.to_node, edge.to_input))
            if src is None or dst is None:
                b.notes.append(
                    f"edge {edge.from_node}.{edge.from_output} -> {edge.to_node} not wired "
                    "(one end has no recipe)"
                )
                continue
            b._connect(src[0], names.get(src[0], ""), [src[1]], dst, names.get(dst, ""))

    # --- unconnected outputs: the silent-row-loss guard --------------------
    #
    # An SSIS output with no <path> is not "nothing happens". A Lookup with
    # NoMatchBehavior=0 FAILS the data flow on a miss, so the rows must go
    # somewhere visible -- auto-terminating would discard them and produce
    # identical row counts on the happy path. The recipe's
    # dangling_output_policy decides; this applies it.
    reject_id: str | None = None

    def reject_sink() -> str:
        """One shared sink for everything SSIS would have failed on."""
        nonlocal reject_id
        if reject_id is None:
            ptype, bundle = b._proc_type("PutFile")
            reject_id = _ident("processor", "reject-sink")
            b.processors.append({
                "identifier": reject_id, "name": "Rejected rows",
                "comments": "Rows SSIS would have failed or redirected. "
                            "Generated because the source package left these outputs unwired.",
                "type": ptype, "bundle": bundle,
                "position": {"x": 400.0, "y": 900.0},
                "properties": {"Directory": "/opt/nifi/data/rejects",
                               "Conflict Resolution Strategy": "replace",
                               "Create Missing Directories": "true"},
                "propertyDescriptors": {}, "style": {},
                "schedulingPeriod": "0 sec", "schedulingStrategy": "TIMER_DRIVEN",
                "executionNode": "ALL", "penaltyDuration": "30 sec",
                "yieldDuration": "1 sec", "bulletinLevel": "WARN",
                "runDurationMillis": 0, "concurrentlySchedulableTaskCount": 1,
                "autoTerminatedRelationships": ["success", "failure"],
                "retriedRelationships": [], "retryCount": 0,
                "backoffMechanism": "PENALIZE_FLOWFILE", "maxBackoffPeriod": "30 secs",
                "componentType": "PROCESSOR", "groupIdentifier": b.group_id,
                "scheduledState": "ENABLED",
            })
            names[reject_id] = "Rejected rows"
        return reject_id

    # PutFile writes to Directory/${filename}, and every dangling output in
    # a package routes into the SAME shared sink with "replace" conflict
    # resolution. Two different lookups missing on the SAME source file
    # therefore both want to write .../rejects/<source filename> -- the
    # second write silently replaces the first, and everything it carried is
    # gone with no error anywhere. Caught by `make verify-behavior`, which
    # feeds a batch designed to miss two different lookups from one file:
    # the first lookup's rejects vanished from both the reject sink and the
    # fact table. This stamp makes every reject write's filename unique
    # (NiFi gives every FlowFile a `uuid` attribute already; no new state to
    # track) so "replace" only ever replaces a write with itself.
    stamp_id: str | None = None

    def reject_stamp() -> str:
        nonlocal stamp_id
        if stamp_id is None:
            ptype, bundle = b._proc_type("UpdateAttribute")
            stamp_id = _ident("processor", "reject-stamp")
            b.processors.append({
                "identifier": stamp_id, "name": "Make reject filename unique",
                "comments": "Two different outputs redirecting to the same reject sink from "
                            "the same source file must not overwrite each other's write.",
                "type": ptype, "bundle": bundle,
                "position": {"x": 400.0, "y": 800.0},
                "properties": {"filename": "${filename}-${uuid}"},
                "propertyDescriptors": {}, "style": {},
                "schedulingPeriod": "0 sec", "schedulingStrategy": "TIMER_DRIVEN",
                "executionNode": "ALL", "penaltyDuration": "30 sec",
                "yieldDuration": "1 sec", "bulletinLevel": "WARN",
                "runDurationMillis": 0, "concurrentlySchedulableTaskCount": 1,
                "autoTerminatedRelationships": [],
                "retriedRelationships": [], "retryCount": 0,
                "backoffMechanism": "PENALIZE_FLOWFILE", "maxBackoffPeriod": "30 secs",
                "componentType": "PROCESSOR", "groupIdentifier": b.group_id,
                "scheduledState": "ENABLED",
            })
            names[stamp_id] = "Make reject filename unique"
            b._connect(stamp_id, names[stamp_id], ["success"], reject_sink(), "Rejected rows")
        return stamp_id

    for df in pkg.dataflows:
        for dangling in df.dangling_outputs:
            target = b.rel.get((dangling.node, dangling.output))
            if target is None:
                continue                      # component had no recipe
            comp = next((c for c in df.components if c.id == dangling.node), None)
            recipe = b.recipes.get(comp.class_id) if comp else None
            if recipe is None:
                continue

            action = "auto_terminate"
            for rule in (recipe.get("dangling_output_policy") or {}).get(dangling.output, []):
                if "when" in rule:
                    # The only predicate form in use: properties.X == 'v'
                    lhs, _, rhs = rule["when"].partition("==")
                    if str(_lookup_path({"properties": comp.properties,
                                         "derived": comp.derived}, lhs.strip())) == rhs.strip().strip("'\""):
                        action = rule.get("action", "auto_terminate")
                        break
                elif "else" in rule:
                    action = rule["else"]
                else:
                    action = rule.get("action", "auto_terminate")
                    break

            pid, relationship = target
            if action == "route_to_reject":
                b._connect(pid, names.get(pid, ""), [relationship],
                           reject_stamp(), "Make reject filename unique")
                b.notes.append(
                    f"{dangling.node}.{dangling.output}: unwired in SSIS and "
                    f"{dangling.disposition}; routed to the reject sink rather than dropped"
                )
            else:
                proc = next(p for p in b.processors if p["identifier"] == pid)
                if relationship not in proc["autoTerminatedRelationships"]:
                    proc["autoTerminatedRelationships"].append(relationship)

    # --- every relationship must be connected or auto-terminated ----------
    #
    # NiFi marks a processor invalid otherwise, which is the most common cause
    # of a generated flow importing to a red canvas. Assert it here, where the
    # error can name the processor, rather than discovering it on the canvas.
    declared = b.bundles.get("relationships", {})
    connected: dict[str, set[str]] = {}
    for conn in b.connections:
        connected.setdefault(conn["source"]["id"], set()).update(conn["selectedRelationships"])

    for proc in b.processors:
        kind = proc["type"].rsplit(".", 1)[-1]
        rels = declared.get(kind)
        if isinstance(rels, dict):        # depends on a property, e.g. LookupRecord
            strategy = proc["properties"].get("routing-strategy", "route-to-success")
            rels = rels.get(strategy, [])
        if not rels:
            continue
        handled = set(proc["autoTerminatedRelationships"]) | connected.get(proc["identifier"], set())
        missing = [r for r in rels if r not in handled]
        if missing:
            # Nothing meaningful to do with them: terminate explicitly so the
            # processor is valid, and say so.
            proc["autoTerminatedRelationships"].extend(missing)
            b.notes.append(
                f"{proc['name']!r}: auto-terminated {', '.join(missing)} "
                "(no destination in the source package)"
            )

    # --- the reader depends on POSITION, not on the component ------------
    #
    # A record processor reads whatever its upstream neighbour wrote. Only the
    # first one in a chain sees the source file's own format; every one after
    # it sees the record writer's output, which is JSON. Giving them all the
    # source reader makes the second processor try to parse JSON as CSV and
    # fail with "Could not determine schema ... MalformedRecordException".
    #
    # So: processors fed directly by the source adapter keep the source
    # reader; everything downstream is switched to a JSON reader that matches
    # the writer.
    source_procs = {
        b.proc_ids[(comp.id, spec["id"])]
        for df in pkg.dataflows
        for comp in df.components
        if not comp.inputs and b.recipes.get(comp.class_id)
        for spec in b.recipes[comp.class_id].get("processors", [])
        if (comp.id, spec["id"]) in b.proc_ids
    }
    downstream: dict[str, str] = {}
    for conn in b.connections:
        downstream[conn["destination"]["id"]] = conn["source"]["id"]

    READER_PROPS = ("record-reader", "put-db-record-record-reader")
    needs_json = [
        p for p in b.processors
        if any(k in p["properties"] for k in READER_PROPS)
        and downstream.get(p["identifier"]) not in source_procs
    ]
    if needs_json:
        json_reader = b._service(
            {"key": "stream_reader", "kind": "JsonTreeReader", "name": "StreamReader",
             "properties": {"schema-access-strategy": "infer-schema"}},
            {"node": {}, "derived": {}, "properties": {}, "binding": {},
             "binding_id": "", "rule_id": "internal"},
        )
        for proc in needs_json:
            for key in READER_PROPS:
                if key in proc["properties"]:
                    proc["properties"][key] = json_reader
            b.notes.append(
                f"{proc['name']!r}: reads JSON from an upstream record processor, "
                "so it uses the stream reader rather than the source-format reader"
            )

    b._resolve_services()

    return {
        "flowContents": {
            "identifier": b.group_id,
            "name": b.group_name,
            "comments": (
                f"Generated by ssis2nifi from {pathlib.Path(pkg.source_file).name}\n"
                f"source sha256: {pkg.sha256}"
            ),
            "position": {"x": 0.0, "y": 0.0},
            "variables": {},
            "processGroups": [],
            "remoteProcessGroups": [],
            "processors": b.processors,
            "inputPorts": [],
            "outputPorts": [],
            "connections": b.connections,
            "labels": [],
            "funnels": [],
            "controllerServices": list(b.services.values()),
            "defaultFlowFileExpiration": "0 sec",
            "defaultBackPressureObjectThreshold": 10000,
            "defaultBackPressureDataSizeThreshold": "1 GB",
            "componentType": "PROCESS_GROUP",
            "flowFileConcurrency": "UNBOUNDED",
            "flowFileOutboundPolicy": "STREAM_WHEN_AVAILABLE",
        },
        "externalControllerServices": {},
        "parameterContexts": {},
        "parameterProviders": {},
        "flowEncodingVersion": "1.0",
        "latest": False,
    }, b.secrets, b.notes
