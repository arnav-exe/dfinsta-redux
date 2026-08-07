# Stage 4 — Assessment and the addictiveness gate

Status: design, grounded in a calibration experiment run 2026-08-01 before any
code was written. Read with [`docs/ROADMAP.md`](ROADMAP.md) section 3 and
[`pipeline_flowchart.md`](../pipeline_flowchart.md).

Stage 3 (`surface_diff.py`) says *what changed*. Stage 4 has to say *what to do
about it*, and get a human to decide. The whole difficulty is that "addictive" is
a judgement, and this project's recurring failure is a confident wrong answer.

## What the experiment established

The premise was worth testing before building on it: is engagement mechanically
detectable in an APK? A signal vocabulary was fixed **first**, from product
mechanics — autoplay, infinite pagination, algorithmic ranking, prefetch, push,
variable reward, engagement telemetry — and only then measured against features
we already hold an opinion about. Positives: the surfaces DFInsta blocks.
Negatives: auth, settings, creator tools. Control: 40 random literals, because
the obvious confound is that positives are simply bigger.

**Six of the seven were noise.** The composite scored positive 1.43, negative
0.90, and *control 1.18* — the random group sat between the labelled ones. A
composite score would therefore have been an authoritative-looking number
measuring roughly "how instrumented is this class". `autoplay` matched nothing at
all, because a request-builder class is the wrong place to look for it.

Only `prefetch` separated, and only weakly: against a size-matched baseline
(literals held by ≥4 classes, of which 64% carry some prefetch, mean density
0.17) consumption surfaces score 0.38. Real, roughly 2×, not a discriminator.

**What does work is the app's own bookkeeping.** `LX/03Ez` on Instagram 439 is a
69-line constant holder with two arrays:

```
A01: feed/timeline/  feed/timeline_stream/  discover/topical_explore/
     feed/reels_tray/  feed/injected_reels_media/  feed/reels_media/
     feed/reels_media_stream/
A00: clips/discover/  clips/homecoming/
```

Exactly the continuous-content surfaces, and not one task endpoint. `LX/02zZ`
consumes them by matching a request's URI path against the list — structurally
the same predicate as DFInsta's own `throwIfBlocked`. That is Instagram
declaring which surfaces belong together: checkable, per-version, and produced by
the adversary rather than by us, which is the principle the evidence ledger
already runs on.

It paid immediately. **Four of those nine are not blocked by DFInsta** —
`feed/timeline_stream/`, `feed/injected_reels_media/`, `feed/reels_media/`,
`feed/reels_media_stream/`. Injecting Reels into the timeline is a textbook
engagement mechanic and it goes straight through.

## The design that follows

**Do not compute an addictiveness score.** The experiment says most of what such
a score would sum is noise, and a number hides that. Instead the assessment
carries two separated parts, and the separation is enforced by the types rather
than by convention:

**Measured** — each item independently checkable against the decode:

| evidence | strength | why it is worth anything |
|---|---|---|
| app-declared grouping | strong | Instagram lists this endpoint alongside known feeds |
| coverage gap | strong | it is in such a group and we do not block it |
| delivery branch | strong | tells a human the COST of blocking before they decide |
| co-location change | medium | how Shopping dissolved across endpoints |
| prefetch vs baseline | weak | ~2× baseline; labelled weak, never summed |

**Judged** — a reading of the above, by an agent or a human, recorded as opinion
with its reasoning attached and never merged into the measured half.

The gate then shows both. A human deciding needs the evidence to be inspectable;
the point of the split is that they can disagree with the judgement without
having to distrust the facts.

## Why grouping membership generalises

The obvious objection is that `LX/03Ez` is one class in one version and next
version it will be renamed. It will — but the *technique* does not depend on the
name. The stage looks for **any class that enumerates several known consumption
endpoints together**, which is a co-location query the Index already answers, and
then reads whatever else that class enumerates. New members of a known group are
the candidates. That is version-independent in exactly the way the `co_literals`
host fingerprint is, and it degrades honestly: if no such class exists in a
future version, the stage reports that it found no grouping rather than inventing
one.

## The gate

A survey of the existing gate machinery settled most of this. Two gates already
exist and are structurally identical: a `GateRequest` built from `workflow.now()`,
a `wait_condition` on a nullable decision slot, a synchronous `@workflow.update`
handler that only mutates state, and a validator carrying six clauses — liveness,
actor, six-way hash binding, timestamp well-formedness, validity window with a
5-minute skew bound, and idempotency. Durable multi-day blocking, timeout →
`blocked` (never implicit approval), append-only ledger persistence and
worker/server-restart survival all come for free.

Three things had to be decided rather than copied.

### The payload does not go through Temporal

An assessment covering ~100 candidates cannot enter History, which must stay
compact and free of large bytes and private paths. The existing answer is a
hash-pinned handle plus content-addressed storage, and `ArtifactRef` enforces
`uri == f"cas://sha256/{sha256}"`, so a ref structurally cannot carry a
filesystem path.

So: the assessment document goes to CAS; a **pure** derivation function produces
the gate request; the Workflow carries only its hash; and the admitting Activity
re-derives the request from recorded state and refuses if the hashes differ. The
derivation must touch no ledger, store, clock or environment — two independent
derivations have to agree byte-for-byte, and the whole authority model rests on
never accepting a caller's assertion of what was approved.

