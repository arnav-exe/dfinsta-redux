# `reversals.jsonl` — the way back

**The file does not exist yet.** No decision has been withdrawn, which is the
ordinary state.

## Why it exists

This project had two one-way doors.

**A block.** A human rules `block`, `rulings.apply` adds the endpoint to
`semantic_deps`, and `feature_gate.VERDICTS` — `block`, `offer_toggle`, `ignore`,
`defer` — contains nothing that means *stop blocking*. That is worse than a
missing button: candidates are computed as *consumption surfaces not already in
`semantic_deps`*, so blocking one removes it from the population the gate draws
from and **the question can never be raised again**.

**A retirement.** `read_retirements` takes the earliest `effective_from` per hook,
deliberately, so appending cannot un-retire.

Both were correct as far as they went, and both left the escape hatch as a human
editing a tracked file or reverting a commit — the unreviewed edit the rest of
this design exists to prevent, and one that **erases** the decision instead of
recording its reversal. Decisions here are permanent; reversals are new decisions.

## The row

```json
{"schema_version": 1, "withdraws": "block", "subject": "feed/timeline_stream/",
 "original_decision_id": "decision-29d4…", "decision_id": "withdraw-block-…",
 "ruled_by": "arnav", "rationale": "Broke the feed on the device: endless spinner
 rather than an empty feed.", "recorded_at": "2026-08-09T10:00:00Z"}
```

`withdraws` is `block` or `retirement`. Everything except `recorded_at` is
required, and `ruled_by` may not be `agent`.

**It names the decision it withdraws, and the pairing is
`(original_decision_id, subject)` — not the id alone.** One gate decision covers
every candidate in its docket, so keying on the id would withdraw six rulings
when a human withdrew one.

**`effective_from` applies to a retirement withdrawal and not to a block one**,
and the asymmetry is real. `expectation` asks whether a hook was retired *as of a
version*, so restoring it must name one — and it must be later than the port the
withdrawal was decided from, so a hook cannot be restored into an already-assessed
port. A block lives in `manifest/hooks.json`, which is applied to whatever version
is being built and has no per-version semantics; a version field there would be a
number nothing reads.

## Both rows survive

Nothing is deleted. The history reads: blocked on the 8th for reason A, withdrawn
on the 9th for reason B. `python -m dfinsta_pipeline.reversal list` prints it.

To block an endpoint again after withdrawing it, rule on it at the gate. That is a
new decision, not an un-withdrawal, and it will appear as a candidate again
precisely because the withdrawal removed it from `semantic_deps`.

## The pipeline proposes; it never withdraws

An automatic reversal would let the system quietly undo its own protections. What
the pipeline should do is *raise a gate with evidence*, and the signals already
exist: a block whose hook never executes (`runtime_probe`), a device contrast that
shows the app broken with it on, an entry declared-but-unenforced for several
versions (`rulings --audit`), or an endpoint that has vanished from the app. The
last kind is the best sort — it fires by itself.

**That proposal path is not built.** Today a reversal is a human at a command
line, and the record is what makes that safe.
