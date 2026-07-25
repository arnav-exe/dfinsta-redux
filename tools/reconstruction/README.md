# Reconstruction Tools

Deterministic tools for recovering a maintainable DFInsta patch from stock and modified APK decodes.

Run from the repository root:

```bash
python3 tools/reconstruction/inventory.py \
  work/1.4.1-reconstruction/stock-340 \
  work/1.4.1-reconstruction/dfinsta-1.4.1 \
  --output work/1.4.1-reconstruction/inventory.json
```

`inventory.py` matches smali classes by their in-file descriptors and compares normalized smali with debug line directives, comments, and blank lines removed. Other files are compared by decode-relative path and raw content hash.

To isolate direct hook hosts from changed classes:

```bash
python3 tools/reconstruction/analyze_class_changes.py \
  work/1.4.1-reconstruction/inventory.json \
  --output work/1.4.1-reconstruction/class-delta-summary.json
```

To materialize reviewable normalized diffs for direct hook hosts:

```bash
python3 tools/reconstruction/extract_class_diffs.py \
  work/1.4.1-reconstruction/inventory.json \
  work/1.4.1-reconstruction/class-delta-summary.json \
  --direct-only \
  --output-dir work/1.4.1-reconstruction/direct-hook-diffs
```

To compare entries in existing `res/values*` XML files semantically:

```bash
python3 tools/reconstruction/analyze_value_resources.py \
  work/1.4.1-reconstruction/stock-340 \
  work/1.4.1-reconstruction/dfinsta-1.4.1 \
  --output work/1.4.1-reconstruction/value-resource-delta.json
```

After reviewing the inventories, bootstrap classified patch source with:

```bash
python3 tools/reconstruction/bootstrap_source.py \
  work/1.4.1-reconstruction/inventory.json \
  work/1.4.1-reconstruction/value-resource-delta.json \
  work/1.4.1-reconstruction/direct-hook-diffs \
  --output dfinsta_source_1.4.1
```

The bootstrap refuses to overwrite an existing output directory and fails on any added class outside the explicitly classified DFInsta and ACRA namespaces.

Prepare an isolated build tree:

```bash
python3 tools/reconstruction/prepare_build_tree.py \
  work/1.4.1-reconstruction/stock-340 \
  dfinsta_source_1.4.1 \
  --output work/1.4.1-reconstruction/rebuilt-source
```

Verify the rebuilt APK's DEX-level contract with:

```bash
python3 tools/reconstruction/verify_apk.py <apk> --output <result.json>
```

The current contract requires the recovered 1.4.1 hook symbols, exactly 11 DEX files, and absence of the dropped 1.3 response-rewrite/Proxygen hook names.

Apply the first maintainable hook family to a clean target tree with:

```bash
python3 tools/reconstruction/apply_endpoint_patches.py \
  <target-decode> \
  dfinsta_source_1.4.1/patches/endpoint_replacements.json \
  --output <report.json>
```

Endpoint operations resolve hosts by `.class` descriptor, enforce exact anchor counts, and are idempotent. They fail on missing, duplicate, or partially applied hooks.

Apply the four remaining host families with:

```bash
python3 tools/reconstruction/apply_anchored_patches.py \
  <target-decode> \
  dfinsta_source_1.4.1/patches/anchored_patches.json \
  --output <report.json>
```

Anchors match significant smali instructions while ignoring debug `.line` directives and blank lines. Operations carry explicit completion markers and reject partial state.

Run focused tool tests with:

```bash
python3 -m unittest discover -s tools/reconstruction/tests -v
```

Run the proven prepare/apply/build/verify sequence end to end with:

```bash
python3 tools/reconstruction/rebuild.py \
  work/1.4.1-reconstruction/stock-340 \
  dfinsta_source_1.4.1 \
  apktool_2.9.3.jar \
  --work-tree work/1.4.1-reconstruction/rebuild-run \
  --output-apk work/1.4.1-reconstruction/rebuild-run-unsigned.apk
```

The command refuses to overwrite either output. It intentionally stops at a verified unsigned APK; alignment, test signing, device installation, and release signing are separate gates.