### One verdict is not enough, so the response is a document too

This is the real design problem. `GateDecision` offers one of
`{approve, reject, defer}` plus a rationale capped at 2048 characters, while this
gate needs a disposition **per candidate**. Stuffing that into `rationale` fails
the length check, and per the project's own rule the sanctioned move is a new
wrapper schema rather than new fields on an existing contract.

The response is therefore symmetric to the request: the human's per-candidate
dispositions go to CAS, and the decision binds that document's hash.

```
FeatureGateSubmissionV1 { schema_version, decision: GateDecision, dispositions: ArtifactRef }
FeatureDispositionsV1   { schema_version, assessment_sha256, policy_revision, dispositions[] }
```

Two rules make that safe, and both are the point rather than decoration:

- **`assessment_sha256` must equal the assessment the human was actually shown.**
  Without it a human could rule on one assessment and have the verdicts applied
  to a different one — the response-side form of "a stale approval cannot
  authorise changed bytes".
- **Every candidate must carry a disposition.** A candidate nobody ruled on
  blocks the run; it does not default to `ignore`. This is stage 4's version of
  the ledger's central rule that absence is never a pass, and it is the
  difference between a human having decided and a human having scrolled past.

Dispositions are `block`, `offer_toggle`, `ignore` or `defer`. `offer_toggle` is
the default shape for anything judged addictive, because the product rule is that
an addictive feature gets a switch rather than a silent removal.

### Five things the first implementation had to decide

Building the contracts surfaced gaps in the paragraphs above. Recorded as
decisions rather than left to whoever implements the next piece.

**The response document must be bound to the reference the human signed.**
Saying the decision "binds the dispositions' hash" is not enough on its own: a
caller could resolve one object and validate another, and every clause above
would then be checked against rulings nobody submitted. The canonical bytes must
hash to `dispositions.sha256`. Size is checked too but is a weak witness by
itself — `block` and `defer` are both five characters, so a document deferring
every candidate serialises to the same byte count as one blocking them all. The
digest is what binds.

**Candidate ids are not house identifiers.** `contracts.ID_PATTERN` forbids `:`
and `/`, while real candidate ids look like `gap:feed/timeline_stream/`. Reusing
the house pattern would have rejected 100% of real candidates — and only at the
moment a real assessment first met a real gate, which is the worst place to find
out. The gate has its own pattern allowing a namespace prefix and slash-separated
segments, and the trailing slash is significant because it is significant to the
endpoints themselves.

**A whole-gate `reject` or `defer` does not excuse per-candidate completeness.**
The wrapper exists precisely because one verb cannot express a hundred rulings,
so one verb cannot dismiss them either. A human who wants to punt marks every
candidate `defer` — which is itself a ruling, and leaves a record of what was
deferred rather than an absence.

**A zero-candidate gate is refused.** Completeness is satisfied vacuously by an
empty list, so without this the run would proceed on a human having ruled on
nothing. Stage 4a reporting no grouping is a legitimate outcome; raising a gate
about it is not.

**The derived subject binds the actor.** As first specified only the envelope
carried `allowed_actor`, which left "who may answer" checked solely by the
Workflow validator against History-resident state and never by the hash chain —
so the admitting Activity could not independently verify it. That breaks the
symmetry the rest of the design rests on, and `ReplayVerificationGrantRequestV1`
already carries its actor for the same reason.

### A second gate needs a new Workflow, not a new field

The ledger is genuinely multi-gate: `decisions` is keyed on `decision_id` and
`idempotency_id` only, `gate_id` is a real column, and neither `run_id` nor
`(run_id, gate_id)` is constrained — so no migration. But three *contracts*
(`WorkflowStatus`, `RunResult`, `ReplayRunResultV1`) assume one gate and one
decision per run, and gate-openness is currently a state-string equality test.

The established precedent is a separate `@workflow.defn` class with its own
status and result envelopes, chosen so old-History replay compatibility stays
trivially true instead of argued. Stage 4 follows it.

### Two divergences recorded rather than hidden

**The invalid-response budget is not implemented**, here or anywhere. The plan
mandates "at most three before the run becomes `blocked`", and an existing test
submits six invalid decisions then a valid one and expects it to succeed. It
cannot be fixed in a validator: an Update rejected by its validator never reaches
History, so a counter there silently resets on worker restart. Enforcing it means
accepting the update into History and classifying inside the handler — a
deliberate inversion that deserves its own reviewed change. Out of scope here,
and noted so it is a decision rather than an oversight.

~~**There is no trusted submission client.**~~ **Closed 2026-08-02, one day after this was
written** — `src/dfinsta_pipeline/submission.py`, design record `docs/SUBMISSION_CLIENT.md`. The
prediction in the last sentence held: it was the first thing built after the gate.

## What stage 4 must not do

- **No composite score.** Six of seven signals were noise; summing them would
  launder that into apparent authority.
- **No blocking on its own judgement.** The human decides; the stage produces
  evidence and a recommendation, and the recommendation is labelled as such.
- **No silent inheritance of a previous decision.** `docs/ADK_PIPELINE_PLAN.md`
  is explicit: a decision is reusable only while feature identity, delivery
  mechanism, evidence fingerprint and policy revision remain compatible. That
  predicate already exists in `decisions.py` and must be used, not reimplemented.
