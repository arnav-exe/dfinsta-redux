# Instagram 430 Minimal Port

This tooling creates a fresh work tree from the clean Instagram 430 apktool decode, overlays the six custom classes into `smali_classes20`, adds the two resource files and one non-exported activity, applies the three 430 anchored patches with the existing generic patcher, installs API 36 framework resources into an isolated framework directory, builds with apktool 2.9.3/aapt1, and verifies the 20-DEX contract.

Run from the repository root with paths that do not already exist:

```bash
python3 tools/port_430/build.py \
  work/430-port/stock-430 \
  dfinsta_source_430 \
  apktool_2.9.3.jar \
  work/430-port/framework-res-api36.apk \
  --framework-path work/430-build/framework-api36 \
  --work-tree work/430-build/tree \
  --output-apk work/430-build/dfinsta_430-unsigned.apk
```

Every generated destination is refuse-overwrite. Remove or choose new generated paths before another run; do not reuse a partially built tree.

## Limitations

- The prototype only captures application context, blocks five request families in Tigon, and opens settings from the existing profile long-click entry.
- Toggle changes persist through framework preferences and take effect after an explicit process restart.
- There are no direct endpoint substitutions, profile-ad blocking, feed-cache clearing, lazy profile-menu fix, welcome flow, telemetry/crash integration, or device validation.
- Shopping remains path-based and may not cover identifiers transported outside the request URI path.
- The decoded 430 resource table emitted apktool warnings. A successful build still requires runtime validation on a test device.
