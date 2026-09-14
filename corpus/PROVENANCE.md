# Corpus provenance

Every fixture here is recorded with where it came from and why it is in the set.
Nothing is invented: a converter validated against packages the author also
wrote proves only that it is self-consistent.

## Genuine packages

Authored in Visual Studio / SQL Server Data Tools, obtained from the public
repository `GoodmanNeil/SSIS-Examples` (Microsoft's own SSIS tutorial series,
"Lesson N"). Used as test fixtures only; not redistributed as a product.

| File | Origin | Dialect | Why it is here |
|---|---|---|---|
| `L1.dtsx` | Lesson 1 | friendly (`Microsoft.Package`, format 8) | The baseline slice. FlatFileSource → Lookup ×2 → OLEDBDestination, with a real `<paths>` DAG and Match/NoMatch/Error outputs. |
| `L1_guid_dialect.dtsx` | Lesson 1, older revision (`SSIS.Package.3`, format 6) | GUID | Same lesson, **authored against a different sample database**. Source of the GUID↔name table and of the topology-equivalence test. |
| `L2.dtsx` | Lesson 2 | friendly | Adds a `STOCK:FOREACHLOOP` container and a package variable. |
| `L3.dtsx` | Lesson 3 | friendly | Adds a FILE connection manager. |
| `L4.dtsx` | Lesson 4 | friendly | Adds a **Script Component** (`Microsoft.ManagedComponentHost`) and a FlatFileDestination, and wires a Lookup's error output. The natural manual-review case. |
| `L6.dtsx` | Lesson 6 | friendly | As L4, with a second variable. |

### A correction worth recording

`L1.dtsx` and `L1_guid_dialect.dtsx` were initially assumed to be one package
saved from two SSIS versions. **They are not.** Both are "Lesson 1" of the same
Microsoft tutorial, but they were authored against different sample databases
and genuinely differ:

| | `L1.dtsx` | `L1_guid_dialect.dtsx` |
|---|---|---|
| catalog | `AdventureWorksDW2014` | `AdventureWorksDW2012` |
| destination table | `[dbo].[NewFactCurrencyRate]` | `[FactCurrency]` |
| fast-load options | `TABLOCK,CHECK_CONSTRAINTS` | `TABLOCK` |

What they DO share is **structure**: the same component classes, wired the same
way, with the same branch semantics -- spelled in two dialects. That is the
property worth testing, and `schema.topology_digest()` is how it is stated.
Claiming byte-identical IR across these two would be claiming something these
fixtures cannot show. `tests/unit/test_ir_roundtrip.py` asserts both halves: the
topologies match, and the contents deliberately do not.

## Negative fixtures

Both are copied from `~/Desktop/SSIS/ssis_packages/`, which are emitted by
`ssis_sim/dtsx_builder.py` — Python f-string concatenation that imitates DTSX
without being it. They are here so the **refusal path is tested rather than
assumed**.

| File | Refused because |
|---|---|
| `negative_synthetic_wellformed.dtsx` | Declares namespace `http://www.microsoft.com/SqlServer/Dts`. Genuine packages use `www.microsoft.com/SqlServer/Dts` with **no scheme**. |
| `negative_synthetic.dtsx` | Not well-formed XML at all — an unescaped `&` in the component name `Sort & De-Dupe on order_line`. The generator does not XML-escape its output. |

**Both have been redacted.** As copied, they carried a live Postgres username
and password in an ODBC connection string. Committing that would have put a
working credential into git history permanently, so both values are replaced
with `REDACTED`. (The real values are in the source repo's `.env`; they are
deliberately not repeated here.) The fixtures test the *namespace* and
*malformed XML* refusal paths, neither of which reads the connection string, so
nothing is lost. If you refresh these from `~/Desktop/SSIS`, redact again.

These files also lack `componentClassID` values (every one is the empty string)
and have **no `<paths>` element**, so they carry no data-flow edges. A parser
written against them would share no code with one that reads a real package,
which is exactly why they are negative fixtures and not test input.

## Known gaps in the corpus

4 of the 7 target components are **not exercised** by these packages. This must
be closed before claiming coverage:

- no `DerivedColumn`
- no `ConditionalSplit`
- no `OLEDBSource`
- no `ExecuteSQLTask`
- no `DTS:PrecedenceConstraint` (every package is single-task, so control-flow
  edges are untested)

Covered today: FlatFileSource, FlatFileDestination, Lookup, OLEDBDestination,
both dialects, connection managers, variables, plus two natural refusal cases
(Script Component, ForEach container).
