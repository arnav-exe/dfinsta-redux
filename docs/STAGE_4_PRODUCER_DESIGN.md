# Joining stage 4a to the feature gate

A design note, 2026-08-03, written before any code. It supersedes the one-line
framing that had been carried in `docs/IMPLEMENTATION_STATE.md` — *"stage 4a
computes an assessment in the driver world while the gate expects it in CAS"* —
which turns out to be generous in a way that matters. See F1.

Everything marked **[measured]** was run read-only against the real tree at
`cf612ad`, not inferred.

## A. What the gate needs in CAS, and in what form

Two objects, and they are not the same thing.

**A.1 The blob.** One CAS blob whose bytes are the canonical JSON of
`assessment.report(...)` (`assessment.py:418-435`). Its digest is what
`FeatureGateRequestV1.assessment.sha256` pins and what
`FeatureDispositionsV1.assessment_sha256` must equal (`feature_gate.py:609-610`).

**[measured]** against `work/index-439` + `manifest/hooks.json`: canonical bytes
3,696 / 3,844 / 3,831 on 430 / 439 / 440, with 4 candidates on each and identical
candidate ids. 439 digest `e57b3034…`. Byte-identical across `PYTHONHASHSEED`
values and repeated calls. The document contains no absolute paths. Stage 4a's
whole input is `api_surface.json` (6.2 MB) plus the hook list; it never reads
`structural.jsonl` (64 MB), and `assess()` takes **0.00 s** after a 0.05 s lazy
load.

**A.2 The `ArtifactRef`, field by field.** The request pins an *exact*
`ArtifactRef` of kind `feature-assessment-v1` (`feature_gate.py:256`), and every
field of it is inside the subject hash because `canonical_sha256` recurses through
dataclasses (`contracts.py:16-18, 30-37`). **[measured]** changing only
`producer_operation_id`, or only `input_hashes`, changes the request hash.

That is the most consequential fact here: **the subject binds the ledger operation
key.** Recording the blob digest somewhere reachable is not enough — the
re-deriving party must recover the *exact recorded* ref, which in practice means
`Ledger.require_completed_operation` (`ledger.py:371-401`).

**A.3 Where each request field would come from.** Of the seven fields, **three
have no source in the driver world at all** — `run_id`, `assessment`,
`allowed_actor` — and a fourth, `policy_revision`, exists in
`manifest/hooks.json` (value `"2026-08-01"`, **[measured]** valid under
`ID_PATTERN`) and is discarded by `load_manifest`, which returns `list[Hook]`
only (`hook_manifest.py:971-978`). `candidate_ids` comes from the document's own
order (`feature_gate.py:236-239`); **[measured]** all four match
`CANDIDATE_ID_PATTERN`. `gate_id` is derived and raises above 104 characters
(`feature_gate.py:145-160`).

## B. Why the driver can't just write it

**It can.** There is no authority mechanism that stops it, and claiming otherwise
would manufacture a difficulty. `Ledger.__init__` takes a `Path`;
`_require_writable` guards only on `read_only`; `begin_operation` wants a non-empty
`owner_token` and nothing more (`ledger.py:40-55, 185-187, 240-241`).
`_activity_owner()` is a *convention* used at eight call sites, not a requirement.
Determinism-under-replay is a Workflow constraint, not an Activity one — and
`driver.port()` is already written to be callable from inside an Activity
(`driver.py:528-533`).

The real obstacles are smaller and more specific:

1. **The driver has no run identity.** `run_id` is the only run-scoped name the
   gate hangs off. Minting one offline invents a durable identifier, which is a
   decision rather than a parameter.
2. **The driver has no state root**, and the moment it grows one, an `--out`-shaped
   tool is also a ledger writer.
3. **A second unsupervised writer changes the retry story.** The
   `owner_token`/`retry_safe` machinery exists so a Temporal *attempt* can adopt a
   prior attempt's effect (`activities.py:1371-1387`). A driver-written operation
   has no attempt to adopt from and no cancellation path to quarantine on — it gets
   the ceremony without the property.
4. **Two different objects are called "the ledger"** — `EvidenceLedger`
   (`evidence.py:423`, JSONL) and the SQLite `Ledger`.

None of these is binding. **The binding constraint is E**: whoever writes it must
write it somewhere a client holding only a run id can read back exactly.

## C. Options considered

- **C1a — driver writes the blob to CAS, an Activity adopts the digest.** Cheap,
  breaks nothing, and fixes the assessment as an *assertion*: the ledger records
  "these bytes were handed to me", never "these bytes are what stage 4a computes".
