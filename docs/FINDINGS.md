# Auto-Patch Dry Run — Findings Log

Goal: Manually execute (as the proposed agentic pipeline would) the upgrade of DFInstagram
from Instagram 300.0.0.29.110 → 340.0.0.22.109, building scripts as needed, and either
complete one dry run or surface substantial blocking errors.

Date started: 2026-06-04
Working dir for source: `dfinsta-redux/dfinsta_source_1.3`
Experiment workspace: `dfinsta-redux/autopatch`

---

## Environment

| Tool | Version | Location |
|------|---------|----------|
| apktool | 2.9.3 | `C:\apktool\apktool.jar` (call via `java -jar` to avoid .bat pause) |
| jadx | 1.5.5 | `C:\jadx-1.5.5\bin\jadx.bat` |
| java | 26.0.1 | on PATH |
| disk free | 360 GB | C: |

APK sizes: IG300 = 57.7 MB, IG340 = 75.8 MB, IG430 = 127.1 MB.

---

## Findings / Blockers

### F1 — apktool.bat pauses on `Press any key to continue` (minor blocker, solved)
`apktool -version` via the .bat wrapper prints output then blocks on a `pause`. In a
non-interactive/background shell this hangs forever. **Workaround:** invoke the jar directly:
`java -jar C:\apktool\apktool.jar <args>`. The pipeline must never call the .bat wrapper.

### F2 — repo `instagram_source/` is a BUILD ARTIFACT, not a clean baseline (important)
`instagram_source/smali_classes7/com/dfinstagram/` exists — i.e. `build.ps1` has overlaid
newCode/overwriteCode into it. Diffing `overwriteCode/` against this would show ZERO changes
and be useless. The pipeline must extract its own clean IG300 (and IG340) baseline from the
original APKs. Doing that now in `autopatch/`.

---

### F3 — "remap every reference" is intractable; the patch is a tiny DELTA (pivotal)
Running the Phase-1 extractor over the raw `overwriteCode/`+`newCode/` reports **1142 external
classes (1049 obfuscated)**. That number is a red herring: `overwriteCode/X/1bI.2.smali` (3,984
lines) and `overwriteCode/X/2XJ.smali` (648 KB) are *complete verbatim copies* of large IG
classes with only a hook added. Their method bodies reference ~1000 other obfuscated classes
that are NOT part of the modification.

Grepping `overwriteCode/` for `dfinstagram` shows the TRUE delta — each file injects only
**1–3 `invoke-static` lines**:

| overwriteCode file | injected hook(s) |
|---|---|
| `X/5R8.smali`   | `new adv_settings(LX/5W9;)` + add to a collection (inject settings menu item) |
| `X/5RE.smali`   | `instance-of adv_settings` → `startDfInstagramSettings()` (handle click) |
| `X/1bI.2.smali` | `getBoolTrueEz("disable_suggested_posts")`, `modifyTigonBuffer(buf)` in `onBody` |
| `X/2XJ.smali`   | `checkVersion(act)`, `setFeedCache(coord)`, `clearFeedCache()` (3 sites) |
| `com/.../InstagramAppShell.smali` | `setContext(app)` in onCreate |
| `com/.../api/tigon/TigonServiceLayer.smali` | `throwIfBlocked(uri)` (uri from `LX/1Zi;->A08`) |
| `com/facebook/proxygen/JniHandler.smali` | `jniHandlerSendHeaders/SendRequest` |
| `com/facebook/proxygen/NativeReadBuffer.smali` | added FIELDS only (no dfinsta call) |

**Methodology correction (delta-driven, not copy-driven):** To port to 340, take IG340's OWN
copy of each host class (already has correct 340 names for all ~1000 incidental refs) and inject
ONLY the hook delta. The remap surface collapses from 1049 classes to:
  (a) ~8 HOST classes to locate in 340, and
  (b) ~15-25 symbols that actually appear in the delta lines / newCode (FeedCacheCoordinator
      + its fields, FlashFeedCache, LX/6Gp dialog, LX/5RE+LX/5W9, LX/1Zi, etc.)
