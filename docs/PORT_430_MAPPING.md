# Instagram 430 Port State and Mapping

Status: minimal resource-free DEX graft built, statically verified, signed, installed, and settings-validated. Feature contrasts remain.

Current branch: `port-430` at `cb63ded`.

Relevant commits:

- `93ddffd add minimal 430 port`: initial custom-Activity/resource prototype.
- `a128481 preserve stock 430 resources`: replaced the lossy resource rebuild output with an exact-stock-resource DEX graft and framework `AlertDialog` settings.
- `94523c8 read settings title from contract`: made the device runner read the selected behavior contract's settings title.
- `a75e60d launch configured app activity`: added contract-defined MAIN/LAUNCHER startup and foreground-activity validation.
- `cb63ded patch live 430 profile action`: attached settings to the live self-profile `ProfileActionBar` model.

## Target

- APK: `apks/com.instagram.android_430.0.0.53.80-383611248_minAPI28(arm64-v8a)(360,400,420,480dpi).apk`
- SHA-256: `38ae9861b9ca89f60f41767324e1c3d54a4e3a00ed5555b92660a08e6db14754`
- Decode: apktool 2.9.3, 28.36 seconds, 1.6 GiB output
- DEX trees: 19
- Unique class descriptors: 179,190
- Generated tree: `work/430-port/stock-430`

Apktool emitted resource string-chunk warnings but baksmaled all DEX files successfully. Resource reconstruction was subsequently proven lossy and is no longer part of the final APK architecture.

## Exact Current Build

- Artifact: `work/430-graft-v3/dfinsta_430-graft-v3-test.apk`
- SHA-256: `95fab99680031aee8ffde3a5f9a202f47e6c4067626c94e9dffb948a9edb048e`
- Installed successfully as an in-place update.
- Package/version preflight: passed for `430.0.0.53.80` / `383611248`.
- Static verification: passed.

The verifier proves:

- Exactly 20 DEX files, `classes.dex` through `classes20.dex`.
- Exactly four custom descriptors: `startapp`, `dfinstagram`, `hooks`, and `SettingsWrapper`.
- Exactly the three expected host-hook markers in the expected DEX files.
- No forbidden legacy, Activity, resource-dependent, ACRA, or Amplitude symbols in custom DEX.
- Byte-identical stock binary `AndroidManifest.xml` and `resources.arsc`.
- Identical stock `res/` entry names and byte-identical contents for every entry.

## Resource Failure and Architecture Decision

The initial stock apktool/aapt1 build failed because Instagram 430 references an Android API 36 framework attribute absent from the local framework. Pulling the isolated API 36 `framework-res.apk` from the test device and installing it into a dedicated apktool framework path made the static aapt1 build succeed.

This did not make full resource rebuilding safe. The decoded/rebuilt Instagram resource table is lossy:

- Original string IDs extend through `0x7f130231`.
- The apktool-rebuilt table extends only through `0x7f130220`.
- Existing stock DEX still references IDs in the dropped range.
- The first installed full-resource-rebuild test, `work/430-build/dfinsta_430-test.apk`, SHA-256 `eb55f232cb6e59f4749f208b2a1123393090c42a61e0b10e9ae60fc7b80e6f5c`, crashed in AndroidX Startup because `0x7f130227` was missing.

The supported prototype architecture is therefore resource-free. Apktool/aapt1 assembles only an intermediate from the patched work tree; the final builder ignores that intermediate's manifest and resources and grafts only these entries into the exact stock APK:

- `classes.dex`: patched `TigonServiceLayer`.
- `classes3.dex`: patched `InstagramAppShell`.
- `classes6.dex`: patched live profile action renderer `LX/077K`.
- `classes20.dex`: four custom DFInsta classes.

Stock signature metadata is removed for re-signing. All other entries are copied from stock, preserving the original compiled resource graph exactly.

## Implemented Minimal Surface

- `startapp` captures the `Application` context after `Application.onCreate()`.
- `dfinstagram` reads five checked-by-default flags from the existing `com.instagram` shared-preference file.
- `hooks.throwIfBlocked(URI)` enforces Feed, Explore, Reels, Stories, and partial Shopping rules in Tigon's existing `IOException` failure path.
- `SettingsWrapper` is attached to the current-profile Options `ImageView` guarded by self-profile action model `LX/077N`, and opens a framework multi-choice `AlertDialog` titled `Distraction-free settings - restart required`.
- There is no custom manifest entry, custom Activity, XML preference screen, custom resource, or fixed `0x7f...` application resource reference.
- Changes require a process restart for clean behavior evaluation.

