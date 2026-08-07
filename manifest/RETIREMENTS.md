# `retirements.jsonl` — the only legitimate way to lower the bar

**The file does not exist yet, and that is its current state, not an oversight.**
No hook has been retired. `dfinsta_pipeline.expectation` reads its absence as
"none recorded" and expects every hook that was release-ready on the previous
version to be release-ready on this one.

**Do not hand-write a row.** `dfinsta_pipeline.retirement` produces them, and
going around it skips every check below:

```
python -m dfinsta_pipeline.retirement candidates --version 441
python -m dfinsta_pipeline.retirement case --version 441 --hook <id> \
    --investigation <json> --out case.json
python -m dfinsta_pipeline.retirement rule --case case.json --verdict retire \
    --rationale "..." --ruled-by <you> --ruled-at <ISO8601> --out ruling.json
python -m dfinsta_pipeline.retirement publish --case case.json --ruling ruling.json
```

The two steps are separate because the middle one is a human's and the others are
not. A case is bytes anyone can reproduce from `(version, hook)` plus the
committed evidence; the ruling is bound to that case's hash, so editing the case
after it was signed makes `publish` refuse rather than apply a stale answer to new
evidence.

## Why a record and not a field

`dfinsta_pipeline.expectation` derives what a port owes from the previous port's
committed evidence. It never reads a target number, because a declared
expectation has exactly one repair when it fails, that repair takes one
character, and in a diff it is indistinguishable from a legitimate change:
edit `4` to `3`.

`Hook.status` in `hooks.json` is not the lever either, for the same reason one
level down — `"status": "active"` → `"status": "retired"` is that same one-line
edit, with no record of who decided, when it takes effect, or why. The manifest
says what a hook *is*; it is not a place to record a judgement about whether the
project still wants it.

So there are two ways, and only two, for a port to be released with fewer
release-ready hooks than the last one:

1. Make the hook pass again.
2. Append a row here.

## The row

```json
{"schema_version": 1, "hook_id": "replace_reels_discover_endpoint",
 "effective_from": "442", "decision_id": "retire-2026-08-08-discover",
 "ruled_by": "human", "rationale": "Instagram removed the surface in 442; the
 anchor now matches a dead code path and the feature it protected no longer
 exists.", "recorded_at": "2026-08-08T00:00:00Z"}
```

Every field except `recorded_at` is required and must be non-empty. The reader
refuses the row otherwise — a retirement that does not say who ruled and why is a
lowered bar with no author.

**`ruled_by` may not be `agent`.** An agent investigates a hook that stopped
passing and drafts the case for retiring it; that is the whole design of the
decision gate. But if the proposer could also *sign* the retirement, the cheapest
route past a red build would be for the thing being measured to rule that the
measurement no longer applies.

**`effective_from` is the first version that stops expecting the hook, and it is
derived rather than chosen.** It is always the version *after* the one the case
was built from, and there is no flag for it: a retirement that could name its own
effective version could be backdated onto the port that exposed the drop, which
is "approve your way out of a red build" wearing a date. A retirement ruled from
441's evidence takes effect at 442, so 441's failure stays on the record.

**Earliest wins, not latest.** The ledger's usual rule is that a later claim
supersedes an earlier one, and it is wrong here: appending a second row for the
same hook with a later `effective_from` would un-retire it for the versions in
between, turning a permanent record into an editable one.

## What still watches a retired hook

Nothing here removes a hook from the manifest, stops it being applied, or stops
its probe reporting. A retirement says only "do not expect this to be
release-ready". If a retired hook starts passing again, `expectation` prints it
as **STILL PASSING** — not an error, but worth a human seeing, because the case
for retiring it was probably made when it was not.
