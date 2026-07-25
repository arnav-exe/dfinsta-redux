# DFInsta 1.4.1 Cleanup Audit

This audit separates proven dead/broken oracle residue from resources that are merely statically unreferenced. Cleanup should happen only after the oracle-faithful baseline remains reproducible.

## Safe Removal Boundary

### Nonexistent activity

Remove `com.dfinstagram.IconChoose` from `dfinsta_source_1.4.1/manifest/added_components.xml`.

No matching class exists, no source launches it, and it has no intent filter. The real settings activity continues to require `DfInstagramPreference`; removing `IconChoose` must not remove that style.

### Uninstantiated synthetic classes

No constructor or class reference exists outside their own definitions:

- `newCode/com/dfinstagram/preference/Preference$1.smali`
- `newCode/com/dfinstagram/dfinstagram$1.smali`

The active settings entry uses `SettingsWrapper` instead.

### Unreachable follower code

`PreferenceFragment.onSharedPreferenceChanged()` contains follower-tracker calls after an unconditional `return-void`. No branch enters that block, and neither referenced tracker class exists. Remove only the unreachable post-return instructions.

Source: `newCode/com/dfinstagram/preference/PreferenceFragment.smali`, method `onSharedPreferenceChanged`.

### Unreachable backup cases

`PreferenceFragment.onPreferenceClick()` contains `save_backup` and `restore_backup` cases calling absent `PrefsBackupHelper` methods. Neither key exists in the active preference XML, and the listener is installed only on donation entries. Remove those cases, not the active donation handler.

### Proven unused private members

- `Preference.getRealPathFromURI()`
- `hooks.str2Bytes()`
- `hooks.bufferStreamField`
- `hooks.readBufferField`
- `PreferenceFragment.updateList`

They are private and have no caller/read/write in maintained source.

### Dead comment helpers

Remove these `DistractionFree` methods if the hardened baseline does not add a Comments feature:

- `improveRemoveComments()`
- `improveRemoveLimitedComments()`
- `improveRemoveStreamComments()`

There is no `disable_comments` UI control, endpoint operation, host caller, or dynamic dispatcher.

### Suggested-post residue

The reconstructed 1.4.1 implementation has no suggested-post host hook or visible switch. Coordinated removable residue:

- `PreferenceFragment.isCachedFeature()` handling for `disable_suggested_posts`
- `dfinstagram_disable_suggested_posts` in `newRes/values/istrings.xml`
- Matching `public.xml` declaration

This conclusion applies to 1.4.1, not legacy 1.3 where response rewriting existed.

## Broken but Framework-Facing

`Preference.onActivityResult()` contains dormant file-picker branches using absent `com.hippo.unifile.UniFile`. No maintained source calls `startActivityForResult`, `showFileListerDialog()` is empty, and the associated field is never initialized.

These paths appear broken and unreachable, but the callback is framework-facing. Remove only after confirming no persisted request state or hidden launch reaches request codes `0x3e7` or `0x68`.

## Runtime Confirmation Required

### Added resources

Sixty-four inherited drawables, four layouts, two fonts, and several dimensions/IDs/styles/colors are statically unreferenced outside their definitions and public declarations. They are strong pruning candidates, not proven dead, because Android resources can be resolved numerically, reflectively, or by server-driven code.

The active settings graph definitely retains:

- `instander_settings.xml`
- `instander_layout_item.xml`
- `instander_layout_action_bar.xml`
- `instander_action_bar.xml`
- `instander_item.xml`
- `instander_item_disable.xml`
- `instander_about_info.xml`
- The ten icons referenced by active preference XML
- `pref_content`, active colors/dimension, `google_sans`, version arrays
- `DfInstagramPreference`

Before pruning resources:

1. Traverse every nested settings screen.
2. Monitor for `Resources$NotFoundException` and inflation errors.
3. Remove each resource together with its public declaration and transitive leaves.
4. Rebuild and rerun startup/settings tests after each logical bundle.

### Public utility methods

Several public methods have no static caller, including old browser and dynamic resource helpers. Keep them until reflection/runtime lookup is ruled out; unlike private members, text-search absence is not proof.

## Not Dead Code

Amplitude and ACRA are reachable startup behavior. They may be removed for privacy/product reasons, but must not be classified as dead residue. See `docs/PRIVACY_1.4.1.md`.

Core `DistractionFree`, Tigon, context, cache, settings, and welcome classes have direct host callers and remain required.

## Recommended Cleanup Sequence

1. Remove the safe boundary above in one behavior-neutral branch.
2. Rebuild from clean stock and run the DEX contract.
3. Install and run startup, welcome, settings, and verified feature contrasts.
4. Prune resources in small dependency bundles with a full settings traversal after each bundle.
5. Keep cleanup commits separate from privacy removal and the future 430 port.