- **C1b — bytes ride into the Activity as an argument.** ~4 KB in History today,
  re-deserialised on every replay, and it puts the body where `feature_gate.py:9-13`
  says it must never go.
- **C2 — the driver becomes a Temporal client.** Breaks the driver's central
  property: it is offline and deterministic by design, and `--discover-hosts` is
  off by default precisely because it needs the network. This would make a running
  worker a precondition for porting an APK.
- **C3 — stage 4a moves into an Activity.** Needs the API surface admitted into CAS
  (6.2 MB, not the 70 MB index). Hard limit: the index **cannot** be produced under
  ledger authority — `CapabilityRole` is `Literal["install_framework","decode","build"]`
  and `docs/WORKFLOW_REGISTRATION_DESIGN.md:195-201` calls widening it "the one
  genuinely irreversible mistake available here". So the surface is *admitted*, not
  derived.
- **C4 — the gate reads a driver file.** Rejected on inspection:
  `ArtifactRef.__post_init__` enforces `uri == cas://sha256/<sha>` specifically so a
  ref cannot carry a filesystem path (`contracts.py:85`).

## D. Recommendation: C5, converging on C3

A small admission program in the Temporal world: admit the API surface into CAS,
**recompute** `assessment.report` from it, record the assessment as a ledger
operation, write a run-keyed authority row. Any driver-produced copy is a
cross-check that must agree byte-for-byte — never the authority.

The deciding measurement is that stage 4a is a pure, sub-millisecond function of
two modest byte strings, reproducible across hash seeds and three real Instagram
versions. **Recomputation-and-compare is available here and is free**, which is not
true of the replay build (hence `replay_gate.resolve_admitted_build` having to
*fetch* a receipt). Choosing adoption when recomputation is free spends the one
thing that distinguishes this gate from a rubber stamp. It reuses the project's own
rule: *two derivations that agree prove something only when they are the same
derivation reading the same recorded state* (`submission.py:23-27`).

Two things to state plainly rather than paper over:

- The `api_surface.json` blob is an **admission**, not a derivation. Nothing in the
  ledger can attest it corresponds to a real APK. Pin its `header.content_hash`
  (`hook_index.py:179-180`), **not** its own digest — **[measured]** the file embeds
  `generated_at` and an absolute `decode_path`, so a rebuild changes its bytes while
  the assessment output does not. Format gotcha: `content_hash` is `"sha256:0a2a…"`
  and `SHA256_PATTERN` wants bare 64-hex.
- The strict decoder that extracts `candidate_ids` **cannot live in
  `feature_gate.py`**: `tests/test_feature_gate.py:246-263` asserts that module's
  imports are exactly `{dataclasses, hashlib, re}` plus one relative import line.
  It belongs in `assessment.py`.

**The smallest verifiable first step** is not "wire it up". It is one test file that
fails today for a nameable reason: pin the document bytes (digest, size, candidate
order); record it for real in a temp state root through
`begin/record_effect/complete`; re-derive from the run id alone under
`read_only=True` and assert the request hash is byte-identical; and carry both
controls — a ledger-file fingerprint (size, mtime_ns, sha256) unchanged across the
read-only pass *plus* a control write that moves all three, the pattern from
`tests/test_submission_resolver.py:46-57`.

## E. What would make this gate unanswerable by `submission.py`

The client gets exactly one input from the world: `published.run_id`
(`submission.py:646-647`). `PortRunWorkflow`'s `phase-a-approval` was unanswerable
because you needed the spec to find the operation that would give you the spec.
That workflow was deleted on 2026-08-15 and the trap is kept here as the worked
example, because it is the shape to avoid rather than a thing to go and read.
Conditions that would reproduce that trap:

- **U1 — no run-id-keyed row.** `operation_events`/`operation_claims` have no
  `run_id` column (`ledger.py:59-77`). The one by-kind lookup,
  `operation_key_for_kind`, raises on the second run ever and is used nowhere in
  `src/`.
- **U2** — an operation key derived from something the client does not hold.
- **U3** — an `ArtifactRef` *reconstructed* rather than loaded: `producer_operation_id`
  and `input_hashes` are in the subject hash, so a guessed `input_hashes=()` makes
  the client refuse every genuine gate.
- **U4** — candidate ids supplied by a caller rather than read from the pinned
  document. `validate_submission` never fetches the assessment blob
  (`feature_gate.py:589-624`), so nothing downstream catches a candidate list that
  disagrees with the bytes the human was shown.
- **U5** — anything run-scoped only the driver knew: index path, decode path, clock,
  `--out`.
