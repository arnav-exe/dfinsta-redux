# Instagram 439 Reconnaissance

First read on porting from 430 to Instagram `439.0.0.37.89`
(`apks/instagram_439-0-0-37-89.apk`, SHA-256
`bd9a6fa01eff928344952876166d56eacf42a215a6fc1b4dceee8e3c65d80e08`).
Decoded 2026-08-01 with apktool 2.9.3 and the API 36 framework to
`work/439-explore/stock-439` — 20 smali trees, 1.7 GiB.

This is reconnaissance, not a port. ~~Nothing has been patched or built for 439.~~ **Stale — 439 was ported, built and device-proved on 2026-08-02**, and 440 and 441 have shipped since. `dfinsta_source_439/`, `manifest/runtime_evidence/439.jsonl` and `manifest/differentials/439-440.jsonl` are committed. Kept as the *pre-port* prediction, which is its value: it called the settings hooks the hard case and the honest test of any automated mapper, and `kind: by_anchor` went on to resolve all seven mechanically, for zero agent invocations on 440 and 441.

## The headline: obfuscated names are recycled, so name existence is a false positive

Every obfuscated host our 430 port hooks — `LX/077K`, `LX/05t2`, `LX/06X7`,
`LX/00ds`, `LX/09rb` — **still exists by name in 439**. A port that checked
"does the class still exist?" would conclude the mapping survived intact. It did
not.

`LX/05t2` is the proof:

| | 430 | 439 |
|---|---|---|
| path | `smali_classes4/X/05t2.smali` | `smali_classes3/X/05t2.smali` |
| lines | 1990 | 596 |
| Reels endpoint literals | 2 | **0** |

Same name, different class. The 439 class that actually holds the Reels
endpoints is **`LX/04tC`** (`smali_classes3`, 2040 lines, both literals). A port
that trusted the name would patch an unrelated class, and might well assemble
and verify cleanly while doing nothing — the same inert-patch failure mode as
the 430 settings hook and the 340 `minshop` bug.

**Any automated mapper must resolve by content, never by descriptor name.**

## What survives, and what churns

**Stable named types survive exactly.** Both live at resolvable paths:

- `com/instagram/app/InstagramAppShell` — `smali_classes3` in both
- `com/instagram/api/tigon/TigonServiceLayer` — `smali` in both

**But their signatures churn.** Every obfuscated parameter and return type in
the Tigon entry point was renamed:

```
430: startRequest(LX/05ez;LX/05fq;LX/05gu;)LX/0Fcm;
439: startRequest(LX/03AS;LX/03AV;LX/03Ah;)LX/095m;
```

So `tigon_url_block` will not match as written: its anchor reads
`iget-object v1, p1, LX/05ez;->A08:Ljava/net/URI;`, and both the owning type and
possibly the field name must be re-resolved against 439.

**Endpoint string literals survive verbatim** — the single best fingerprint:

| literal | files in 430 | files in 439 |
|---|---|---|
| `"clips/discover/stream/"` | 3 | 4 |
| `"clips/homecoming/"` | 2 | 2 |
| `"clips/discover/"` | 7 | 5 |
| `"feed/timeline/"` | — | 8 |
| `"discover/topical_explore/"` | 6 | 6 |
| `"feed/reels_tray/"` | — | 5 |
| `"profile_ads/get_profile_ads/"` | — | 1 |

Note the trailing slash. Searching for `"discover/topical_explore"` with a
closing quote returns zero hits in both versions and invites the false
conclusion that Explore blocking is gone. It is not.

**Structural shape survives even when names do not.** The 430 Reels builder
method and its 439 counterpart have near-identical shapes — a long parameter
list ending in `Ljava/util/List;Lkotlin/jvm/functions/Function0;` followed by
eleven `Z` booleans:

```
430  LX/05t2;->A09(... Ljava/util/List;Lkotlin/jvm/functions/Function0;ZZZZZZZZZZZ)LX/03xp;
439  LX/04tC;->A0A(... Ljava/util/List;Lkotlin/jvm/functions/Function0;ZZZZZZZZZZZ)LX/02ue;
```

Method letter, owning class, and return type all changed; the shape did not.

## What this implies for the mapper

The fingerprint precedence already stated in `AGENTS.md` is confirmed by data,
and should be the mapper's resolution order:

1. **Stable named types** — `InstagramAppShell`, `TigonServiceLayer`. Reliable
   for locating the class, useless for the signature.
2. **Stable string literals** — endpoint paths survived every rename. This is
   the strongest available signal and should be the primary index.
3. **Structural shape** — parameter-list topology and return arity survive
   obfuscation and disambiguate when a literal appears in several classes.
4. **Numeric constants** — last resort; resource ids are unresolvable here
   anyway because 439, like 430, uses sparse resource encoding.

Explicitly forbidden as a signal: the obfuscated descriptor itself.

## Estimated per-operation porting cost

| Operation | 439 outlook |
|---|---|
| `set_app_context` | low risk — stable class and method, only the live register needs re-checking |
| `tigon_url_block` | medium — class stable, but all three parameter types and the URI field must be re-resolved |
| `replace_reels_*` (3) | medium — host moves `LX/05t2` to `LX/04tC` and `smali_classes4` to `smali_classes3`; literals and shape both locate it |
| `install_settings_long_click` (2 variants) | highest — no stable name or literal anchors them; the 430 pair was found only by resource-id cross-reference and a device session |
| custom-code overlay | low — self-contained, but the target DEX index must be recomputed for 439's 20 trees |

The settings hooks are the hard case and the honest test of any automated
mapper. Everything else has a literal or a stable type to hang on.
