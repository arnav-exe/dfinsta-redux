# Runtime evidence, per version

The baseline the differential reads. `python -m dfinsta_pipeline.differential
--baseline manifest/runtime_evidence/<N-1>.jsonl --current …` compares a new
version's device results against the last one's.

**Why this lives here and not under `work/`.** It did live under `work/`, which is
gitignored — so the one artifact that must survive *between* ports was the one
artifact a clean checkout did not have. The first differential ever taken (439 →
440) could compare only 2 of 7 hooks, and the reason was entirely on the baseline
side: 439's ledger holds no identity-shaped claims. A baseline that can be lost is
worse than a thin one.

Only `runtime_probe` claims are kept. Differential claims are *about* two versions
and belong to neither, so filing them here would make the next comparison read a
previous comparison's output as though it were a measurement.

**When taking version N's evidence, record every shape you can** — identity,
delta, absence — not just the one that answers today's question. The claim you
skip is the comparison N+1 cannot make. Produce them with
`python -m dfinsta_pipeline.record_runtime`.