- **U6** — an over-long or malformed run id.
- **U7 — ~~and this one bites even if U1–U6 are solved~~. RETRACTED**, see
  `docs/ANSWERING_THE_FEATURE_GATE.md:148-155`, which records U7 as *"overstated on
  `DerivedSubject`"* and F10/U1 as already stale. `GateKind.payload` is the seam that closed it.
  **The tail of this bullet is still open** and is the one live item in this file: the CAS-write
  capability, restated at the end of §F. Marked 2026-08-08. The original text follows.
  The client structurally
  cannot submit this gate's response today. `submit_answer` sends a bare
  `GateDecision` (`submission.py:720-722`) while the feature gate's payload is
  `FeatureGateSubmissionV1 {decision, dispositions: ArtifactRef}`. `Answer` holds a
  verdict and a rationale (`:319-332`) with no way to express per-candidate rulings,
  and `DerivedSubject` is `frozen, slots=True` with seven fixed fields (`:217-241`).
  The human must also put the dispositions document *into* CAS — and note that
  `configure_runtime(read_only=True)` makes the **ledger** read-only while leaving
  the **content store writable** (`activities.py:143-150`). That is a convenient
  accident and should be made a decision.

**So the design must**: mirror `admitted_replays_v3` with a `run_id TEXT PRIMARY KEY`
table and a `..._handle_for_run` accessor documented as not-for-stages
(`ledger.py:92-103, 574-602`); reach the ref through `require_completed_operation`;
put candidate-id extraction in exactly one strict decoder both sides call; and
decide what happens to `DerivedSubject`, `Answer` and `submit_answer` **before**
writing the resolver. A resolver that derives a subject the client cannot submit is
a gate that is answerable in a test and unanswerable in practice.

## F. Surprises worth knowing before touching this

1. ~~**Nothing computes a stage 4a assessment — not even the driver.**~~ **Closed 2026-08-03**
   by `assessment_record.py`, which recomputes the document from admitted bytes, puts it in CAS
   and files the run-keyed row; the driver calls it from the `assess` stage. *The second half is
   still true and is deliberate:* `surface_diff.py` remains a standalone CLI the driver never
   invokes, and an audit settled that it must stay that way — 44 of its 105 candidates are
   permalink spellings of one already-blocked surface, and putting 105 candidates through a gate
   proven at 4 would turn "every candidate must be ruled on" into a rubber stamp.
   `tests/test_open_items.py` asserts it still has no importer.
2. **`driver.py` has an `assessments` artifact, an `assess()` and an `Assessment` —
   all of them the *other* ones**, from `.proposals` (`driver.py:73-80, 317-319,
   655-675`). Grepping the driver for stage 4a finds four hits and none is stage 4a.
3. **The one test touching a real index skips nearly everywhere and pins the wrong
   thing.** `RealIndexTests` (`tests/test_assessment.py:1322-1384`) skips when the
   gitignored 70 MB indexes are absent, runs against a hardcoded `blocking_hooks()`
   rather than `manifest/hooks.json`, and never pins `canonical_json(report(...))` —
   so the digest the whole gate hash-chain would root in has never been pinned.
4. **`validate_submission` is looser than the design reads.** The dispositions
   document is bound by digest *and* size; the assessment gets no equivalent check.
   A request pinning a ref to bytes not in CAS passes every clause. **The derivation
   is load-bearing precisely because the validator is not.**
5. **The "~100 candidates" premise is not what reality produces.** **[measured]** 4
   candidates, 3.7–3.9 KB, no private paths, on all three versions. The CAS design is
   still right — hash-pinned derivation symmetry is the real reason — but the size
   argument should not be what a future reader leans on.
6. **The submission client's content store is writable in read-only mode**, and a
   `--state-root` typo creates a `cas/` directory before the missing-ledger error is
   raised.
7. **`manifest/hooks.json` carries a `policy_revision` nobody reads**, while
   `decisions.py:140-145` makes it one of four dimensions a decision's reusability
   hangs on.
8. **`tests/test_feature_gate.py` never builds a ledger-shaped `ArtifactRef`** — its
   `producer` is a human-readable name that could never satisfy `record_effect`. All
   54 gate tests are pure-derivation tests; not one has seen a real recorded
   operation. Same gap `docs/SUBMISSION_CLIENT.md:141-144` records for
   `prepare_replay_verification_gate_activity`, arriving again one gate later.
9. **`Ledger.operation_key_for_kind` is dead in `src/` and is a trap if resurrected**
   — it raises the moment two operations share a kind.
10. `assessment.py` imports `json` and `Path` and uses neither — worth knowing,
    because the strict decoder D calls for is the natural thing to put there.
