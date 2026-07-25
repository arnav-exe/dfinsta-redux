# DFInsta 1.4.1 Reconstructed Source

This tree is the privacy-hardened maintained baseline reconstructed from clean apktool 2.9.3 decodes of stock Instagram `340.0.0.22.109` and the DFInsta `1.4.1` APK oracle.

## Current State

- `newCode/`: nine maintained DFInsta classes.
- `thirdPartyCode/`: empty; the oracle's 79 ACRA classes were removed by policy.
- `newRes/`: 91 resource files absent from stock 340.
- `appendRes/`: entries added to eight existing `res/values*` files.
- `resourcePatches/`: two existing value resources changed by the oracle.
- `manifest/`: the retained settings activity addition.
- `oracleDeltas/host/`: normalized historical evidence for 23 directly patched Instagram host classes.
- `behavior_contract.json`: machine-readable confirmed device selectors, restart rules, feature observations, and unresolved tests.

`patches/endpoint_replacements.json` and `patches/anchored_patches.json` contain 30 endpoint and seven deterministic, idempotent anchored operations against a fresh stock 340 decode. A complete delta-driven apktool/aapt1 build has assembled successfully, passed the hardened DEX contract, installed on a Pixel 9, and passed startup/settings checks. The original feature implementation has confirmed feed/Explore/Reels/Stories on/off contrasts.

The maintained baseline deliberately removes unconditional Amplitude telemetry, inherited ACRA crash reporting, and proven-safe dead residue. `tools/reconstruction/verify_apk.py --hardened` enforces that policy while preserving default oracle verification for historical analysis.

See `../docs/RECONSTRUCTION_1.4.1.md` and `../docs/DFINSTA_1.4.1_DELTA.md` for methodology and findings.

## Rebuild

From the repository root, with a clean stock 340 decode:

```bash
python3 tools/reconstruction/rebuild.py \
  work/1.4.1-reconstruction/stock-340 \
  dfinsta_source_1.4.1 \
  apktool_2.9.3.jar \
  --work-tree work/1.4.1-reconstruction/rebuild-run \
  --output-apk work/1.4.1-reconstruction/rebuild-run-unsigned.apk
```

The rebuild intentionally outputs an unsigned APK. For a local device test, zip-align it and sign with a test key. A test key cannot update an app signed by the original DFInsta key; uninstall the existing package first if signatures differ.