This is the single most important design decision for a working pipeline.

### F4 — the project's own docs about obfuscated-class roles are UNRELIABLE
`dfinsta_source_1.3/CLAUDE.md` claims `X/5R8` & `X/5RE` "redirect `_read`/`_size` to
hooks.nativeReadBufferRead/Size". The actual diff shows 5R8/5RE handle the **advanced-settings
menu item** (`new adv_settings`, `startDfInstagramSettings`). The docs are wrong/stale.
=> The pipeline must derive ground truth from the binary diff, never from prose docs.

---

## Stage Log
(chronological; appended as I go)

- Stage 0 STARTED: extracting clean IG300 + IG340 baselines into autopatch/.
- Phase-1 extractor written + run (refs_catalog.json). Surfaced F3.
- True hook deltas located via grep. Surfaced F4. Remap surface is small & tractable.

### F5 — decompilation cost & scale (scalability data point)
`apktool d` on IG300 (57.7 MB) took **585 s (~10 min)** and produced **106,588 .smali files**.
IG340 (75.8 MB) is comparable/larger. Implications for the pipeline:
- A full grep over 106k files is the core search primitive — must measure it (next).
- Do NOT feed the tree to an LLM. The agent must work via grep/scripted search + reading only
  the handful of candidate files. Decompiling jadx-to-Java for the WHOLE app would be even larger
  and is unnecessary; only decompile specific classes to Java on demand for reasoning.
- Decode is a fixed ~10-min cost per version; fine for a per-release pipeline, run once & cached.

### F6 — apktool mangles case-colliding class filenames; suffix is NOT stable (important)
Obfuscated names collide on case-insensitive Windows FS. In `ig300/smali/X/`:
`1BI.smali`(=LX/1BI), `1Bi.1.smali`(=LX/1Bi), `1bI.2.smali`(=LX/1bI), `1bi.3.smali`(=LX/1bi).
apktool keeps the first-discovered as `Name.smali` and disambiguates the rest as `Name.N.smali`;
the in-file `.class` directive always has the correct case. So:
- The dfinsta `overwriteCode/smali/X/1bI.2.smali` filename is apktool's disambiguator for LX/1bI,
  NOT a typo. The dev hard-coded it.
- The `.N` suffix is assigned by DECODE ORDER and the set of colliding classes — both can differ
  across versions. In IG340, LX/1bI may land at `1bI.smali` or a different `.N`.
- **Pipeline rule:** never locate/write a class by assumed filename. Resolve a target descriptor
  (e.g. `LX/1bI;`) to its file by matching the `.class` directive, and when writing the patched
  class back, reuse the EXACT filename+dir apktool produced for 340. Writing to the wrong `.N`
  silently corrupts a different class.
- Also: classes are redistributed across dex files between versions (IG300 max smali_classes7;
  IG340 has smali_classes2..11), so the dex-dir is unstable too.

### F7 — grep is ~90s/pass over 106k files; build a one-pass index instead (efficiency answer)
A single full-tree ripgrep (`Lcom/facebook/tigon/TigonCallbacks;`) over IG340 took **93 s** (cold
cache) and matched 5 files. At that cost, 10+ fingerprint greps is painful. Solution: ONE rg pass
extracting structural directives (`.class/.super/.implements/...`) into an index, then every
fingerprint query is an instant in-memory/indexed lookup. IG340 decode = 670 s. Building the
header index now.

### F8 — raw diff is swamped by formatting noise; must NORMALIZE smali first (important)
Diffing clean IG300 vs `overwriteCode` produces huge noisy diffs even though the real change is
1-5 lines. Two noise sources:
- `.line NNN` debug directives differ wholesale (the dev's overwriteCode was disassembled by a
  DIFFERENT baksmali/apktool version than my 2.9.3; line tables format differently).
