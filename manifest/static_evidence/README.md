# Static build evidence, per version

The `static_verified` claims a port produced, one file per Instagram version.
Written by `dfinsta_pipeline.driver` at the end of a successful build, when the
run is labelled (`--version` and `--recorded-at`).

**Why this exists.** `static_verified` is one of the three kinds
`EvidenceKind` requires of every hook after a build, and until 2026-08-07 it was
written **only** to `<run>/evidence.jsonl` under gitignored `work/`. So the one
artifact needed to answer "is this port release-ready" survived exactly as long
as a scratch directory — and `dfinsta_pipeline.reaper` exists to delete scratch
directories. Recomputed from committed data alone, Instagram 441 read **0 of 7
release-ready**; with the run directory present it read 4 of 7. A check whose
inputs live in gitignored space is a check that silently stops working.

This is the same gap `manifest/differentials/` closed the day before, one
evidence kind over.

**Only `static_verified` lives here**, the way `runtime_evidence/` holds only
`runtime_probe`. The pre-apply kinds a run also produces — `anchor_unique` and
`registers_safe` — are deliberately not copied: they are re-derived from the
decode on every run, and the pre-apply gate already refuses to build without
them, so persisting them would duplicate a fact rather than preserve one.

**Appended, never rewritten**, following `record_runtime.append`: the ledger's
superseding rule is "a later claim wins", so a re-port adds rows and the history
of what was asserted stays on disk. Duplicate identical claims do not affect
readiness — verified.

**Every claim is attributed** — version, build hash and timestamp — so a claim
can be joined to the APK it is about. That is what makes a report assembled from
this directory, `runtime_evidence/` and `differentials/` mean anything.
