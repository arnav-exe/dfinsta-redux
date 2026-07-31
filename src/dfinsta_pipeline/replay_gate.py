"""Pure derivation helpers for the final replay verification gate.

The final verification gate is authorized *after* the build stage completes: its
subject binds a build receipt that does not exist when the replay itself is
admitted.  Two Activities therefore derive the same subject independently -- one
publishes only its hash so the Workflow can wait on a human decision, the other
re-derives it when the decision arrives and admits the grant.  Neither can trust
the other's copy, so derivation has to be a pure function of already-recorded
state: identical arguments must always produce byte-identical canonical JSON.

`derive_verification_request` is consequently side-effect free.  It reads no
ledger, no content store, no clock, no environment and no filesystem; every
field comes from its arguments.  Ledger access is confined to the resolver
helpers below, which only *fetch* the exact recorded objects that derivation
consumes.

Nothing here names a concrete port target or APK file.  Every identifier is
derived from `admitted.run_spec.run_id`, which is the only run-scoped name the
admitted authority already carries.
"""

from __future__ import annotations

from .contracts import ArtifactRef
from .executor import ExecutorCapability
from .ledger import Ledger
from .replay_contracts import (
    ID_PATTERN,
    AdmittedReplayV3,
    ReplayPatchedApkReceiptV1,
    ReplayVerificationGrantRequestV1,
)

DECODE_ROLE = "decode"
BUILD_OPERATION_KIND = "replay_build_patched_apk_v1"

#: Suffixes appended to `run_id` to name the gate's own objects.  They are part
#: of the derived bytes, so changing one changes every request hash.
GRANT_ID_SUFFIX = "-final-verification-grant"
GATE_ID_SUFFIX = "-final-verification-gate"
CAPABILITY_ID_SUFFIX = "-final-verification-decode"

#: `ReplayVerificationGrantRequestV1.__post_init__` pins the verification
#: capability to exactly these values; it rejects any other argv, path
#: arguments, input kind, output kind, environment or mutation paths.  They are
#: restated here so derivation satisfies the validator by construction.
FINAL_DECODE_ARGV = (
    "-jar",
    "{tool}",
    "d",
    "-f",
    "{input_apk}",
    "-o",
    "{decoded_tree}",
    "-p",
    "{framework_dir}",
)
FINAL_DECODE_PATH_ARGUMENTS = (
    "decoded_tree",
    "framework_dir",
    "input_apk",
    "tool",
)
FINAL_DECODE_INPUT_KINDS = ("final-apk",)
FINAL_DECODE_OUTPUT_KIND = "decoded-tree"
FINAL_DECODE_ALLOWED_ENVIRONMENT: tuple[str, ...] = ()
FINAL_DECODE_FIXED_ENVIRONMENT: tuple[tuple[str, str], ...] = ()
FINAL_DECODE_MUTATION_PATHS = ("framework", "output")


def derived_identifier(run_id: str, suffix: str, label: str) -> str:
    """Name a gate object after `run_id` without inventing run-scoped state.

    The contract validates `grant_id` and `gate_id` with the same identifier
    pattern it applies to `run_id`, and that pattern is length-bounded, so a
    long run id can push a suffixed name out of range.  Failing loudly beats
    truncating: a truncated name could collide with a different run's gate.
    """

    if type(run_id) is not str:
        raise TypeError("Verification run id must be a string")
    if type(suffix) is not str:
        raise TypeError("Verification identifier suffix must be a string")
    value = f"{run_id}{suffix}"
    if not ID_PATTERN.fullmatch(value):
        raise ValueError(f"Derived {label} is not a valid identifier")
    return value


def derive_verification_capability(
    admitted: AdmittedReplayV3, run_id: str
) -> ExecutorCapability:
    """Derive the final-decode-only capability the gate authorizes.

    The executable is the admitted decode executable -- `admitted.capability`
    resolves the role binding recorded in the toolchain profile, which is the
    same accessor `replay_decode_checkpoint_activity` uses.  Everything else is
    pinned by the request contract.
    """

    return ExecutorCapability(
        1,
        derived_identifier(run_id, CAPABILITY_ID_SUFFIX, "verification capability id"),
        admitted.capability(DECODE_ROLE).executable_sha256,
        FINAL_DECODE_ARGV,
        FINAL_DECODE_PATH_ARGUMENTS,
        FINAL_DECODE_INPUT_KINDS,
        FINAL_DECODE_OUTPUT_KIND,
        FINAL_DECODE_ALLOWED_ENVIRONMENT,
        FINAL_DECODE_FIXED_ENVIRONMENT,
        FINAL_DECODE_MUTATION_PATHS,
    )


