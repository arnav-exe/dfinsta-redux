# Differentials, per version pair

One file per comparison, named `<baseline>-<current>.jsonl`. Produced by

    python -m dfinsta_pipeline.differential \
        --baseline manifest/runtime_evidence/439.jsonl --baseline-version 439 \
        --current  manifest/runtime_evidence/440.jsonl --current-version 440 \
        --actor device:P3227J000775 \
        --out manifest/differentials/439-440.jsonl

**Why this is a separate directory and not a file beside the baselines.**
`manifest/runtime_evidence/README.md` states the rule and the reason: only
`runtime_probe` claims live there, because `differential.py` reads those files as
*measurements*. A differential filed among them would be read by the next
comparison as though it were one, and a comparison of a comparison is not
evidence about a port — it is evidence about this pipeline's own bookkeeping,
wearing the same shape.

**Why it exists at all.** Until 2026-08-06 nothing wrote a differential anywhere
durable. The 439 → 440 comparison was computed, printed, and lost — and
`differential` is one of the three kinds `EvidenceKind` requires of every hook
after a build, so no hook could reach release readiness while the one artifact
that could satisfy it was thrown away each time. That is the same
complete-but-disconnected shape as a gate with no producer.

**A differential claim's `version` is the CURRENT one.** "440 did not regress" is
a fact about 440, with `detail.baseline_version` naming what it was measured
against. It carries **no `build_sha256`**, deliberately: a claim spanning two
builds cannot name one of them, and `EvidenceLedger.record` preserves both of
those properties rather than overwriting them with the run's own attribution.

**What a thin baseline costs, measured.** 439 → 440 yields **2 passing claims** of
7. The other five are `shapes_disjoint`: 439's ledger recorded only feature-shaped
claims, and 440's strongest evidence for those hooks is *identity*, so there is no
pair of comparable shapes. Nothing can fix that retroactively — which is exactly
why `manifest/runtime_evidence/README.md` says to record every shape you can when
taking version N's evidence. The claim you skip is the comparison N+1 cannot make.
