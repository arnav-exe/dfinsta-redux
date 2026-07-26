# Future Work

## End Goal

Build a reproducible, privacy-respecting DFInsta porting system that accepts a clean stock Instagram APK, applies small version-native patches, builds and signs it, and produces structured device evidence for retained behavior. Instagram 340 / DFInsta 1.4.1 is the golden reference; Instagram 430 is the first target.

Completion requires:

- A clean, delta-driven golden source with no copied Instagram host classes.
- Machine-readable static and behavioral contracts for every retained feature.
- Explicit privacy and dead-code policy rather than inherited oracle behavior.
- Device automation that handles restarts, lazy UI, and continuous media.
- A 430 port that passes the retained 1.4.1 behavior contract.
- Agentic orchestration only around ambiguous mapping and diagnosis; mechanical steps remain deterministic.

## Immediate Roadmap

1. Attempt profile-ad endpoint/UI contrast on eligible profile and contextual-feed surfaces; treat absent inventory as inconclusive.
2. Integrate zipalign/sign/final verification into the validated clean-stock refuse-overwrite report.
3. Repeat core contrasts with installed-APK identity, verified preferences, required state-specific assertions, successful restarts, and a declared cache protocol.
4. Wrap the proven deterministic build/sign/install/validation activities in the durable agentic workflow.
5. Establish an approved release-signing policy; the current v7 test artifact uses Android Debug.

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
