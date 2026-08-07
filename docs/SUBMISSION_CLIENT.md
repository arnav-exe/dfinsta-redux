# The trusted submission client

Status: built, 2026-08-02. `src/dfinsta_pipeline/submission.py`.
Read with [`docs/ADK_PIPELINE_PLAN.md`](ADK_PIPELINE_PLAN.md) (the authority on
gates) and [`docs/STAGE_4_DESIGN.md`](STAGE_4_DESIGN.md).

Until this existed, `execute_update` appeared only in tests. Every human
decision in the pipeline was hypothetical: the gates were durable, validated and
ledger-backed, and unanswerable. This is the program a human runs to answer one.

## The rule the whole module is built from

> The client re-derives the gate subject from recorded state, and refuses to let
> a human sign a hash it cannot reproduce.

The obvious client does the opposite. It reads the pending `GateRequest` off the
Workflow, asks a human yes or no, and copies the published hashes into a
`GateDecision`. That client is worse than none: the human's signature adds a name
to a number nobody checked, and the pipeline gains a human-approval record whose
only evidence is the thing being approved.

This project has already paid for that lesson once. A standalone replay CLI was
designed, reviewed, and deleted unexecuted because it self-asserted the
capability hashes it should have been verifying; `HANDOVER.md` still carries
"fresh review required for any alternate entry point". This client is an
alternate entry point, so the difference has to be structural rather than
stated:

| the rejected CLI | this client |
|---|---|
| asserted hashes | recomputes them and compares |
| issued its own receipts | issues nothing; the Activity records the decision |
| wrote authority | opens the ledger `mode=ro` and cannot write |

## What follows from the rule

**Re-derivation is the same code the Activity ran.** `_resolve_replay_verification`
is `prepare_replay_verification_gate_activity`'s body, reached through
`configure_runtime(..., read_only=True)`. Writing a second implementation would
have defeated the point: two derivations that agree prove something only when
they are the same derivation reading the same recorded state.

**The ledger cannot be written.** `Ledger(path, read_only=True)` opens SQLite
through `mode=ro`, skips every schema statement, refuses to create a missing
file, and guards all eight mutating methods. Two independent defences on
purpose — the Python guard gives a legible error, and the connection refuses
even if a future caller reaches past it. `RuntimeError` is the guard's type
because it is already in the stage retry policy's non-retryable list, so a
read-only ledger reached from an Activity fails closed instead of retrying until
the budget is gone.

**A gate with no registered resolver is refused, not trusted.** `PortRunWorkflow`'s
`phase-a-approval` binds `canonical_sha256(spec)` plus two operation outputs, and
the ledger indexes operations by content hash rather than by run — so a client
holding only a run id cannot reach them. It is therefore *not registered*, and
the client says so and stops. Refusing to answer is the correct behaviour;
falling back to the published hashes is the failure the module exists to prevent.

**The actor is the OS principal, not an argument.** An `--actor` flag would make
authorization a matter of typing. The actor comes from a 0600 file owned by the
invoking uid — the same trust boundary the ledger (a same-uid SQLite file) and
the content store (same-uid 0444 blobs) already rest on. This does **not** close
the plan's open item ("replace synthetic actor equality with authentication in a
trusted submission client"): a same-uid process can still write that file. What
it changes is that the actor is no longer a string the caller chooses. Claiming
more would be the more dangerous error.

**The decision's identity is a function of its content.** `decision_identity`
digests every `GateDecision` field except the schema tag and the two identifiers,
so identical answers produce identical ids and different answers produce
different ones. Identical ids are what make a resubmission deduplicate — measured,
not assumed: a retry from a *third* connection returns the byte-identical
receipt, and the Workflow never sees a second decision.

