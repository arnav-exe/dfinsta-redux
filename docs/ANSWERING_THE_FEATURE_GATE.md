# Answering the feature gate

A design note, 2026-08-03, written after the producer landed
([`docs/STAGE_4_PRODUCER_DESIGN.md`](STAGE_4_PRODUCER_DESIGN.md)) and before
`submission.py` was touched. Its purpose is to say what changes, what does not,
and which of the two is the safety property.

## The human decides ten values. Everything else is derived.

For a 4-candidate gate — **measured**: four candidates on 430, 439 and 440, with
identical ids — the human supplies a gate verdict, a gate rationale, and four
`(verdict, rationale)` pairs. That is all.

Derived, and never typeable: the candidate ids and **their order**, the assessment
digest, the policy revision, the actor, and all three subject hashes. They come
from `assessment_record.resolve_with(...)`, which reaches the `ArtifactRef`
through `require_completed_operation` and reads the candidate ids out of the
pinned bytes with the single decoder in `assessment.py`.

A complete 4-candidate dispositions document is **795 canonical bytes**.

## What changes in `submission.py`, and what must not

**`DerivedSubject` does not change.** The feature gate's subject is one hash, and
the existing seven fields hold it — `admission_sha256` and `prepared_sha256` are
set to the request hash, exactly as the replay gate already does. Adding
`candidate_ids` here would trip the structural test that forces every field to be
compared or exempted, for a value already inside the hash that *is* compared.

**`GateKind.resolve` keeps its single argument.** One argument — the run id — is
what makes *"a subject unreachable from a run id is unregisterable"* structural
rather than conventional. It is the entire reason `phase-a-approval` is refused.
Widening it re-opens that trap for every gate at once.

**`Answer` gains one optional field**, `detail`, because `Answer` is documented as
the only part of a decision a human supplies and for this gate the human supplies
more. The load-bearing part is the default: **a kind that does not understand a
detail must refuse, never drop it.** A human who supplies rulings and gets a bare
`approve` submitted is this module's whole reason for existing, arriving through
an ignored argument.

**`GateKind` gains a `payload` builder** with a default that returns the decision
unchanged and refuses a non-`None` detail. Today's kind constructs positionally
and is unaffected.

## The three things that can silently substitute a human's rulings

1. **The journal stores a `GateDecision` and nothing else.** A resubmission would
   pair a journalled decision with a *freshly built* dispositions document. If
   either side moved, the human's rulings are replaced without a word. Fixed by
   recording `payload_sha256` in the journal and comparing it, refusing with the
   same message as every other journal mismatch.

2. **The Temporal update id is a digest of the decision alone.** Two different
   dispositions documents under one decision therefore share an update id;
   Temporal returns the first receipt and the second document is dropped, and the
   client prints `accepted True`. The journal check catches this on the same host
   and not from another machine. Fixed by making the update id cover the payload.

3. **The candidate list could become an input rather than a derivation.** This is
   the `phase-a-approval` trap in its per-candidate form, and the reason it is
   dangerous is that nothing downstream catches it:
   `feature_gate.validate_submission` **never fetches the assessment blob**. It
   compares the ruled set against `request.candidate_ids` — which is whatever the
   deriving side pinned. If both sides pinned a list the bytes do not contain,
   every clause passes and a human has ruled on a document nobody read.

   The checks that prevent it: candidate ids come only from the strict decoder
   over bytes fetched by the recovered ref; the client builds the dispositions by
   **iterating the derived tuple and looking each id up in the human's file**,
   never by iterating the file; a key not in the tuple is refused by name and a
   missing one likewise; and the client runs the admitting side's own
   `validate_submission` on its own submission before sending it. If it cannot
   admit its own answer it refuses, rather than making a human's decision fail at
   the worker.

## Who writes the dispositions document to CAS

The client does, and that should be a **named capability** rather than the current
accident whereby `configure_runtime(read_only=True)` makes the ledger read-only
and leaves the content store writable.

The justification is that CAS is not authority. `put_blob` touches no ledger
table; an `ArtifactRef` acquires provenance only when `record_effect` binds it to
an operation key. Every read re-verifies digest, size, mode, owner and inode. So
the write grants availability and never meaning — and the client must still make
no ledger write at all.

## Using it

    submission show <workflow-id>                     # the derived subject
    submission show <workflow-id> --assessment        # read the evidence itself
    submission show <workflow-id> --rulings-template > rulings.json
    $EDITOR rulings.json                              # 4 verdicts + 4 rationales
    submission submit <workflow-id> --verdict approve \
        --rationale "…" --rulings rulings.json --confirm <prefix>

`describe` prints the derived subject and not the candidate list; the candidates
appear under `--assessment` and `--rulings-template`, both of which come from the
derived request.

**The template is invalid as emitted, deliberately.** Every candidate verdict but
`ignore` requires a rationale, so submitting the unedited skeleton is refused and
a human cannot answer this gate without typing something for each candidate. A
template that submitted cleanly as-is would let someone approve four rulings they
never made — the same failure as a client copying the Workflow's hashes, one level
down.

Note also that a **candidate** verdict is `block / offer_toggle / ignore / defer`
while the **gate's** verdict is `approve / reject / defer`. Both appear on the same
command line, so reaching for the wrong one is the ordinary mistake; it is refused
by name, naming the candidate and both vocabularies.

Yes, that is a hand-edited JSON file, and at 795 bytes and four rows it is a
smaller edit than the rationale string. Two properties make it safe rather than
merely tolerable: the template's ids come from the derived request, so a renamed,
dropped or added id is refused **by name** before anything is signed; and the file
is a *mapping consumed in the request's order*, so hand-editing cannot change the
order the digest depends on.

`--assessment` is not decoration. Without it the human confirms a hash over
evidence they never read.

## Built, and proven on the real 440 gate

All of the above landed. `FEATURE_ASSESSMENT_GATE` is registered — the second kind
this client has ever carried, and it joined only once `recorded_assessments_v1`
made its subject reachable from a run id.

Driven against the recorded Instagram 440 assessment, with no Temporal server:

    gate_id       port-440-feature-assessment-gate
    subject       fc575a3617870dcadb29b12463158c5c67ac329c9def139483d61cc65b63fbf3
    payload       FeatureGateSubmissionV1
    dispositions  cas://sha256/781fa762…  674 bytes
    → feature_gate.validate_submission ran inside the builder and did not raise

and every refusal fires by name:

    unknown candidate: Rulings name a candidate this gate does not cover: gap:feed/invented/
    missing ruling:    No ruling for candidate gap:feed/timeline_stream/
    not an object:     This gate needs a ruling for every candidate: pass --rulings …

The measured document is **674 bytes**, not the 795 estimated above — the estimate
assumed longer rationales. Either way it is smaller than the `--rationale` string
it accompanies.

## What this corrects in the earlier note

`STAGE_4_PRODUCER_DESIGN.md`'s U7 is **overstated on `DerivedSubject`** — the seven
fields are not a blocker — and **understated on the journal**, which it does not
mention at all and which is the only member of the set that can silently
substitute a human's rulings. Its F10 and U1 are already stale: the strict decoder
and the run-keyed row now exist.
