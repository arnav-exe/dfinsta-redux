# APK assembly and stock graft

**This is the builder every port runs.** `driver.py` pins `build.py` as its `BUILDER` and
shells out to it with per-version arguments; it built 439, 440, 441, 442 and 443. The directory
was called `port_430` until 2026-08-23, and the worked example below is still a 430 one because
that is the run it carries evidence for — but nothing here is 430-only except
`verify_apk_430.py`, which is why that file now says so in its name.

The driver passes `--verifier generic`, routing verification to
`tools/verify/verify_build.py`, which takes every version-specific fact from the caller.
`--verifier port430` still defaults on for a bare invocation and pins descriptors that moved in
439, so pass `--verifier` explicitly on any target that is not 430.

This tooling decodes the supplied stock APK into a fresh refuse-overwrite tree, overlays the custom classes into a free `smali_classesN`, applies the resolved anchored host operations, and uses apktool 2.9.3/aapt1 only to assemble the changed DEX files. It then grafts the patched DEX entries into the exact supplied stock APK, removing signing artifacts while preserving every other stock ZIP entry.

The architecture is resource-free because apktool loses data while decoding/rebuilding Instagram 430's resource table. The grafted APK therefore retains the stock `resources.arsc`, binary `AndroidManifest.xml`, and every `res/` entry byte-for-byte. Settings are a framework `AlertDialog` opened by long-pressing the existing profile Options view; no Activity, manifest component, custom resource, or fixed application resource ID is used.

Run from the repository root with paths that do not already exist:

```bash
python3 tools/build/build.py \
  work/430-build/stock-430-clean \
  'apks/com.instagram.android_430.0.0.53.80-383611248_minAPI28(arm64-v8a)(360,400,420,480dpi).apk' \
  tests/fixtures/dfinsta_source_430 \
  apktool_2.9.3.jar \
  work/430-port/framework-res-api36.apk \
  --framework-path work/430-build/framework-api36 \
  --work-tree work/430-build/tree \
  --output-apk work/430-build/dfinsta_430-unsigned.apk
```

The first positional path is a new stock-decode destination, not an existing decode. Every generated destination is refuse-overwrite, including that decode, the adjacent `*-intermediate.apk`, verification JSON, and build-provenance JSON. Choose new generated paths before another run; do not reuse a partially built tree.

## Limitations

- The test port captures application context, blocks Feed, Explore, Reels, Stories, and exact profile-ad requests in Tigon, replaces three central Reels endpoint selections, and opens settings from the existing profile long-click entry.
- The dialog shows exactly five checked-by-default choices. Changes are applied to the `com.instagram` shared-preference file immediately and take effect after an explicit process restart.
- There is no feed-cache clearing, lazy profile-menu fix, welcome flow, telemetry, or crash integration. Shopping is intentionally retired because 430 has no standalone Shopping tab and distributed commerce is not represented honestly by the old setting.
- The verifier disassembles the candidate and checks exact host methods/invocations, all retained stock payload bytes, and optional fail-closed signature identity. The build still stops at an unsigned APK, so zipalign/sign/final signed verification remain separate commands.
- The decoded 430 resource table emitted warnings and apktool's rebuilt resource payload was diagnosed as lossy. The final graft deliberately discards all intermediate resources and manifest data; release readiness still requires integrated signing provenance, installed-APK identity checking, and controlled runtime validation.