- disassembler comments differ, e.g. `const-wide v2, 0x8109c500001bd8L  # 3.03...E-306`
  (mine) vs the same line without the comment (dev's).
These are semantically INERT (debug info + comments don't affect bytecode). The pipeline MUST
diff a normalized form: strip `.line N`, strip comments, drop blanks. After that the delta is
exactly the dfinsta hook lines. Verified next with a normalizer.
Real hooks confirmed by this diff:
- InstagramAppShell.onCreate: `+ invoke-static {v0}, Lcom/dfinstagram/startapp;->setContext(...)`
- LX/5R8.A00(...): `+` 5-line block constructing `adv_settings` and adding to the return list.

### MILESTONE — full ground-truth patch extracted cleanly (Stage 0b DONE)
After normalization, `clean_diff.py` yields the COMPLETE patch = 8 files, 13 hook lines total:
| host class (IG300) | kind | injected | IG symbols the delta depends on |
|---|---|---|---|
| TigonServiceLayer (named) | URI block | 2 lines (`throwIfBlocked`) | `LX/1Zi;->A08:Ljava/net/URI;` |
| InstagramAppShell (named) | onCreate | 1 line (`setContext`) | none |
| com/facebook/proxygen/JniHandler (named) | 2 methods | 2 lines | none |
| com/facebook/proxygen/NativeReadBuffer (named) | add fields | 4 `.field` | none |
| LX/1bI (obf) | onBody | block + `.locals 5→8` + p1→v7 rename | `LX/1bI;->A02`,`A06` (TigonRequest) |
| LX/2XJ (obf) | 3 sites | 3 lines (checkVersion/setFeedCache/clearFeedCache) | anchors only |
| LX/5R8 (obf) | settings list | 5 lines (`adv_settings`) | `LX/5R8;->A01:LX/5W9;`, `A00:I` |
| LX/5RE (obf) | click handler | 5 lines (`startDfInstagramSettings`) | anchor `LX/5RE;->A01:LX/5W9;` |

Saved to `patch_recipe.diff`. The hardest re-application is LX/1bI (block + variable rename in a
method IG may have refactored). The rest are single-line anchored inserts.

Index built: `ig340_headers.txt` = 304,280 lines / 35 MB from one 102.8 s rg pass
(.class/.super/.implements only). Body-content fingerprints (magic consts, strings) still need
either targeted greps or a second index pass.

### MILESTONE — Stage 1 class location working (5/8 hosts instantly via index)
123,191 classes in IG340. Each fingerprint returns exactly ONE hit:
- `LX/1bI` → **`LX/1Vb`** (`smali/X/1Vb.1.smali`) via `.implements Lcom/facebook/tigon/TigonCallbacks;`.
  Verified: 1Vb has `onBody(Ljava/nio/ByteBuffer;)V`. Note suffix changed `.2`→`.1` (F6).
- Named classes by exact descriptor (1 hit each):
  - InstagramAppShell → `smali/com/instagram/app/InstagramAppShell.smali`
  - TigonServiceLayer → `smali/com/instagram/api/tigon/TigonServiceLayer.smali`
  - JniHandler → `smali_classes10/...` (MOVED from classes2 in 300 — F6 dex drift)
  - NativeReadBuffer → `smali_classes10/...` (MOVED)
Index lookups are instant (<1s) after the 103 s one-time build. This is the efficiency answer:
build index once, query many times; never re-grep the 106k tree per fingerprint.

### F9 — obfuscated FIELD remap needs type+finality+role matching (not just names)
The 1bI onBody hook references `A02` & `A06` (both `Lcom/facebook/tigon/iface/TigonRequest;`).
In 1Vb(340) the two TigonRequest fields are `A01` (non-final) & `A07` (final). Mapping by
(type, final?, declaration-order, role): A02(non-final)→A01, A06(final)→A07. Inferable from the
two class headers but needs reasoning, not a string substitution. Field names are NOT stable.

### F10 — the onBody hook is semantic surgery, not an anchored insert (hardest case)
Re-applying the 1bI hook requires: (a) insert the conditional-modify block after `:try_start_0`,
(b) rename buffer param `p1`→`v7` across the WHOLE method, (c) bump `.locals 5`→`8`. The number
and position of `p1` uses depends on the method body, which IG refactors per version
(e.g. helper `LX/0Ks;->A0B` in 300 is `LX/0AQ;->A0A` in 340). A naive line-insert breaks here.
Two pipeline options: (1) faithfully replay the register surgery (fragile), or (2) re-implement
the hook in a refactor-robust form — insert block + `move-result-object p1` to reassign the param
so the unchanged body transparently uses the modified buffer (no method-wide rename). Option (2)
is strongly preferable and is a key design recommendation.

### F11 — magic-constant fingerprints are NOT reliable (5R8 anchor failed)
The QE/mobileconfig param `0x8109bd00001bb4` used in LX/5R8 returns ZERO matches in IG340 (the
param ID changed between releases). So numeric magic-constant fingerprints can rot across
versions. Fingerprint priority should be: (1) stable type refs (com/instagram, com/facebook
class names), (2) string literals, (3) interface/superclass shape, (4) numeric consts LAST.

### F12 — multi-candidate fingerprints need a scoring/intersection step
`Lcom/instagram/mainfeed/network/FeedCacheCoordinator;` (stable name, used by the 2XJ hook) is
referenced by **26** classes in IG340. Locating the 2XJ-equivalent requires INTERSECTING signals:
references FeedCacheCoordinator AND has a `getRootActivity` call site AND an `onDestroy` site AND
is a Fragment-like controller. Single-anchor grep is insufficient for hosts like 2XJ; the pipeline
needs a scorer that ranks candidates by how many independent fingerprints they satisfy.
(LX/5R8 & LX/5RE — settings-list builder & settings-item base — remain the hardest: no stable
type refs, magic const rotted, only structural shape + the adv_settings/5RE inheritance relation.)

### MILESTONE — Stage 2 applied to real IG340 for 4 hosts (clean) + rename map discovered
`apply_patch.py` injected all 4 named-class hooks into real IG340 smali, idempotently:
- JniHandler: 2 calls, anchors identical to 300 (proxygen names stable) — ZERO remap.
- NativeReadBuffer: 4 fields added.
- TigonServiceLayer: throwIfBlocked block; field remap `LX/1Zi;->A08` → `LX/1Os;->A09`
  was DISCOVERED from the 340 body (the original code reads `v7, LX/1Os;->A09` two lines below).
- InstagramAppShell: setContext; the `this` register adapted from context (`v0`→`v9`).
Rename map so far (auto-derived):
  LX/1bI→LX/1Vb (class) ; LX/1Zi→LX/1Os & .A08→.A09 (TigonServiceLayer URI) ;
  1bI.A02/A06→1Vb.A01/A07 (TigonRequest fields, by type+finality).
Build of the patched tree launched (apktool b) to validate assembly.

### MILESTONE — patched smali ASSEMBLES (Stage 3, smali half proven)
First `apktool b` (default aapt2) log shows ALL 11 dexes assembled with no smali errors:
`Smaling smali folder into classes.dex ... classes10 ... classes11 ...` (clean). => the 4
host-class edits are valid Dalvik bytecode. Smali assembly of the whole 340 app is NOT slow.

### F13 — must rebuild with `--use-aapt1`; aapt2 fails on IG's layouts.xml (confirmed for 340)
The build then FAILED at resource compilation:
`res/values/layouts.xml:3: error: invalid value for type 'layout'. Expected a reference.`
This is the exact reason the project pins apktool 2.9.3 + `--use-aapt1`. I had dropped the flag
(guessed aapt2 ok for 340 — wrong). The failure is UNRELATED to the smali patch. Rebuilding with
`--use-aapt1`. Confirms the aapt1 requirement carries forward to IG340.

### MILESTONE — full patched APK BUILT (Stage 3 complete for host patches)
`apktool b --use-aapt1` → `dfinsta340_test.apk`, **83.6 MB, 191.6 s**. apktool reused the dexes
assembled in the prior run (which include the 4 host-class hooks) and packaged a valid APK.
=> The mechanical loop EXTRACT→DELTA→LOCATE→APPLY→BUILD is proven end-to-end on real IG340.
(Not signed/installed here — needs keystore+device — and newCode/resources not yet ported, so this
APK is "stock IG340 + 4 hooks", proving the mechanism, not a shippable mod. See remaining work.)

### F14 — shell quoting: ripgrep treats a pattern starting with `->` as a flag
My 2XJ intersection grep failed (`rg: unrecognized flag ->`) because the pattern began with `-`.
Pipeline must pass patterns via `rg -e <pat>` or after `--`. Minor but real automation footgun.

### MILESTONE — hooks verified PRESENT in the built APK's dex (end-to-end confirmed)
Extracted classes.dex + classes10.dex from `dfinsta340_test.apk` and confirmed the injected
symbols are in the dex string pools:
- classes.dex: type `Lcom/dfinstagram/hooks;`, `Lcom/dfinstagram/startapp;`; method `throwIfBlocked`, `setContext`
- classes10.dex: `jniHandlerSendHeaders`, `jniHandlerSendRequest`, `modifiedResponse`, `requestURI`
i.e. all 4 host hooks compiled into the APK. (dex stores type descriptors and method names as
SEPARATE strings — grep the pieces, not `Class;->method`.)

### MILESTONE — multi-signal intersection located 2XJ (F12 recommendation validated)
`FeedCacheCoordinator` ref (26 files) ∩ `getRootActivity()` caller (187 files) = EXACTLY 1:
`smali\X\2XI.smali`. So `LX/2XJ`(300) → `LX/2XI`(340). Confirmed 2XI contains all 3 hook anchors
(FeedCacheCoordinator check-cast, getRootActivity, onDestroy — 8 anchor hits). Single-anchor grep
was ambiguous (26); the 2-signal intersection is unique. This is the scorer the pipeline needs.

## RENAME MAP (auto-derived, IG300 -> IG340)
| 300 | 340 | how found |
|---|---|---|
| LX/1bI | LX/1Vb | implements TigonCallbacks (1 hit / 123k) |
| LX/2XJ | LX/2XI | FeedCacheCoordinator ∩ getRootActivity (1 hit) |
| LX/1Zi (req-info) | LX/1Os | read from 340 TigonServiceLayer body |
| LX/1Zi.A08 (URI) | LX/1Os.A09 | same |
| 1bI.A02/A06 (TigonRequest) | 1Vb.A01/A07 | type+finality match |
| TigonServiceLayer/InstagramAppShell/JniHandler/NativeReadBuffer | same (named) | exact descriptor |
| LX/5R8, LX/5RE (settings) | UNRESOLVED | no stable anchor; hardest case |

---

## ORACLE VALIDATION vs real dfinsta 1.4.1 (built on IG340) — one-time ground truth
Decompiled the pre-existing `dfinsta_1_4_1.apk`. Both it and my `ig340` derive from the same IG340
dex, so obfuscated names line up and I can check predictions directly.

### F15 — ripgrep skips .gitignored dirs by default (false-negative footgun)
First pass reported ALL dfinsta symbols ABSENT — wrong. The extracted-dex dir is under a
.gitignored path; rg honored it and skipped the files. `--no-ignore` (or `-uu`) fixed it. Any
search inside build/output dirs MUST pass `--no-ignore` or results silently lie.

### RESULT — predictions confirmed where hooks carried over; byte-for-byte on applied ones
| hook | my result | real 1.4.1 host | match |
|---|---|---|---|
| throwIfBlocked | TigonServiceLayer + field `LX/1Os;->A09` | TigonServiceLayer, `LX/1Os;->A09`, regs v7/v0 | **IDENTICAL smali** |
| setContext | InstagramAppShell, reg v9 | InstagramAppShell, reg v9 | **IDENTICAL smali** |
| setFeedCache/clearFeedCache/checkVersion | `LX/2XI` (multi-signal intersection) | `LX/2XI` | **class CONFIRMED** |
| modifyTigonBuffer (1bI→1Vb) | `LX/1Vb` | **hook DROPPED in 1.4.1** | n/a (1Vb still the TigonCallbacks class) |
| jniHandlerSendHeaders/Request | JniHandler (named) | **hook DROPPED in 1.4.1** | n/a |
| settings menu (adv_settings via 5R8/5RE) | UNRESOLVED (couldn't fingerprint) | **REDESIGNED**: 1.4.1 uses its own `com/dfinstagram/SettingsWrapper`, no longer injects into the obfuscated IG settings-list class | vindicated |

The two hooks my pipeline FULLY applied (throwIfBlocked, setContext) match the human developer's
hand-written 1.4.1 patch byte-for-byte (anchor, remapped field, registers). The hardest hooks I
flagged as fragile (Tigon buffer surgery, proxygen JniHandler, IG-settings-list injection) are
EXACTLY the ones the human dev dropped or redesigned for 1.4.1 — independent confirmation that
those are the brittle parts, not just hard for an agent.

---

## VERDICT & REMAINING WORK
DRY RUN OUTCOME: the mechanical pipeline EXTRACT→DELTA→LOCATE→APPLY→BUILD works end-to-end on real
IG340 and produces a valid APK whose injected hooks match the human dev's where they overlap.
PROVEN: clean-baseline extraction; normalized delta extraction (13 hook lines); class location via
index + interface/intersection fingerprints (1bI→1Vb, 2XJ→2XI, named); field remap by type+finality
(1Os.A09); anchored application; dex assembly; APK build; in-APK verification; oracle match.
NOT DONE (documented, not blocking the thesis):
- Resolve LX/5R8/LX/5RE purely structurally (no stable anchor; magic const rotted). In practice the
  human dev abandoned this injection too — suggests the pipeline should prefer robust hook points.
- Apply the 1Vb onBody register-surgery hook (designed: insert + `move-result-object p1`, no rename).
- Port newCode/* obfuscated refs (LX/6Gp dialog, adv_settings parent, FeedCacheCoordinator lambda
  fields A01/A08) and resources, for a fully functional mod + sign + device test.
### F16 — 1.4.1 REMOVED the whole response-rewrite subsystem (dropped, not renamed)
1.4.1 `com/dfinstagram/hooks` defines only: handleStartActivity, openBrowserThatDoesNotSuck (NEW),
str2Bytes, throwIfBlocked. ZERO occurrences in the entire APK of: feed_recs, pagination_source,
modifyFeedResponse, modifyTigonBuffer, nativeReadBufferRead, NativeReadBuffer, disable_suggested_posts.
=> The "strip suggested posts by buffering+rewriting the JSON response" feature (Path B) and the
redundant Proxygen request-block (JniHandler) were fully removed. 1.4.1 keeps only the robust
URL-blocking path (throwIfBlocked@TigonServiceLayer) + lifecycle/feed-cache + own SettingsWrapper.
1.3's own README already said "check if JniHandler can be removed" — removal was planned.
1.4.1 = re-scoped release (simpler core + new features: analytics, backup, external browser), NOT a
1:1 port. Lesson: the fragile/high-maintenance hooks are exactly the ones humans shed.

KEY DESIGN TAKEAWAYS for the pipeline:
1. Delta-driven, never copy-the-class. 2. One-pass structural index, not repeated 90s greps.
3. Fingerprint priority: stable type refs > strings > interface/super shape > numeric consts (rot).
4. Multi-signal intersection/scoring for ambiguous hosts. 5. Resolve files by `.class` descriptor,
   reuse apktool's exact 340 filename (case-collision `.N` suffixes are unstable). 6. Normalize smali
   (strip .line/comments) before diffing. 7. Re-implement hooks robustly (param reassign) over
   replaying register surgery. 8. Always `--use-aapt1`, `--no-ignore`, and `java -jar apktool.jar`.