This design deliberately excludes direct endpoint replacements, profile-ad blocking, feed-cache clearing, welcome UI, telemetry, and crash-reporting code. Shopping remains partial because non-URI Bloks identifiers are outside the Tigon path rule. A lazy profile-menu repair is unnecessary on 430 because the action renders immediately.

## Device State

- MAIN/LAUNCHER alias `com.instagram.android.activity.MainTabActivity` reaches foreground `com.instagram.mainactivity.InstagramMainActivity`; deprecated `LauncherActivity` was the earlier self-finishing trampoline.
- Startup passes foreground, process-liveness, and fatal-log checks.
- Profile Options is present immediately. It is long-clickable after the `LX/077K` patch, and the settings dialog opens on the first attempt without header swipes.
- Exactly five choices render in order: Feed, Explore, Reels, Stories, Shopping. All inherited values are checked.
- Normal Options click still enters Instagram's stock surface.
- The in-place update preserved login and preferences created under the current 340 installation.
- Do not clear data or uninstall without explicit user approval.

## Mapping Rules

- None of the old obfuscated 340 hook descriptors survive in 430.
- Map by stable endpoint, named type, structural role, method shape, and data flow.
- Do not replace every matching literal.
- Do not patch generated global string pools (`LX/0005`, `LX/0033`) or the critical-API whitelist (`LX/05jj`) directly.
- Empty strings inside the critical-API whitelist are especially dangerous because every path contains `""`.
- Patch direct request-path assignments or caller-level selected results.

## Literal Expansion

| Literal | 340 | 430 |
|---|---:|---:|
| `feed/timeline/` | 8 | 12 |
| `discover/topical_explore/` | 3 | 6 |
| `clips/discover/` | 3 | 7 |
| `clips/discover/stream/` | 9 | 3 |
| `feed/reels_tray/` | 5 | 6 |
| `feed/reels_tray/_v1` | 3 | 3 |
| `com.bloks.www.minishops.ad.storefront` | 4 | 3 |
| `com.bloks.www.minishops.storefront.ig` | 2 | 2 |
| `profile_ads/get_profile_ads/` | 2 | 2 |

The 340 literals `mixed_media/discover/` and `mixed_media/discover/stream/` are absent. Mixed-media behavior is partly represented by request modes, `enable_mixed_media_chaining`, and `clips/discover/interest/stream/`.

## Deferred Direct-Site Research

The following mapping remains useful for later feature expansion, but none of these direct endpoint substitutions is part of the current minimal graft. Do not apply them until stock-vs-graft launcher behavior and the minimal Tigon/settings contract are validated.

### Feed

Patch direct request paths:

- `LX/02qk;->A01(...)` in `smali/X/02qk.smali`
- `LX/02ps;->A02(...)` in `smali/X/02ps.smali`
- `LX/0CAZ;->run()` in `smali_classes2/X/0CAZ.smali` (feed priming)
- `LX/0WOC;->run()` in `smali_classes16/X/0WOC.smali`

Skip timeline literals used only for metadata/comparison/whitelisting in `LX/0N5P`, `LX/05bX`, `LX/04tT`, `LX/03tw`, `LX/02Ji`, and `LX/05jj`.

### Explore

Patch all five non-table request paths:

- `LX/06SN;->A05(...)` in `smali_classes5/X/06SN.smali`
- `LX/0JeJ;->run()` in `smali_classes5/X/0JeJ.smali`
- `LX/0CSN;->A00(...)` and `A01(...)` in `smali_classes5/X/0CSN.smali`
- `LX/01SR;->run()` in `smali_classes5/X/01SR.smali`

Skip the `LX/05jj` whitelist entry.

### Reels

Patch direct paths:

- `LX/05t2;->A07(...)` and `A09(...)` in `smali_classes4/X/05t2.smali`
- `LX/0aOK;->C18(...)` and `DGa(...)` in `smali_classes15/X/0aOK.smali`
- `LX/0ZSA;->A03(...)` and `A04(...)` in `smali_classes15/X/0ZSA.smali`

For stream mode, patch the selected result in `LX/0aOK;->EOC(...)` and `EOE(...)` after global string lookup. These methods select between:

