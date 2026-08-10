# Redacted captures

One per session in `../observations/<version>.jsonl`, named for its `session_id`.
Produced by `tools/redact_capture.py --verify`, which keeps only the three kinds of
line the parser reads — the `!toggles` state the build reported, the watched request
paths, and each block header with the category line above it — and **refuses unless
`observation.parse` gives the identical answer for the reduction and the original**.

They exist because `../observations/` is committed evidence and the raw logcat is
gitignored, which left a store nothing committed could be checked against. A clone
can now re-derive every count:

    python -m dfinsta_pipeline.observation record --version 439 \
        --capture manifest/captures/<session_id>.log ...

Raw captures are ~1.4 MB each of the phone's whole log and are deliberately not
committed: nothing in the lines kept here identifies an account or its content, and
the rest of the log is unrelated app telemetry that does not belong in a public
repository.

**Not every capture becomes evidence.** `439-exploration-all-off` was taken twice.
The first attempt states no toggle state at all — the build logged it once per
process and `logcat -c` had cleared it — and `redact_capture --verify` refuses that
capture rather than committing a measurement that cannot say what it measured. The
three 441 captures predate the toggle line entirely and are refused for the same
reason; 441 must be re-measured.
