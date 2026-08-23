# Reconstruction Tools

Deterministic tools for recovering a maintainable DFInsta patch from stock and modified APK decodes.

**Two halves, and the split matters.** `inventory.py`, `apply_endpoint_patches.py` and
`apply_anchored_patches.py` are **live library code** — `driver.py` reaches them through
`tools/resolver/validate_candidates.py` on every port, so changing them changes what ships.
`rebuild.py`, `prepare_build_tree.py` and `verify_apk.py` are the documented 1.4.1 rebuild
entry point. Everything under [`archive/`](archive/) is a **completed one-time job** and is
described at the bottom of this file.

Run from the repository root:

```bash
python3 tools/reconstruction/inventory.py \
  work/1.4.1-reconstruction/stock-340 \
  work/1.4.1-reconstruction/dfinsta-1.4.1 \
  --output work/1.4.1-reconstruction/inventory.json
```

`inventory.py` matches smali classes by their in-file descriptors and compares normalized smali with debug line directives, comments, and blank lines removed. Other files are compared by decode-relative path and raw content hash.

Prepare an isolated build tree:

```bash
python3 tools/reconstruction/prepare_build_tree.py \
  work/1.4.1-reconstruction/stock-340 \
  tests/fixtures/dfinsta_source_340 \
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
  tests/fixtures/dfinsta_source_340/patches/endpoint_replacements.json \
  --output <report.json>
```

Endpoint operations resolve hosts by `.class` descriptor, enforce exact anchor counts, and are idempotent. They fail on missing, duplicate, or partially applied hooks.

Apply the four remaining host families with:

```bash
python3 tools/reconstruction/apply_anchored_patches.py \
  <target-decode> \
  tests/fixtures/dfinsta_source_340/patches/anchored_patches.json \
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
  tests/fixtures/dfinsta_source_340 \
  apktool_2.9.3.jar \
  --work-tree work/1.4.1-reconstruction/rebuild-run \
  --output-apk work/1.4.1-reconstruction/rebuild-run-unsigned.apk
```

The command refuses to overwrite either output. It intentionally stops at a verified unsigned APK; alignment, test signing, device installation, and release signing are separate gates.

## `archive/` — the reconstruction that already happened

These four scripts ran once, in this order, and their output is
`tests/fixtures/dfinsta_source_340` — the tree the Phase-B fixtures are built from. They are kept
so that tree has a stated provenance and a reproducer, **not** because anything calls them.
Nothing in `src/` or `tools/` imports them, and the test suite does not notice their absence.

Re-running the sequence needs `work/1.4.1-reconstruction/`, which is gitignored and does not
survive a clone, so on a fresh checkout this is a record rather than a runnable path.

```bash
# 2. isolate direct hook hosts from changed classes
python3 tools/reconstruction/archive/analyze_class_changes.py \
  work/1.4.1-reconstruction/inventory.json \
  --output work/1.4.1-reconstruction/class-delta-summary.json

# 3. materialize reviewable normalized diffs for those hosts
python3 tools/reconstruction/archive/extract_class_diffs.py \
  work/1.4.1-reconstruction/inventory.json \
  work/1.4.1-reconstruction/class-delta-summary.json \
  --direct-only \
  --output-dir work/1.4.1-reconstruction/direct-hook-diffs

# 4. compare entries in existing res/values* XML semantically
python3 tools/reconstruction/archive/analyze_value_resources.py \
  work/1.4.1-reconstruction/stock-340 \
  work/1.4.1-reconstruction/dfinsta-1.4.1 \
  --output work/1.4.1-reconstruction/value-resource-delta.json

# 5. bootstrap the classified patch source
python3 tools/reconstruction/archive/bootstrap_source.py \
  work/1.4.1-reconstruction/inventory.json \
  work/1.4.1-reconstruction/value-resource-delta.json \
  work/1.4.1-reconstruction/direct-hook-diffs \
  --output tests/fixtures/dfinsta_source_340
```

Step 1 is `inventory.py` at the top of this file, which stayed out of `archive/` because it is
also live library code. The bootstrap refuses to overwrite an existing output directory and fails
on any added class outside the explicitly classified DFInsta and ACRA namespaces. Its output
includes `oracleDeltas/` — 23 normalized host diffs — and `reconstruction.json`, which is why
those exist and why nothing reads them.
