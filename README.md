# SSIS2NIFI

Reads a real SQL Server Integration Services package (`.dtsx`) and generates the
equivalent Apache NiFi flow.

A **deterministic compiler**, not a prompt. Same package in, byte-identical flow
out, every time — reviewable, diffable, and committable, which is the point: an
ad-hoc AI translation cannot tell you where it guessed.

For the pictures, see [`DIAGRAM.md`](DIAGRAM.md).

---

## Status

**Milestones 1–3 of 5 done. A real SSIS package now becomes a running-shaped
NiFi flow.**

```
$ make convert && make verify-import
wrote out/L1.flow.json
  5 processors, 9 connections, 5 controller services
  1 sensitive property left null; see L1.secrets.json
  note: lookup_currency_key.Lookup No Match Output: unwired in SSIS and
        fail_component; routed to the reject sink rather than dropped
...
valid: NiFi accepted the flow with no flow-level validation errors
```

Verified against Apache NiFi 1.27.0. The check is non-destructive — the flow is
imported into a new process group and deleted afterwards, so it can run against
an instance that is already busy.

Next: M4 (deploy and load real rows) and M5 (the behavioural gate).

```bash
python3 -m ssis2nifi analyze corpus/packages/L1.dtsx
```

```
package: Lesson 1   (Microsoft.Package, format 8, friendly dialect)
connections: 2 (1 FLATFILE, 1 OLEDB)
control flow: 1 task(s), 0 precedence constraint(s)

dataflow "Extract Sample Currency Data": 4 components, 3 paths

  [Extract Sample Currency Data]  FlatFileSource  ✓
   ├─ Flat File Source Output ──▶ [Lookup Currency Key]
   └─ Flat File Source Error Output  ✗ rows would be redirected here
  [Lookup Currency Key]  Lookup  ✓
   ├─ Lookup Match Output ──▶ [Lookup Date Key]
   ├─ Lookup No Match Output  ✗ fails the data flow on a miss
   └─ Lookup Error Output  ✗ rows would be redirected here
  ...

coverage: 4/4 convertible, 0 need manual review
```

## Commands

```bash
make analyze FILE=corpus/packages/L1.dtsx   # the report above
make ir      FILE=corpus/packages/L1.dtsx   # write out/<pkg>.ir.yaml
make convert FILE=corpus/packages/L1.dtsx   # write out/<pkg>.flow.json
make verify-import                          # import into a live NiFi, check validity
make corpus                                 # run every package, show exit codes
make test                                   # the suite, in Docker
```

`analyze` is standard library only — no install step. `ir` needs PyYAML
(`requirements.txt`). `make test` runs pytest in a container because this
machine has no `pip`.

## The IR

`make ir` writes the intermediate representation: an edge-bearing description of
what the package does, in SSIS's own vocabulary. It is the artifact you hand to
the person who owns the package and ask *"is this what it does?"* — no NiFi
knowledge required to answer.

It round-trips losslessly (`load(dump(pkg)) == pkg`, tested over the whole
corpus), so a reviewed and hand-edited IR can drive the Converter.

Two digests, because they answer different questions:

| digest | answers |
|---|---|
| `content_digest` | is this the same package? (everything but where the file came from) |
| `topology_digest` | is this the same pipeline *shape*? (classes + wiring + branch semantics, no configuration) |

The second exists because of a correction worth knowing about — see
[`corpus/PROVENANCE.md`](corpus/PROVENANCE.md).

## What the generated flow looks like

`Microsoft.Lookup` becomes `LookupRecord` + `DatabaseRecordLookupService`, and
SSIS's branch names become NiFi relationships:

```
Extract Sample Currency Data --success--> Lookup Currency Key
Lookup Currency Key        --matched--> Lookup Date Key
Lookup Date Key            --matched--> Sample OLE DB Destination
                         --unmatched--> Rejected rows
                           --failure--> Rejected rows
```

Two properties hold by construction, and both are tested:

**No silent row loss.** An output SSIS left unwired is not auto-terminated when
SSIS would have failed on it. `NoMatchBehavior=0` means a miss fails the whole
data flow, so those rows go to a visible reject sink. Auto-terminating would
give identical row counts on the happy path and opposite behaviour when it
matters — see picture 5 in [`DIAGRAM.md`](DIAGRAM.md).

**No secrets in the artifact.** Sensitive properties are emitted as `null` (what
NiFi's own export does) and the environment variable that supplies each one is
recorded in a `.secrets.json` sidecar. The flow is safe to commit.

## Determinism

Every identifier is `uuid5` of the SSIS `refId` that produced it, never `uuid4`.
So the same package always produces byte-identical JSON — golden-file tests
work, a catalogue change shows as a readable diff rather than 40 churned UUIDs,
and **the refId is the provenance key**: given a processor on a canvas you can
compute which SSIS component it came from. Each processor's `comments` carries
its source refId and the rule that generated it.

## Exit codes are a contract

| code | meaning |
|---|---|
| `0` | every component has a conversion rule |
| `3` | parsed, but something needs a human |
| `4` | refused — not a genuine `.dtsx` |

`3` is non-zero on purpose. A CI pipeline must not be able to ship a
half-translated package by accident, and "it printed a warning" is not a gate.

## What it converts

| Supported | Refused, by name and with a reason |
|---|---|
| FlatFileSource / FlatFileDestination | Script Component (`ManagedComponentHost`) — arbitrary .NET |
| OLEDBSource / OLEDBDestination | Sort — blocking, no streaming NiFi equivalent |
| Lookup | Merge Join — needs sorted inputs |
| DerivedColumn | Aggregate — semantics differ from `QueryRecord GROUP BY` |
| ConditionalSplit | ForEach / For / Sequence containers — orchestration, which NiFi has no concept of |

**Refusing is a feature.** A refused component is one we understand well enough
to know a faithful translation does not exist. Saying so is worth more than
emitting something plausible and wrong.

## The corpus

Six genuine Visual Studio packages, plus two negative fixtures.

The negative fixtures are **generated lookalikes** — XML that imitates DTSX
without being it. They exist so the refusal path is tested, not assumed:

- one declares `http://www.microsoft.com/SqlServer/Dts`; real packages use
  `www.microsoft.com/SqlServer/Dts` with no scheme
- one is not even well-formed XML — an unescaped `&` in a component name

Both are refused with a message naming the actual problem.

## Design rules

1. **The Analyzer must not know NiFi exists.** `ssis2nifi/dtsx/` and
   `ssis2nifi/ir/` are checked for NiFi vocabulary by a test. That boundary is
   what lets the IR be reviewed by the person who owns the package.
2. **Property keys come from a flow proven to import and run**, never from
   documentation or memory.
3. **The mapping is data.** A reviewer reads one YAML file and knows what will
   be emitted.
4. **Nothing is silently dropped.** Every output is either wired or recorded as
   dangling, with the SSIS consequence attached — see picture 5 in
   [`DIAGRAM.md`](DIAGRAM.md), which is the one failure mode in this tool that
   is otherwise invisible.

## Related

- `~/Desktop/NIFI-FLOW` — the hand-built reference implementation, and the
  source of every property key used here. Its comparison harness becomes this
  tool's behavioural gate.
