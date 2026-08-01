# Future Work

## End Goal

Build a reproducible, privacy-respecting pipeline with Temporal as the durable outer orchestrator and Google ADK as the multi-agent reasoning layer. It accepts a clean stock Instagram APK, resolves and applies small version-native DFInsta patches, builds and signs after human authorization, and produces structured device evidence. Instagram 340 / DFInsta 1.4.1 is the golden reference; Instagram 430 is the first fully traced replay fixture and future baseline candidate.

Completion requires:

- A clean, delta-driven golden source with no copied Instagram host classes.
- Machine-readable static and behavioral contracts for every retained feature.
- Explicit privacy and dead-code policy rather than inherited oracle behavior.
- Device automation that handles restarts, lazy UI, and continuous media.
- A 430 port that passes the retained 1.4.1 behavior contract.
- Agentic orchestration only around ambiguous mapping and diagnosis; mechanical steps remain deterministic.

## Immediate Roadmap

> Superseded by [`docs/ROADMAP.md`](ROADMAP.md), which is now the single roadmap.
> The list below is retained as history and is not maintained.


1. Treat the synthetic Temporal durability/contracts/ledger/executor slice as implemented in `3e91eb5` and `ac4da5b`; keep APK, signing, device, and ADK actions out of Phase A.
2. Finish Phase A authority/deployment evidence: authenticated Update submission from a separate client process, hard process-loss recovery, and a saved replay corpus. Current-version rollout and persistent Temporal-server restart with fresh SDK connections are already proven.
3. Extract 340 and 430 into version-independent intent plus version-scoped resolutions, then prove one target-neutral apply/build/verifier engine and mutation fixtures.
4. Add Google ADK later as bounded read-only Temporal Activities, then replay the proven engine and 430 through the durable workflow.
5. Repeat core contrasts with installed-APK identity, measured preferences, required state-specific assertions, successful restarts, and a declared cache protocol before promoting 430 as the accepted behavioral baseline.
6. Add one bounded mapping/assessment agent only after deterministic candidate generation and generalization are benchmarked.
7. Attempt profile-ad endpoint/UI contrast only when eligible inventory is available; treat absent inventory as inconclusive and do not let it block pipeline development.

Phase A durability evidence is not execution authority. The current executor admits exact tool digests, argv templates, environment values, workspace paths, artifact kinds, and mutation paths, but those checks are not an OS sandbox. No APK tool has been admitted and no signing/device/ADK worker exists yet.

## Lazy Profile Options

This section documents the 340 behavior only. On Instagram 430, Options appears immediately; the live action is a direct `ImageView` rendered by `LX/077K` from self-profile model `LX/077N`, so no lazy-action-bar fix is required there.

### Observed behavior

The current-user profile's top-right `Options`/hamburger action can be absent after navigation. Home > Profile plus a swipe back toward the profile header causes it to render. DFInsta settings are attached to this control's long-click action.

### Confirmed static cause chain

DFInsta does not create the lazy behavior. Its `LX/66Y` delta only replaces Instagram's existing `LX/FAE` long-click listener with `SettingsWrapper`; it does not change view creation or visibility.

Instagram's `UserDetailFragment.configureActionBar()` applies boolean field `A1b` through `LX/2QV.A0W(Z)`. `A1b` initially defaults false, and `A0W(false)` sets the action-bar root to `GONE`. The only discovered writer of `A1b` is `RefreshableAppBarLayoutBehavior.DHe(AppBarLayout, int)`, which runs on app-bar offset/scroll events and reapplies visibility. Listener registration occurs late enough that the initial offset state may not be replayed after the fragment joins the callback list.

Key evidence:

- Listener-only DFInsta delta: `dfinsta_source_1.4.1/oracleDeltas/host/X__66Y.diff`
- Reconstructed listener patch: `dfinsta_source_1.4.1/patches/anchored_patches.json`
- Parent visibility application: stock `UserDetailFragment.configureActionBar()` around the `A1b` read and `LX/2QV.A0W(Z)` call.
- Offset-driven writer: stock `RefreshableAppBarLayoutBehavior.DHe()` writes `A1b`, calls `A0W()`, and may rebuild through `A0R()`.

The precise reason the initial offset callback is missed remains a runtime-tracing question, but the static visibility chain is established.

### Preferred future fix

After `UserDetailFragment` is registered in `RefreshableAppBarLayoutBehavior.A0H`, deliver the behavior's actual current app-bar offset through the existing state-update path. This preserves Instagram's collapse/fade logic.

Do not simply force `A1b=true` on every `LX/66Y.configureActionBar()` call: that is more targeted but can override intended scroll transitions and is less portable.

### Acceptance criteria

- Home > Profile exposes one visible, long-clickable `Options` node within one second without a swipe.
- Long-click opens `Distraction Free settings`; normal click retains Instagram's stock menu.
- Other-user profiles do not gain the self-profile menu.
- Scroll collapse/fade behavior still works.
- Repeated rebuilds do not duplicate action items.
- Cold start, warm tab switch, process recreation, and restored scroll position pass.

### Required investigation before patching

- Instrument or log first `configureActionBar`, hamburger `A9l`, `A0W`, and `DHe` calls.
- Record `A1b`, relevant collapsed-state fields, and current app-bar offset.
- Reproduce on clean stock 340 to confirm that DFInsta is not changing runtime timing indirectly.
- Identify a safe current-offset accessor before adding an initial `DHe()` dispatch.

## Deferred Product Decisions

- Broad resource pruning remains deferred until full runtime settings traversal.
- Shopping is retired on 430 because no standalone tab remains and distributed commerce cannot be safely represented by the old global label.
- Profile-ad blocking is visible and exact in v7; eligible-account runtime contrast remains.
- Treat cache state as a first-class test boundary. Current v7 Nothing contrasts record cache as unknown; separate v6 diagnostic Pixel evidence required cache exhaustion before disabled Reels became visible.
- Decide whether Hardcore Mode remains effectively irreversible through the UI.
