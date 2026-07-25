# Instagram 430 First-Pass Mapping

Status: research mapping only; no 430 patch has been applied or built.

## Target

- APK: `apks/com.instagram.android_430.0.0.53.80-383611248_minAPI28(arm64-v8a)(360,400,420,480dpi).apk`
- SHA-256: `38ae9861b9ca89f60f41767324e1c3d54a4e3a00ed5555b92660a08e6db14754`
- Decode: apktool 2.9.3, 28.36 seconds, 1.6 GiB output
- DEX trees: 19
- Unique class descriptors: 179,190
- Generated tree: `work/430-port/stock-430`

Apktool emitted resource string-chunk warnings but baksmaled all DEX files successfully. Resource reconstruction requires a separate gate before declaring the target buildable.

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

## Recommended Direct Sites

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

- 340 `LX/66Y` maps to `LX/06X7;` at `smali_classes6/X/06X7.smali`.
- Existing long-click listener maps to `LX/0LAk;`, case 1, at `smali_classes9/X/0LAk.smali`.
- Two action-item branches require classification before patching.

### Lazy profile menu

Named `UserDetailFragment` and `RefreshableAppBarLayoutBehavior` survive under `smali_classes6/com/instagram/profile/`.

The visibility path changed from 340 `A1b`/`LX/2QV.A0W()` to fields `A1F`/`A1G` and `LX/00ds.A1T(Z)`. Do not copy the proposed 340 fix forward; trace 430 initialization ordering independently.

### Feed lifecycle/cache

- Main fragment candidate: `LX/04nC;` at `smali_classes2/X/04nC.smali`
- Cache abstraction candidate: `LX/04EV;` at `smali_classes5/X/04EV.smali`
- Concrete named type: `com.instagram.mainfeed.network.MainFeedCacheDataSource`

`FeedCacheCoordinator`, `FlashFeedCache`, and `FeedItemDatabase` are gone. The 340 field-level cache cleaner cannot be ported. Map public behavior/API or redesign cache invalidation rather than guessing new fields.

## Next Deterministic Work

1. Create a versioned 430 candidate manifest containing only high-confidence direct sites.
2. Add an analyzer that verifies method-scoped anchor and occurrence counts before writing.
3. Trace selected-result patch points for `LX/0aOK.EOC/EOE`.
4. Trace Tigon request URI def-use and implement the named-host hook.
5. Map `LX/06X7` action-item branches and stock listener semantics.
6. Trace 430 cache APIs separately; do not block the first build on brittle cache internals.
7. Validate resource decode/build before adding custom resources.
8. Build a minimal context + request-blocking + settings-entry prototype before full feature coverage.

Generated detailed evidence remains under ignored `work/430-port/`.