- `clips/discover/stream/`
- `clips/discover/interest/stream/`

Do not edit `LX/0033`/`LX/0005` string pools. Skip `LX/04Pn` analytics literals and `LX/05jj` whitelist entries.

Manual broader-policy candidates:

- `LX/09le;->A01(...)` interest-stream prefetch
- `clips/homecoming/` branch in `LX/05t2.A09`

### Stories

Patch five direct base-endpoint assignments:

- `LX/03rm;->A05(...)` in `smali/X/03rm.smali`
- `LX/04vN;->A0G(...)` in `smali_classes2/X/04vN.smali`
- `LX/0TOe;->run()` in `smali_classes15/X/0TOe.smali`
- Both base-endpoint branches of `LX/0HZD;->A02(...)` in `smali_classes15/X/0HZD.smali`

The three `_v1` literals are cache-key selectors, not network endpoints. Preserve them unless strict oracle fidelity is explicitly chosen; emptying them risks collisions. Skip the `LX/05jj` whitelist entry.

### Profile Ads

Clean one-to-one request mappings:

- `LX/0kza;->A00(I)` in `smali_classes16/X/0kza.smali`
- `LX/0kzj;->A00(List,I)` in `smali_classes16/X/0kzj.smali`

These remain behaviorally significant because the Tigon blocker has no profile-ad rule.

## Shopping

Do not port the broken 340 helper unchanged. It checks `minshop`, which does not match `minishops` identifiers.

Relevant 430 control-flow sites:

- `LX/006B;->A09(...)` two storefront UI/deep-link routing branches
- `LX/0fWm;->A00(Bundle)` direct storefront construction
- `LX/0mbs;->invokeSuspend(Object)` coroutine storefront dispatch

Generated `LX/0005` string entries must remain untouched. Decide the intended Shopping policy first, fix matching semantics, then gate these callers or wrap only proven direct construction sites.

## Named and Anchored Hosts

### Tigon

- Descriptor survives: `Lcom/instagram/api/tigon/TigonServiceLayer;`
- Path: `smali/com/instagram/api/tigon/TigonServiceLayer.smali`
- `startRequest()` survives.
- Request type is now `LX/05ez;`.
- URI field is now `A08` (340 used `LX/1Os.A09`).
- Existing `IOException` failure path remains the preferred insertion context.

### Application startup

- Descriptor survives: `Lcom/instagram/app/InstagramAppShell;`
- Path moved to `smali_classes3/com/instagram/app/InstagramAppShell.smali`.
- `Application.onCreate()` call remains a stable anchor.
- Hardened policy should inject context only; do not port Amplitude/ACRA by default.

### Profile settings

- `LX/06X7` contains legacy action builders but does not create the runtime-visible 430 Options action.
- `ProfileActionBar` calls `LX/077K.A00()` to create direct action `ImageView` instances.
- Self-profile Options is represented by `LX/077N`; guarding on that model excludes other action types and preserves the stock click listener.
- The patch installs `SettingsWrapper` through `View.setOnLongClickListener()` immediately after stock click setup.

### Lazy profile menu

The current-user 430 Options action appears immediately after Profile navigation. The 340 lazy-menu issue does not reproduce and no visibility patch should be ported.

### Feed lifecycle/cache

- Main fragment candidate: `LX/04nC;` at `smali_classes2/X/04nC.smali`
- Cache abstraction candidate: `LX/04EV;` at `smali_classes5/X/04EV.smali`
- Concrete named type: `com.instagram.mainfeed.network.MainFeedCacheDataSource`

`FeedCacheCoordinator`, `FlashFeedCache`, and `FeedItemDatabase` are gone. The 340 field-level cache cleaner cannot be ported. Map public behavior/API or redesign cache invalidation rather than guessing new fields.

## Next Deterministic Work

1. Run restart-bounded Feed, Explore, Reels, Stories, and Shopping enabled/disabled validation and restore all five checked values afterward.
2. Verify preference mutation/persistence and confirm an unrelated-user profile does not receive the long-click action.
3. Only then consider direct-site coverage, profile ads, Shopping caller coverage, and cache invalidation. Keep those separate from the minimal proven path.
4. Add the proven mechanical flow to the durable agentic orchestration layer.
5. Do not reintroduce custom resources or manifest components unless a non-lossy resource packaging method is independently proven.

Generated detailed evidence remains under ignored `work/430-port/`.
