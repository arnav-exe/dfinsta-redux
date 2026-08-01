"""Give every hook a runtime identity, so presence stops being mistaken for execution.

Four separate failures in this project were the same failure: a patch that was
present and never ran.

    340  `minshop` was substituted while every identifier said `minishops`,
         so the comparison could never match
    430  the settings hook applied cleanly, passed every static assertion, and
         was dead because MobileConfig picked the other action-bar implementation
    439  the action-bar settings hook attaches to a method that appears never to
         be dispatched
    —    a verifier searched DEX bytes for a smali string form DEX does not store,
         so the check itself never fired

Each was found by a different ad-hoc investigation, none by a standing check, and
each took a version's worth of hindsight. Adding a reachability analyser would be
a fifth ad-hoc answer — and Android reachability is undecidable anyway, because
reflection and dependency injection call methods nothing references.

So this takes the direct route: **make each injection site say when it runs.**
Every payload gains one instruction calling a no-argument method named after its
hook. The method name is the identity, which means:

  * it needs **no registers**, so it never forces a `.locals` change and never
    competes with the payload for scratch space — this is why the identity is in
    the method name rather than a string argument
  * the DEX string table ends up carrying one literal per hook, so the *static*
    verifier can also prove each hook's site is present in the DEX that owns it
  * at runtime one line per hook per process says the site executed, regardless
    of what the user's toggles are set to

That last point is the one that matters. A probe watching a shared feature signal
cannot tell three Reels endpoint hooks apart, or say which of two settings
variants opened a dialog — both real cases here, and the second is how a probably
dead hook passed verification. A per-hook line is unambiguous by construction.

The emitted class is generated from the manifest rather than hand-written, so the
hook list and the probe methods cannot drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Sequence

from .hook_manifest import Hook, ManifestError

PROBE_DESCRIPTOR = "Lcom/dfinstagram/probe;"
PROBE_CLASS_PATH = "com/dfinstagram/probe.smali"
LOG_TAG = "DFInstaProbe"

#: Method names are derived from hook ids, so they must be valid Java identifiers.
SAFE_NAME = re.compile(r"[^A-Za-z0-9_]")


def probe_method(hook_id: str) -> str:
    """`replace_reels_discover_endpoint` -> `h_replace_reels_discover_endpoint`.

    Prefixed because a hook id may begin with a digit, and kept a pure function of
    the id so the name in the payload, the name in the generated class and the
    name the verifier looks for are the same string by construction.
    """
    if not hook_id.strip():
        raise ManifestError("cannot derive a probe method from an empty hook id")
    return "h_" + SAFE_NAME.sub("_", hook_id.strip())


def probe_call(hook_id: str) -> str:
    """The single instruction a payload adds. Takes no registers, by design."""
    return f"    invoke-static {{}}, {PROBE_DESCRIPTOR}->{probe_method(hook_id)}()V"


def assert_distinct_identities(
    hooks: Sequence[Hook], require_any: bool = False
) -> list[Hook]:
    """Active hooks, refusing any set whose ids collide into one probe method.

    The collision check is shared by the generator and the verifier map so the
    two cannot disagree about what a valid hook set is. They did: the generator
    rejected `a.b` and `a_b` while the map silently returned the same
    `(descriptor, method)` pair for both, which defeats the static half of
    attribution at exactly the point its docstring claims it holds — the verifier
    would no longer be able to tell the two hooks apart.

    ``require_any`` is NOT shared, deliberately. Generating a probe class with no
    methods means nothing is instrumented and is refused. An empty *symbol map*
    is a truthful answer to "what should the verifier look for" when every hook
    is retired, and the vacuity danger there is already handled where it matters:
    `verify_build.verify` refuses an empty host-hook map, because a verifier with
    nothing to prove passes everything.
    """
    active = [hook for hook in hooks if hook.status == "active"]
    if require_any and not active:
        raise ManifestError("refusing to derive runtime identities from no hooks")
    names = [probe_method(hook.hook_id) for hook in active]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        # Two hook ids differing only in punctuation would collide into one
        # method, and then one hook's execution would be reported as the other's.
        raise ManifestError(
            f"hook ids collide into the same probe method(s): {sorted(duplicates)}"
        )
    return active


def render_probe_class(hooks: Sequence[Hook]) -> str:
    """Generate `com/dfinstagram/probe.smali` for exactly these hooks.

    Dedup uses `ConcurrentHashMap.putIfAbsent`, not a `HashSet`: `throwIfBlocked`
    runs on Instagram's network threads and a plain HashSet mutated concurrently
    can spin or corrupt. The manifest says this helper must not throw, and a
    logging helper that can hang the app would be a worse bug than the one it
    exists to catch.

    `putIfAbsent` returning null means the id was absent, i.e. this is the first
    execution of that site in this process — so exactly one line is logged per
    hook per process however hot the site is.
    """
    active = assert_distinct_identities(hooks, require_any=True)
    lines = [
        f".class public final {PROBE_DESCRIPTOR}",
        ".super Ljava/lang/Object;",
        "",
        "# GENERATED from manifest/hooks.json by "
        "src/dfinsta_pipeline/runtime_identity.py. Do not edit by hand.",
        "#",
        "# One no-argument method per hook. The method NAME is the hook identity,",
        "# so a call site needs no registers and can never force a .locals change.",
        "",
        "# static fields",
        f".field private static final A00:Ljava/util/concurrent/ConcurrentHashMap;",
        "",
        "# direct methods",
        ".method static constructor <clinit>()V",
        "    .locals 1",
        "",
        "    new-instance v0, Ljava/util/concurrent/ConcurrentHashMap;",
        "",
        "    invoke-direct {v0}, Ljava/util/concurrent/ConcurrentHashMap;-><init>()V",
        "",
        f"    sput-object v0, {PROBE_DESCRIPTOR}->A00:Ljava/util/concurrent/ConcurrentHashMap;",
        "",
        "    return-void",
        ".end method",
        "",
        ".method public constructor <init>()V",
        "    .locals 0",
        "",
        "    invoke-direct {p0}, Ljava/lang/Object;-><init>()V",
        "",
        "    return-void",
        ".end method",
        "",
        ".method private static fired(Ljava/lang/String;)V",
        "    .locals 3",
        "",
        f"    sget-object v0, {PROBE_DESCRIPTOR}->A00:Ljava/util/concurrent/ConcurrentHashMap;",
        "",
        "    invoke-virtual {v0, p0, p0}, Ljava/util/concurrent/ConcurrentHashMap;->"
        "putIfAbsent(Ljava/lang/Object;Ljava/lang/Object;)Ljava/lang/Object;",
        "",
        "    move-result-object v1",
        "",
        "    if-nez v1, :cond_already_logged",
        "",
        f'    const-string v2, "{LOG_TAG}"',
        "",
        "    invoke-static {v2, p0}, Landroid/util/Log;->"
        "i(Ljava/lang/String;Ljava/lang/String;)I",
        "",
        "    :cond_already_logged",
        "    return-void",
        ".end method",
    ]
    for hook in active:
        lines += [
            "",
            f".method public static {probe_method(hook.hook_id)}()V",
            "    .locals 1",
            "",
            f'    const-string v0, "{hook.hook_id}"',
            "",
            f"    invoke-static {{v0}}, {PROBE_DESCRIPTOR}->fired(Ljava/lang/String;)V",
            "",
            "    return-void",
            ".end method",
        ]
    return "\n".join(lines) + "\n"


def instrument(payload: Sequence[str], hook_id: str) -> tuple[str, ...]:
    """Prepend the probe call to a payload, idempotently.

    Placed first so the site reports execution even if a later instruction in the
    payload throws — the question being answered is "did control reach here",
    which is exactly the question a partially-failing payload also raises.
    """
    call = probe_call(hook_id)
    if any(line.strip() == call.strip() for line in payload):
        return tuple(payload)
    return (call, "") + tuple(payload)


def is_instrumented(payload: Iterable[str], hook_id: str) -> bool:
    call = probe_call(hook_id).strip()
    return any(line.strip() == call for line in payload)


def write_probe_class(hooks: Sequence[Hook], custom_code_root: Path) -> Path:
    """Write the generated class into a patch source's `newCode/` tree."""
    destination = Path(custom_code_root) / PROBE_CLASS_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_probe_class(hooks), encoding="utf-8")
    return destination


def expected_dex_symbols(hooks: Sequence[Hook]) -> dict[str, tuple[str, str]]:
    """`hook_id -> (descriptor, method)` the static verifier should look for.

    A DEX stores a method reference as three separate indices, so only the type
    descriptor and the bare method name exist as literal strings. That pair is
    what a byte search can actually find — and because the method name is unique
    per hook, this turns "some DFInsta call is in this DEX" into "THIS hook's
    call is in this DEX", which is the static half of the same attribution
    problem the runtime line solves.
    """
    return {
        hook.hook_id: (PROBE_DESCRIPTOR, probe_method(hook.hook_id))
        for hook in assert_distinct_identities(hooks)
    }
