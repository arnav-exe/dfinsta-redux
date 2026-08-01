# Pipeline Implementation State

Resume point as of 2026-08-01, HEAD `ad924bc` on branch `port-430`.
Read with [`docs/ROADMAP.md`](ROADMAP.md) (authoritative progress) and
[`pipeline_flowchart.md`](../pipeline_flowchart.md) (design). This file is the
practical "how to pick this up" record: what exists, what is next, and the
specific things that will waste a day if rediscovered.

## Where the pipeline stands

Stage numbers refer to `pipeline_flowchart.md`.

| Stage | State | Where |
|---|---|---|
| 0 Load | manifest exists; Decision Memory not started | `manifest/hooks.json` |
| 1 Extract | manual apktool invocation | — |
| **2 Index** | **done** | `tools/indexer/build_index.py` (+46 tests) |
| 3-4 Feature discovery / report | **not started** | — |
| Gate 1 | not started | — |
| **5a Port Planner** | proven as an ad-hoc Workflow; not yet a stage | — |
| **5b Adversarial verifier** | proven ad-hoc; not yet a stage | — |
| **5c Mechanical validator** | **done** | `tools/resolver/validate_candidates.py` (+8 mutation tests) |
| Evidence ledger | **not started** — this is the key missing control | — |
| 6-7 Apply / Build | done, target-parameterized | `tools/port_430/build.py` |
| 8 Static verify | done, one per target | `tools/port_439/verify_439.py`, `tools/port_430/verify_apk.py` |
| 9 Runtime verify | manual only | `tools/device_validation/runner.py` |
| 10-11 Manifest update / report | not started | — |
| Durable orchestration | `ReplayRunWorkflow` registered, **never run for real** | `src/dfinsta_pipeline/` |

**The engine that makes hooks version-independent** is `src/dfinsta_pipeline/hook_manifest.py`.
Anchors are patterns with `<name:kind>` captures; payloads template off them.
**5 of 7 hooks resolve mechanically against both 430 and 439**, reproducing the
hand-authored anchors and payloads exactly. The 2 that do not are the `ui`-tier
settings hooks, declared `kind: "by_agent"`.

## Immediate next steps, in order

1. **Wire the Index into host search.** `by_literal` hooks currently need a caller to
   find the host; `api_surface.json` maps `literal -> [descriptors]` already. This makes
   the reels hooks fully automatic instead of needing a known descriptor.
2. **Teach a caller about `Resolution.already_applied`.** Nothing under `src/` imports
   the engine yet. Code written as `if not resolution.resolved: fail()` would treat a
   normal re-run as a failure.
3. **Build the Resolve stage** for the two `by_agent` hooks: k independent proposers, an
   adversarial verifier that sees only claims and is told to falsify, then the mechanical
   validator. The ad-hoc Workflow that did this for 439 is the reference.
4. **Build the evidence ledger.** Gate on *absent evidence*, never on self-reported
   confidence. Table is in `pipeline_flowchart.md`.
5. **Runtime probe runner** enforcing two-directional deltas, plus differential vs N−1.
6. **The driver** — one command, APK in, verified build out. None exists today.

## Constraints that cost real time to learn

**Obfuscated names are recycled.** `LX/05t2` exists in both 430 and 439 and is a
different class in each. Never join on a descriptor across versions. Index per version.

**Drawable ids are NOT stable.** Of 11,737 drawable names in both versions, only 103
keep their hex id — 99.1% are renumbered. Anchor on the drawable *name*; re-resolve the
id from that version's index. String ids are unresolvable entirely (sparse encoding
exposes ~555 of ~19,000).

**Static verification has a ceiling.** Three separate inert-patch incidents: the 340
`minshop`/`minishops` bug, the 430 settings hook (perfect statically, dead at runtime
because a MobileConfig flag picks a different implementation), and a verifier searching
DEX bytes for a smali string form that does not exist there. **A DEX stores method refs
as separate class/name/proto indices** — only type descriptors and bare method names are
literal strings.

**Probes are per-hook and must move both ways.** Logcat block-counting proves feed,
Explore, Stories — and is structurally blind to Reels, because `replaceReelsEndpoint`
blanks the endpoint upstream of the block. Zero signal in both directions means the probe
is invalid, not that the hook passed.

**Builds are semantically, not bitwise, reproducible.** `apktool` full rebuild stamps
every ZIP entry with the build date. Verify by assertions, never by hash equality against
a stored value. The graft preserves 16,399 stock timestamps and only stamps what it writes.

**439 needs `classes21.dex`** — it already ships `classes20`. `tools/port_430/build.py`
now takes `--custom-tree` and `--replace-dex`; defaults preserve 430.

## Operational gotchas

- **apksigner**: `--ks-pass file:` reads store *and* key passwords sequentially from the
  file. Passing `--key-pass file:` at the same single-line file hits EOF. For PKCS12 omit
  `--key-pass` entirely.
- **Hardlinked sandboxes** (`cp -al`) share inodes — writing into one corrupts the
  original decode. Verification against them must be read-only.
- **UI Automator** cannot reach idle while Reels plays or a blocked feed retries
  (`ERROR: could not get idle state`). Drive to a dialog, or use `screencap`.
- **A failed `uiautomator dump` leaves the previous file in place**, so `adb pull`
  silently returns stale data. `rm -f` first and assert freshness.
- **Never `git add` a file a background agent is writing.** A commit once shipped an
  agent's deliberate scratch mutation — 15 ledger authority calls regressed to a
  shadowable form — and the suite stayed green because no test exercised that defence.
- **Blind experiments need the answers physically removed**, not merely forbidden. `.git`
  alone makes every answer reachable, and harness context injection leaks commit subjects.

## Environment

```
python        .venv/bin/python   (3.13; system python3 is 3.14 and unsupported)
tests         PYTHONPATH=src .venv/bin/python -W error -m unittest discover -s tests
              665 tests, 1 expected skip
tool suites   tools/{indexer,resolver,port_430,reconstruction,release,device_validation}/tests
adb           $HOME/Android/Sdk/platform-tools/adb   device serial P3227J000775
build-tools   $HOME/Android/Sdk/build-tools/36.0.0
keystore      ~/.android/dfinsta-signing/dfinsta-release.keystore  alias dfinsta
              cert ee12866dc224d4f20a4f832cfdb0b9b6824ff6f4abbb1fefbaa522445aa3262d
```

Decodes (read-only, under gitignored `work/`):
`work/439-explore/stock-439`, `work/430-clean-build-v2/stock-430`.
Sandboxes outside the repo: `/home/arnav/dfinsta-holdout-clean`, `/home/arnav/dfinsta-439-map`.

Current shipped artifact: `work/439-build-v1/dfinsta_439.apk`,
SHA-256 `d3d5ebcfe79fe7b08cd2826aa8bb172ef3f3ee768fa52b9cab34c85266575637`,
installed and confirmed working on the phone.

## Known open items

- `expected_anchor_count` is fixed at 1 and now vestigial in the schema; multi-site hooks
  need one entry per site.
- An over-count of a marker reports "partially applied", which matches the applier's
  wording but is inaccurate; both counts are in the message.
- `Resolution.smali_path` is declared and never populated.
- `RESERVED_CAPTURE_NAMES` restates the regex lookahead and has no runtime consumer; a
  test binds them so they cannot drift.
- The `type` kind accepts object and primitive arrays but not every exotic descriptor.
- The registered `ReplayRunWorkflow` has never executed a real port — the real-evidence
  harness drives Activities directly.
