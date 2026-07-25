# Blog Notes: Teaching Agents to Patch Instagram

Working notes for an informal development post. This is a journal and source-material dump, not polished documentation.

## Possible Premise

Instagram changes constantly, while DFInsta works by patching Instagram's compiled Android bytecode. Every release scrambles many internal names, so an update that sounds like "copy the old changes over" is actually a reverse-engineering problem involving more than 100,000 smali files.

The fun question: how much of that can be turned into a repeatable multi-agent pipeline without giving an LLM permission to freestyle-edit an APK?

## Tone

- Explain unfamiliar Android terms before using them heavily.
- Keep the journey chronological, including wrong assumptions and failed approaches.
- Prefer concrete scale, timings, and examples over abstract architecture language.
- Leave room for informal asides and screenshots rather than making this read like a research paper.

## Story Beats So Far

### The misleading starting point

The repository contains DFInsta 1.3 source, but it is not source code in the normal Android-app sense. It is custom smali plus complete copies of a few patched Instagram classes. The build decompiles an official APK, overlays those files, rebuilds it, and signs the result.

At first glance, porting the patch appears to require remapping more than a thousand obfuscated references. The earlier 300-to-340 experiment found the important trick: almost all of those references belong to copied Instagram code. The real 1.3 patch was only eight host classes and a handful of injected instructions.

Potential line: the first major optimization was not a smarter model; it was realizing we were asking the wrong diff question.

### Why 1.4.1 comes before Instagram 430

We have both stock Instagram 340 and a real DFInsta 1.4.1 APK. That pair is an oracle: because both APKs share the same Instagram base, their meaningful differences reveal what the human developer actually kept, removed, or redesigned.

Jumping directly from the older 1.3 source to Instagram 430 would carry forward brittle code that 1.4.1 already abandoned. Reconstructing 1.4.1 first gives the future pipeline a cleaner baseline and a known answer against which its output can be checked.

### Deterministic spine, agents at the seams

APK extraction, indexing, normalization, patch application, building, and symbol verification are ordinary deterministic programs. Agents are useful only where the evidence is ambiguous: identifying a renamed host, understanding a changed field's role, assessing a new feature, or diagnosing why a semantically reasonable patch failed.

This distinction is likely the central architecture point of the eventual post. "Multi-agent" does not mean replacing every shell command with a chatbot.

### Tooling reality

The project pins apktool 2.9.3 because these Instagram resources require the older aapt1 path. Android Studio supplied `adb`, `zipalign`, and `apksigner`, but did not put them on the shell `PATH`. The first reproducibility task was therefore simply locating every tool and recording its exact version/path.

## Facts Worth Capturing During Development

- Decode and index timings, output sizes, and class counts.
- Examples where filenames or DEX directories changed but descriptors remained useful.
- The first hook successfully recovered from the 1.4.1 oracle.
- Failed fingerprints and why they failed.
- Screenshots of the workflow, Temporal UI, reports, and human approval gates.
- Before/after snippets showing a huge host class collapsing into a tiny normalized delta.
- The first successful 340 rebuild, first install, and first verified blocked feature.
- Anything surprising, annoying, or funny enough to make the final post feel like a development story.

## Glossary Candidates

- APK: the installable Android application archive.
- smali: a readable assembly-like representation of Android DEX bytecode.
- hook: a small injected call that redirects behavior into DFInsta code.
- oracle: the known human-built modified APK used to validate reconstructed behavior.
- fingerprint: stable evidence used to relocate a class after obfuscation changes its name.
- anchor: the exact semantic location where a hook should be inserted.

## Timeline

### 2026-07-25