**The journal is what makes that reach past a crash.** `issued_at` is part of the
decision and therefore part of its identity, so re-running the CLI would
re-timestamp and mint a new decision the Workflow then refuses as a duplicate —
leaving a human unable to distinguish a dropped connection from a rejected
decision. The assembled decision is written to `<state-root>/submissions/` at
0600 *before* submission, and a later run resubmits those exact bytes. A journal
entry for a *different* subject is ignored rather than reused: the gate may have
been re-raised over changed bytes, and reusing the old answer would be a stale
approval arriving through the client's own cache.

**A read-only open is not filesystem-inert, and that is fine.** On a WAL database SQLite
creates `ledger.sqlite3-shm` and an empty `-wal` beside the ledger, and a read-only
connection cannot checkpoint them away on close, so both survive. The authority file itself
is byte-identical — asserted by size, nanosecond mtime *and* digest, with a positive control
proving a real write moves all three. Two operational consequences: the state-root directory
must be writable, and a `?immutable=1` "optimisation" would be a silent correctness bug,
because it makes a reader skip the `-wal` and answer from stale bytes. There is a test that
catches exactly that.

**A confirmation token proves a human read it.** `submit` requires at least
twelve characters of the *derived* subject hash. It is not a security control —
the subject is already verified — it is the difference between a human having
read what they are approving and a human having approved whatever was pending,
which is the same distinction the feature gate draws when it refuses to let a
missing disposition mean `ignore`. It quotes the derived hash rather than the
published one, so what a human confirms is what the client vouched for.

## Using it

```
python -m dfinsta_pipeline.submission \
    --state-root .pipeline-state \
    --principal ~/.config/dfinsta/principal.json \
    show <workflow-id>

python -m dfinsta_pipeline.submission \
    --state-root .pipeline-state \
    submit <workflow-id> --verdict approve \
    --rationale "the receipt binds the build I inspected" \
    --confirm 4f2a9c10bb3d
```

The principal file, mode 0600, owned by the invoking uid:

```json
{"schema_version": 1, "uid": 1000, "actor": "operator"}
```

## What it deliberately does not do

**It does not enforce the "at most three invalid responses" budget.** That cannot
live in a client, and it cannot live in a validator either: an Update rejected by
its validator never reaches History, so any counter there resets on worker
restart. Enforcing it means accepting the update into History and classifying
inside the handler — a deliberate inversion that deserves its own reviewed
change. Recorded, not overlooked.

**It does not authenticate to a remote authority.** See the principal note above.

~~**It does not answer the feature gate yet.**~~ **Closed 2026-08-03**, and the client now
answers three gates: `REPLAY_VERIFICATION_GATE`, `FEATURE_ASSESSMENT_GATE` and
`HOOK_RETIREMENT_GATE` (`submission.GATE_KINDS`). Both non-replay gates also have starters
(`assessment_record.raise_gate`, `retirement_record.raise_gate`), added 2026-08-08 — until then
the feature gate was answerable and unraisable, which is the same disconnection one link along.

## What this uncovered

Two things worth knowing before the next slice.

**`prepare_replay_verification_gate_activity` had never run against real recorded
state.** Every existing test stubbed it. Building the client's resolver meant
building the first test that drives the real derivation over a real ledger and
content store, which is now `tests/test_submission_resolver.py`.

~~**The feature gate has no producer.**~~ **Closed 2026-08-03 by
`src/dfinsta_pipeline/assessment_record.py`**, and the diagnosis below was exactly right about
what the missing link was. *Kept because the reasoning is the reusable part:* stage 4a computes an
assessment in the *driver* world while the gate expects it in CAS as a completed ledger operation
in the *Temporal* world, and the join is a design question about where those two meet rather than
a wiring task. What it needed, and now has, is a **run-keyed authority row**
(`recorded_assessments_v1`) — the operation tables are keyed by content hash, so without it a
client holding only a run id cannot reach the subject at all. `recorded_retirement_dockets_v1`
exists for the same reason, and `phase-a-approval` is still unregistered because it has no such
row.