def derive_verification_request(
    admitted: AdmittedReplayV3,
    completed_build: ArtifactRef,
    build_receipt: ReplayPatchedApkReceiptV1,
) -> ReplayVerificationGrantRequestV1:
    """Derive the final verification gate subject from recorded authority only.

    Pure: no ledger, no store, no clock, no I/O.  `completed_build` must be the
    exact `ArtifactRef` the ledger recorded for the build operation (see
    `resolve_completed_build`) and `build_receipt` its parsed payload.
    """

    if type(admitted) is not AdmittedReplayV3:
        raise TypeError("Admitted replay must be an exact AdmittedReplayV3")
    if type(completed_build) is not ArtifactRef:
        raise TypeError("Completed build must be an exact ArtifactRef")
    if type(build_receipt) is not ReplayPatchedApkReceiptV1:
        raise TypeError("Build receipt must be an exact ReplayPatchedApkReceiptV1")

    run_spec = admitted.run_spec
    run_id = run_spec.run_id
    return ReplayVerificationGrantRequestV1(
        1,
        derived_identifier(run_id, GRANT_ID_SUFFIX, "verification grant id"),
        run_id,
        derived_identifier(run_id, GATE_ID_SUFFIX, "verification gate id"),
        run_spec.allowed_actor,
        run_spec.policy_revision,
        admitted.sha256,
        completed_build,
        build_receipt.patched_apk,
        admitted.profile.profile_id,
        admitted.profile.tool_for_role(DECODE_ROLE).artifact_sha256,
        admitted.plan(DECODE_ROLE).timeout_seconds,
        derive_verification_capability(admitted, run_id),
    )


def resolve_completed_build(
    ledger: Ledger, build_receipt: ReplayPatchedApkReceiptV1
) -> ArtifactRef:
    """Return the exact completed build `ArtifactRef` the receipt claims.

    The ledger call is deliberately unbound (`Ledger.require_completed_operation`
    with the instance passed explicitly): an exact-type check on `ledger` does
    not stop a per-instance attribute from shadowing the method, so the
    class-level function is looked up directly.
    """

    if type(build_receipt) is not ReplayPatchedApkReceiptV1:
        raise TypeError("Build receipt must be an exact ReplayPatchedApkReceiptV1")
    return Ledger.require_completed_operation(
        ledger,
        build_receipt.operation_key,
        BUILD_OPERATION_KIND,
        build_receipt.expected_operation_input_sha256,
    )


def resolve_admitted_build(
    admitted: AdmittedReplayV3,
) -> tuple[ArtifactRef, ReplayPatchedApkReceiptV1]:
    """Locate the completed build receipt for an admitted replay.

    This is the pre-grant half of `activities._replay_verification_predecessors`:
    that function reconstructs the build operation identity from the admitted
    replay and validates the recorded receipt, but it can only be called with an
    `AdmittedReplayVerificationGrantV1`, which does not exist yet when the gate
    subject is being derived.  Rather than restate the chain, the same private
    helpers are reused verbatim.

    `activities` is imported inside the function on purpose: the Activities that
    call this module import it at module scope, so a module-level import here
    would close an import cycle.
    """

    from . import activities

    (
        completed_framework,
        framework_receipt,
        completed_apply,
        patched_tree_receipt,
        compiled,
    ) = activities._replay_build_predecessors(admitted)
    build_key, build_input, build_request = activities._replay_build_operation_identity(
        admitted,
        completed_apply,
        patched_tree_receipt,
        compiled,
        completed_framework,
        framework_receipt,
    )
    completed_build = Ledger.require_completed_operation(
        activities.runtime().ledger,
        build_key,
        BUILD_OPERATION_KIND,
        build_input,
    )
    build_receipt = activities._validate_replay_patched_apk_receipt(
        completed_build,
        build_key,
        admitted=admitted,
        completed_patched_tree_receipt=completed_apply,
        patched_receipt=patched_tree_receipt,
        compiled=compiled,
        execution_request=build_request,
        completed_framework_cache_receipt=completed_framework,
        framework_receipt=framework_receipt,
    )
    return completed_build, build_receipt
