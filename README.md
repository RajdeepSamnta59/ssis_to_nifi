# SSIS2NIFI

Reads a real SQL Server Integration Services package (`.dtsx`) and generates the
equivalent Apache NiFi flow.

A **deterministic compiler**, not a prompt. Same package in, byte-identical flow
out, every time — reviewable, diffable, and committable, which is the point: an
ad-hoc AI translation cannot tell you where it guessed.

For the pictures, see [`DIAGRAM.md`](DIAGRAM.md).

## Running it

This repo owns its own NiFi + Postgres — nothing else needs to be running or
cloned first.

```bash
make setup   # once: .env + the Postgres JDBC driver
make up      # starts NiFi (localhost:8081) + Postgres (localhost:5435, db ssis2nifi)
```

---

## Status

**All 5 milestones done.** A real SSIS package becomes a NiFi flow, NiFi
accepts it, and an independently computed expectation — not the tool grading
its own homework — agrees with what actually landed.

```
$ make verify-behavior
reading 2 reference table(s) directly (not through the lookup service)
  dbo.dimcurrency: 3 row(s)
  dbo.dimdate: 14 row(s)
batch: 12 rows -> 6 expected landed, 6 expected rejected
...
actual: 6 landed, 6 rejected
agrees: every row landed or was rejected exactly where the independent
        check expected
```

`verify-behavior` reads each Lookup's reference table with a plain `SELECT` —
code that shares nothing with `emit/flowdef.py` or with NiFi's own
`DatabaseRecordLookupService` — reproduces the match/no-match decision itself,
then feeds a batch engineered to hit every cell of the disposition matrix
(DIAGRAM.md picture 5) through the real, deployed flow and diffs the two
answers row for row. See [*What M5 actually caught*](#what-m5-actually-caught)
below — it found a real bug on the first run.

Earlier, smaller runs are still true: 38 arbitrary input rows produced 30
landed / 8 rejected with correctly resolved surrogate keys, verified end to
end against Apache NiFi 1.27.0 and Postgres 16.

```
$ make convert && make verify-import
wrote out/L1.flow.json
  6 processors, 10 connections, 6 controller services
  1 sensitive property left null; see L1.secrets.json
  note: lookup_currency_key.Lookup No Match Output: unwired in SSIS and
        fail_component; routed to the reject sink rather than dropped
...
valid: NiFi accepted the flow with no flow-level validation errors
```

`make verify-import` is non-destructive — the flow goes into a new process group
and is deleted afterwards, so it can run against a busy instance.

## What M5 actually caught

The first real run of the behavioural gate found a genuine silent-row-loss
bug, not a test-harness bug — the exact failure mode DIAGRAM.md picture 5
exists to prevent, discovered by *triggering* it rather than by reading the
property descriptors.

**The bug.** Every dangling output in a package — regardless of which
component or which lookup it came from — was wired to one shared `PutFile`
processor writing `Directory/${filename}` with `Conflict Resolution
Strategy: replace`. Feed a batch that misses **two different** lookups from
the **same source file**, and both rejections try to write the same path.
Whichever one runs second silently replaces the first — no error, no
bulletin, nothing in the logs. A batch of 12 engineered to hit every branch
of the disposition matrix showed it immediately: 3 rows that should have been
rejected at the currency lookup were in neither the fact table nor the reject
file. Gone, with the flow reporting no problem at all.

**The fix.** A small `UpdateAttribute` ("Make reject filename unique") now
sits between every dangling output and the shared sink, stamping
`filename` to `${filename}-${uuid}` before the write — using the FlowFile's
own `uuid` attribute, which NiFi already guarantees is unique, so there is no
new state to track. Two lookups missing on one file now produce two reject
files instead of one file with half its rows missing. Re-running the same 12
row batch afterwards: `6 landed, 6 rejected`, and the independent oracle
agrees with all twelve.

**Why Tier 3 didn't catch this.** The flow was, and remained, perfectly
*valid* the whole time — no processor was ever in an invalid state, because
NiFi has no way to know two of your relationships happen to write the same
file. Validity and correctness are different claims; this is the difference
Tier 4 exists to catch, and the reason M5 is not optional polish on top of
M1–M4.

## Retargeting is a bindings edit, not a code change

The package targets SQL Server. This deployment targets **Postgres**, and that
is one file:

```yaml
db_main:
  for: "Package.ConnectionManagers[localhost.AdventureWorksDW2014]"
  db_type: "PostgreSQL"
  url: "jdbc:postgresql://postgres:5432/ssis2nifi"
  identifier_case: lower
```

An SSIS connection manager says *"localhost, AdventureWorksDW2014, Integrated
Security=SSPI"* — none of which survives a move to NiFi. Windows integrated
security has no JDBC equivalent. So the package says *which* logical connection
each component uses, and the binding says what that resolves to at deploy time.
This is what SSIS project configurations already do.

## Three retarget hazards the tool now reports

Found by deploying, not by reading documentation. Each is a diagnostic the
converter emits **before** you deploy:

| code | what bites you |
|---|---|
| `LOOKUP_DATE_KEY_CAST` | The pipeline carries dates as strings. SQL Server implicitly casts string→date in a `WHERE`; Postgres does not, and the lookup fails with *"operator does not exist: date = character varying"*. |
| identifier casing | Postgres folds unquoted identifiers to lower case. `[dbo].[NewFactCurrencyRate]` from the `.dtsx` is a different, missing table — the flow deploys cleanly and fails at runtime. |
| `LOOKUP_FILTERED_REFERENCE` | The SSIS reference query has a `WHERE`, but `DatabaseRecordLookupService` reads the whole table. Extra matches are possible. |

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
make up                                     # this repo's own NiFi + Postgres (make setup first, once)
make analyze FILE=corpus/packages/L1.dtsx   # the report above
make ir      FILE=corpus/packages/L1.dtsx   # write out/<pkg>.ir.yaml
make convert FILE=corpus/packages/L1.dtsx   # write out/<pkg>.flow.json
make verify-import                          # import into a live NiFi, check validity
make deploy                                 # import, inject secrets, start
make verify-behavior                        # M5: feed a batch, diff against an independent oracle
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

- `~/Desktop/NIFI-FLOW` — a separate, hand-built reference implementation of a
  different pipeline. Its proven-working NiFi property keys informed the
  catalogue here early on, but this repo does not depend on it, import from
  it, or need it running — `make up` above is this tool's own stack, and
  `make verify-behavior` is this tool's own behavioural gate, independent of
  NIFI-FLOW's comparison harness.
