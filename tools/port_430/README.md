# Instagram 430 Minimal Port

This tooling creates a fresh work tree from the clean Instagram 430 apktool decode, overlays four custom classes into `smali_classes20`, applies the three anchored host patches, and uses apktool 2.9.3/aapt1 only to assemble the changed DEX files. It then grafts `classes.dex`, `classes3.dex`, `classes6.dex`, and `classes20.dex` into the exact supplied stock APK, removing signing artifacts while preserving every other stock ZIP entry.

The architecture is resource-free because apktool loses data while decoding/rebuilding Instagram 430's resource table. The grafted APK therefore retains the stock `resources.arsc`, binary `AndroidManifest.xml`, and every `res/` entry byte-for-byte. Settings are a framework `AlertDialog` opened by long-pressing the existing profile Options view; no Activity, manifest component, custom resource, or fixed application resource ID is used.

Run from the repository root with paths that do not already exist:

```bash
python3 tools/port_430/build.py \
  work/430-port/stock-430 \
  'apks/com.instagram.android_430.0.0.53.80-383611248_minAPI28(arm64-v8a)(360,400,420,480dpi).apk' \
  dfinsta_source_430 \
  apktool_2.9.3.jar \
  work/430-port/framework-res-api36.apk \
  --framework-path work/430-build/framework-api36 \
  --work-tree work/430-build/tree \
  --output-apk work/430-build/dfinsta_430-unsigned.apk
```

Every generated destination is refuse-overwrite, including the adjacent `*-intermediate.apk` and verification JSON. Choose new generated paths before another run; do not reuse a partially built tree.

## Limitations

- The prototype only captures application context, blocks five request families in Tigon, and opens settings from the existing profile long-click entry.
- The dialog shows exactly five checked-by-default choices. Changes are applied to the `com.instagram` shared-preference file immediately and take effect after an explicit process restart.
- There are no direct endpoint substitutions, profile-ad blocking, feed-cache clearing, lazy profile-menu fix, welcome flow, telemetry/crash integration, or device validation.
- Shopping remains path-based and may not cover identifiers transported outside the request URI path.
- The decoded 430 resource table emitted warnings and apktool's rebuilt resource payload was diagnosed as lossy. The final graft deliberately discards all intermediate resources and manifest data; a successful graft still requires signing and runtime validation on a test device.