- Reviewed the repository, corrected stale architecture documentation, and established that the sensible route is 1.4.1 reconstruction, then a 430 port.
- Reviewed the proposed pipeline. The sequence is viable, but durable orchestration should own execution while LLM agents handle only ambiguous reasoning.
- Started the 1.4.1 reconstruction with fresh stock and modified APK decodes in an isolated ignored workspace.
- Both decodes finished in roughly 32 seconds. The previous Windows experiment recorded around 10 minutes for a similar decode, making environment and filesystem performance an unexpectedly visible part of the story.
- The first new script failed in zero seconds because Linux had `python3` but no `python`. A suitably unglamorous reminder that automation projects often begin by discovering which spelling of Python the machine understands.
- The first real inventory found 92 added classes, which initially sounds like a fairly large custom implementation. Thirteen were DFInsta classes; the other 79 were a bundled crash-reporting library. Even "new classes" need classification before they become useful evidence.
- Fresh analysis corrected the old conclusion that 1.4.1 merely kept URL blocking and removed brittle response code. It did remove the old response rewriter, but replaced it with 19 host patches that swap Instagram endpoint constants for tiny preference-aware methods. Searching only for old hook names had missed the new architecture entirely.
- The endpoint trick is wonderfully direct: replace `const-string "feed/timeline/"` with a method that returns either `"feed/timeline/"` or `""`. No giant JSON surgery, just make the internal route disappear when its toggle is enabled.
- One less-obvious oracle finding: app startup sends Android ID and the DFInsta version to Amplitude. Reconstructing what the old release did and deciding what a future release should do are deliberately separate questions.
- The modified APK produced 188 changed classes after normalization, but only 23 directly reference DFInsta. Many of the rest are bytecode round-trip noise: duplicate exception-table entries disappear or string instructions switch to their `jumbo` encoding after the DEX string pool changes. A raw diff can be technically accurate and still tell the wrong story.
- I initially hand-counted the 92 added classes as 12 DFInsta plus 80 ACRA. The source bootstrap reported 13 plus 79 and forced the correction. This is a tiny example of the broader rule for the eventual pipeline: prose explains evidence, but generated inventories own the numbers.
- The first complete reconstructed APK built successfully: all 11 DEX files plus resources, 84 MB in total, in about two minutes and forty seconds. Unlike the earlier four-hook experiment, this build includes the recovered custom classes, ACRA dependency, resources, manifest entries, and all 23 direct host patches.
- `apksigner` then announced `DOES NOT VERIFY`, which sounds dramatic but was exactly right: this was intentionally the unsigned intermediate. Assembly, signing, installation, and behavior are separate gates so one green check cannot impersonate the others.
- After alignment and debug signing, `apksigner` verified the APK correctly. `adb devices` then showed absolutely nothing, so the pipeline reached its first genuinely physical dependency: eventually a phone has to be plugged in.
- The first complete build still used exact copies of 23 oracle host classes as scaffolding. Once that proved the recovered custom code and resources were complete, those copies were deleted and replaced by 38 explicit operations: 30 endpoint substitutions and eight anchored lifecycle/settings/startup operations.
- Running the patchers twice became a useful tiny test: the first run reported every operation applied; the second reported every operation already present. Idempotency is boring right up until an interrupted automation run applies half a smali hook twice.
- The source then built again without any complete patched Instagram classes. That was the real reconstruction milestone: stock 340 plus small declared deltas produced an APK with the same required hook contract as the human-built oracle.
- Finally, the whole deterministic sequence was wrapped in one command and run from scratch again. It prepared the tree, applied all 38 operations, assembled 11 DEX files, built resources, and verified the hook contract in just over two minutes. The agentic pipeline now has a boring, testable mechanical core to call rather than a prompt that says "please patch Instagram carefully."
- The connected phone unexpectedly contained the exact historical oracle APK, byte for byte. That turned a signing obstacle into a behavioral reference device: capture what the old app actually does before replacing it with the reconstruction.
- The first attempt to open settings directly through ADB failed because the activity is not exported. The next assumption was also wrong: long-pressing the bottom Profile tab opened Instagram's account switcher. The real route was Profile, then a long-press on the top-right Options control. Static class context narrowed the search; runtime accessibility evidence settled it.
- The oracle settings screen showed exactly five enabled switches. There was no suggested-post switch and no profile-ad switch, agreeing with the static resource analysis. Agreement between independent static and behavioral evidence is much stronger than either one alone.
- After explicit approval, the oracle was uninstalled and the reconstruction installed. It reached Instagram's first-run screen and stayed alive without a fatal startup trace: the first true runtime proof that the delta-built APK was more than assemblable bytes.
- The old UI test expected the word `Password`; the current first-run screen says `Join Instagram` and `I already have a profile`. Even a valid test can become a false alarm when it confuses one transient UI state with the underlying startup contract.
- The reconstructed welcome dialog appeared after login, and both its Settings button and the profile long-press route opened the same settings screen. A feed toggle survived a full process restart and was then restored.
- One apparent reconstruction failure was just lazy UI rendering: the profile's hamburger button was missing until a small human swipe made the action bar appear. The lesson for automation is not "sleep longer"; it is "stimulate the known state transition, then poll for the semantic control."
- Another apparent failure came from testing too soon after a preference change. A feed post remained visible until the process restarted; afterward feed and Explore were empty and Reels showed a handled error instead of crashing. The eventual test harness needs restart boundaries as part of the feature contract, not as flaky-test folklore.
- Story blocking is also more nuanced than "the tray disappears": the user's own story remains. Behavioral assertions must describe what is intentionally removed rather than demand a visually empty screen.
- On/off comparisons made the feature evidence much stronger. Explore changed from an empty search shell to a populated grid; Reels changed from a handled error to playing video; feed posts disappeared after the required restart. The settings were restored afterward.
- Enabled Reels exposed another test-harness trap: UI Automator refused to dump because a playing video never became idle. Continuous media needs screenshot/process assertions or a non-idle-aware driver, not a test that waits forever for calm.
- The behavioral conclusions were also written into a small JSON contract. Agents can still reason about ambiguous mappings, but they should not have to reread a blog-style journal to learn that settings require a restart or that the Options button is lazily rendered.
- Stories produced the expected restart-bounded contrast without opening anything: enabling showed three other users' unseen entries; disabling left only the current user's own story.
- Shopping exposed the kind of bug a UI checkbox cannot reveal. Its helper checks for `minshop`, while all three patched identifiers contain `minishops`; the direct substitutions therefore preserve every string regardless of the toggle.
- Hardcore Mode really is hardcore: once enabled, the same listener blocks turning Hardcore itself back off. That test belongs on disposable app data, not a logged-in reference installation.
