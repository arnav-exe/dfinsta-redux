# DFInsta 1.4.1 Reconstructed Source

This tree is being reconstructed from clean apktool 2.9.3 decodes of stock Instagram `340.0.0.22.109` and the DFInsta `1.4.1` APK oracle.

## Current State

- `newCode/`: 13 added DFInsta classes.
- `thirdPartyCode/`: 79 added ACRA classes, kept separate from project-owned code.
- `newRes/`: 91 resource files absent from stock 340.
- `appendRes/`: entries added to eight existing `res/values*` files.
- `resourcePatches/`: two existing value resources changed by the oracle.
- `manifest/`: application components added by the oracle.
- `oracleDeltas/host/`: normalized evidence for 23 directly patched Instagram host classes. These are reference deltas, not yet an automated patch format.
- `behavior_contract.json`: machine-readable confirmed device selectors, restart rules, feature observations, and unresolved tests.

`patches/endpoint_replacements.json` and `patches/anchored_patches.json` are deterministic, idempotent operations against a fresh stock 340 decode. A complete delta-driven apktool/aapt1 build has assembled successfully, passed the DEX contract, installed on a Pixel 9, and passed startup/settings plus feed/Explore/Reels on/off tests.

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
