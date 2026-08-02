# DFInstagram Auto-Upgrade — google-adk Pipeline Design

> **Superseded concept document — do not implement from this file.**
>
> Written 2026-06-07, before Temporal became the durable orchestrator. `docs/ADK_PIPELINE_PLAN.md`
> is the authority; it explicitly incorporates this document but corrects its pre-ADK-2.x
> assumptions and unsafe authority boundaries. Retained for the design reasoning behind the
> agent topology and the feature-assessment schema, both of which remain relevant to the
> eventual ADK layer.
>
> Known contradictions with the implemented architecture: this file makes an ADK `SequentialAgent`
> the root orchestrator (Temporal is), routes human approval through `LongRunningFunctionTool`
> (validated Temporal Updates are authoritative), names two human gates (there are four), lets an
> agent write institutional memory directly (forbidden), and assumes an `apktool b --use-aapt1`
> full rebuild is the only build spine. That last assumption is disproven for Instagram 430: the
> full resource rebuild is lossy and produced a crashing APK, which is why the 430 target uses a
> stock DEX graft. See `docs/PORT_430_MAPPING.md`.

Grounded in the dry-run findings (FINDINGS.md, F1–F16). Built **strictly on google-adk**.

## Design principles (earned from the dry run)
1. **Deterministic spine, LLM only at the seams.** apktool decode, indexing, normalized diff,
   anchored apply, build, dex-verify are all deterministic — they are ADK *tools* run by thin
   `BaseAgent` stages, NOT `LlmAgent`s. Reserve `LlmAgent` for the genuinely ambiguous work:
   fingerprint scoring, field/anchor remap, feature assessment, build-error triage, reporting.
   (Over-agentifying deterministic steps burns tokens and adds nondeterministic failure modes.)
2. **Artifacts by reference, never by value.** Decodes are ~GB; the index is 35 MB; a single class
   can be 648 KB. These NEVER enter `session.state` or an LLM prompt. Stash them via
   `ArtifactService` / filesystem and pass PATHS + small summaries. LLMs receive only excerpts.
3. **The hook manifest is the durable backbone.** A versioned, declarative file is the source of
   truth across releases — not whole-class copies. Each upgrade re-resolves it against the new APK.
4. **Two human gates, persisted.** (a) new-feature triage [your idea], (b) low-confidence / broken
   port of an existing hook [you missed this]. Both pause via `LongRunningFunctionTool` and must
   survive process restarts → `DatabaseSessionService`, not `InMemorySessionService`.
5. **Verification is behavioral, and release stays human-gated.** "It assembled" ≠ "it works"
   (a dead JSON-rewriter assembles fine). Sign/distribute is never automated.

## The hook manifest (per-hook, version-independent intent)
```yaml
- id: block_feed_timeline
  intent: "block the main feed when disable_feed is set"
  tier: robust                 # robust | fragile | ui
  strategy: url_block          # url_block | response_rewrite | ui_suppress | lifecycle
  semantic_deps: ["/feed/timeline/"]      # stable strings that must still exist
  host_fingerprint:            # how to RE-LOCATE the host each version
    kind: named
    descriptor: "Lcom/instagram/api/tigon/TigonServiceLayer;"
  anchor: { after: "->logQPL(", then_label: ":try_start_0" }
  payload_template: |
    iget-object v0, {req_reg}, {reqinfo_cls}->{uri_field}:Ljava/net/URI;
    invoke-static {v0}, Lcom/dfinstagram/hooks;->throwIfBlocked(Ljava/net/URI;)V
  remap:                       # symbols resolved per-version from the target body
    reqinfo_cls: { by: field_type, type: "Ljava/net/URI;", role: request_info }
  status: active               # active | dropped@1.4.1 | needs_review
```

## Agent topology (ADK)
```
SequentialAgent  DFInstaUpgrade
├─ ExtractAgent            (BaseAgent)      decode old+new APK  → state.paths           [deterministic]
├─ IndexAgent              (BaseAgent)      structural + API-surface index             [deterministic]
├─ FeatureDiscoveryAgent   (LlmAgent+tools) diff API surface, classify new features    [LLM]
│     tools: diff_api_surface, query_index, fetch_play_notes(optional)
├─ ReportFormatterAgent    (LlmAgent,       emit FeatureReport (Pydantic)              [LLM]
│                           output_schema)   ← split out because output_schema disables tools
├─ FeatureTriageGate       (LongRunningTool) PAUSE → human keep/remove/toggle per feature [HUMAN]
├─ PortPlannerAgent        (LlmAgent+tools) re-resolve manifest hosts in new version    [LLM]
│     tools: locate_by_descriptor, locate_by_interface, score_candidates,
│            extract_delta, resolve_remap
├─ PortReviewGate          (LongRunningTool) PAUSE *only if* low-confidence/broken hooks [HUMAN]
├─ LoopAgent  ApplyBuildVerify (max_iters=3)                                            [mostly det.]
│     ├─ ApplyAgent        (BaseAgent)      idempotent anchored inserts + new hooks
│     ├─ BuildAgent        (BaseAgent)      apktool b --use-aapt1; assemble check
│     └─ VerifyAgent       (LlmAgent)       dex-symbol + behavioral(adb); triage errors,
│                                            escalate=True on success or give-up
├─ ManifestUpdateAgent     (BaseAgent)      write resolved names + new/dropped hooks
└─ FinalReportAgent        (LlmAgent)       human-readable changelog + what needs sign-off
```
Cross-cutting: `before_tool_callback` guardrail (deny sign/publish/destructive); `after_tool_callback`
validates outputs (assembled dex exists, etc.); `ArtifactService` holds decodes/index/APK/reports.

## The FeatureAssessment schema (reframes "addictiveness")
`addictive` is too subjective for the agent to assert alone. Classify on observable axes, tie
confidence to evidence, and let the human decide:
```python
class FeatureAssessment(BaseModel):
    name: str
    evidence: list[str]          # new endpoints / QE-flags / string-res / manifest entries
    delivery_branch: Literal["A_endpoint","B_inline_response","C_client_ui"]  # = feasibility+cost
    maps_to_existing_category: str | None   # ads/reels/explore/stories/suggested/shopping | None
    engagement_signals: list[str]           # autoplay, infinite_scroll, recommendation, push
    recommendation: Literal["block","offer_toggle","ignore"]
    blockability: Literal["easy_url","medium_json","hard_ui","unknown"]
    confidence: float            # 0..1, justified by signal strength
    rationale: str
```
Delivery_branch is the key output: **A (own endpoint) → one line in `throwIfBlocked`, fully
auto. B (inline JSON) → fragile rewriter, review. C (pure UI) → hard RE, human.**

## Feature discovery = diff the STABLE-STRING layer, not classes
Obfuscation churns class names every release, so a class-level diff shows "everything changed"
(F11). Anchor discovery on signals that survive obfuscation:
- distinct URL-path literals (`/api/v1/.../`)   ← strongest; gives Branch-A blockables directly
- SharedPreferences / QE / mobileconfig flag names
- string + layout resource names; new AndroidManifest activities/permissions
`build_api_surface(decode)` extracts these into a set; `diff_api_surface(old,new)` yields the new
surface. THAT is the feature-candidate list. Play-Store notes are weak corroboration only.

## HITL mechanics in ADK
- Use `LongRunningFunctionTool`. The tool returns immediately with `{"status":"pending","ticket":id,
  "report": <small summary>}`. The run yields; the agent loop is suspended. Persist the session
  (`DatabaseSessionService`) so the pause can last days across restarts.
- A separate channel (your UI/email) shows the report; you reply with decisions.
- Resume by sending a `types.FunctionResponse` (same ticket id) carrying the decisions; the Runner
  continues from the suspended tool. Decisions land in `session.state` for downstream agents.
- For the simpler PortReviewGate (approve/reject a low-confidence port) ADK's tool-confirmation
  flow also works; LongRunningFunctionTool is the robust general primitive.

## Skeleton (illustrative; tool bodies wrap the dry-run scripts)
```python
from google.adk.agents import LlmAgent, SequentialAgent, LoopAgent, BaseAgent
from google.adk.tools import FunctionTool, LongRunningFunctionTool
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from pydantic import BaseModel
from typing import AsyncGenerator, Literal

# ---- deterministic tools (wrap apktool/index/diff/apply/build from the dry run) ----
def decode_apk(apk_path: str, out_dir: str) -> dict: ...        # java -jar apktool.jar d (NOT .bat)
def build_structural_index(decode_dir: str) -> str: ...         # one rg pass -> headers index
def build_api_surface(decode_dir: str) -> dict: ...             # endpoints/flags/res/perms
def diff_api_surface(old: dict, new: dict) -> dict: ...
def locate_by_interface(index: str, iface: str) -> list[str]: ...
def score_candidates(index: str, signals: dict) -> list[dict]:...# multi-signal intersection
def extract_delta(base_file: str, mod_file: str) -> list[str]:...# normalize+diff
def resolve_remap(target_file: str, spec: dict) -> dict: ...     # field by type+finality
def apply_hook(target_file: str, anchor: dict, payload: str) -> dict: ...  # idempotent
def build_apk(decode_dir: str, use_aapt1: bool = True) -> dict: ...
def verify_symbols(apk: str, symbols: list[str]) -> dict: ...
def run_behavioral_test(apk: str) -> dict: ...                  # adb/emulator

# ---- deterministic stage pattern: a BaseAgent that just runs a tool ----
class ExtractAgent(BaseAgent):
    async def _run_async_impl(self, ctx) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        s["old"] = decode_apk(s["old_apk"], s["work"]+"/old")
        s["new"] = decode_apk(s["new_apk"], s["work"]+"/new")
        yield Event(author=self.name, content=types.Content(
            parts=[types.Part(text="decoded old+new")]))

# ---- new-feature triage gate (HUMAN) ----
def present_feature_report(report: dict) -> dict:
    # returns immediately; framework suspends until a FunctionResponse arrives
    return {"status": "pending", "ticket": report["id"], "report": report}
feature_gate = LongRunningFunctionTool(func=present_feature_report)

feature_discovery = LlmAgent(
    name="FeatureDiscoveryAgent", model="gemini-2.5-pro",
    tools=[FunctionTool(diff_api_surface), FunctionTool(build_api_surface)],
    instruction="Diff old vs new API surface. For each NEW endpoint/flag/surface, assess it. "
                "Anchor on stable strings, not class names. Write findings to state['features'].",
    output_key="features")

class FeatureReport(BaseModel):
    features: list["FeatureAssessment"]
report_formatter = LlmAgent(           # output_schema => NO tools here (ADK constraint)
    name="ReportFormatterAgent", model="gemini-2.5-flash",
    instruction="Format state['features'] into the FeatureReport schema.",
    output_schema=FeatureReport, output_key="feature_report")

triage_gate = LlmAgent(
    name="FeatureTriageGate", model="gemini-2.5-flash", tools=[feature_gate],
    instruction="Call present_feature_report with state['feature_report']; on resume store the "
                "human's per-feature decisions (block/offer_toggle/ignore) into state['decisions'].")

# ---- apply/build/verify loop with retry+triage ----
class VerifyAgent(LlmAgent): ...     # escalate=True on verified-or-giveup
build_loop = LoopAgent(name="ApplyBuildVerify", max_iterations=3,
                       sub_agents=[ApplyAgent(name="Apply"), BuildAgent(name="Build"),
                                   VerifyAgent(name="Verify", model="gemini-2.5-pro")])

root = SequentialAgent(name="DFInstaUpgrade", sub_agents=[
    ExtractAgent(name="Extract"), IndexAgent(name="Index"),
    feature_discovery, report_formatter, triage_gate,
    PortPlannerAgent(name="PortPlanner"), PortReviewGate(name="PortReview"),
    build_loop, ManifestUpdateAgent(name="Manifest"), FinalReportAgent(name="FinalReport")])

runner = Runner(agent=root, app_name="dfinsta",
                session_service=DatabaseSessionService(db_url="sqlite:///dfinsta.db"))
```

## Critique of the proposed plan (what to change)
1. **Reframe "addictive opinion".** The agent can't reliably judge addictiveness, and it's
   subjective. Output the evidence-based `FeatureAssessment` (delivery branch, category match to
   dfinsta's existing targets, engagement signals) + confidence; keep YOU as the decider. The
   single most useful field is delivery_branch — it tells you the cost before you decide.
2. **Discover on the stable-string layer, not by class diff.** Obfuscation makes class diffs pure
   noise. Diff endpoints/flags/resources/permissions instead.
3. **Play-Store notes are weak** ("bug fixes & improvements"). Use only as corroboration; primary
   signal is the API-surface diff. (Optional tool, low weight.)
4. **Add a SECOND human gate** for low-confidence / semantically-broken ports of EXISTING hooks
   (e.g., the host can't be uniquely located, or a `semantic_dep` string vanished — the exact
   situation that silently killed the suggested-posts hook). Your plan only gates new features.
5. **"It built" is not verification.** Assembly/packaging succeed even for dead hooks. Require a
   behavioral check (adb/emulator) per feature; a feature-block that can't be verified is a flag.
6. **Don't over-agentify.** Keep the extract/index/diff/apply/build spine deterministic (BaseAgent
   + tools); LLM only at discovery/planning/triage/report. Cheaper, more reliable, easier to debug.
7. **Persist sessions** (`DatabaseSessionService`) — your "put it on hold" can last days; an
   in-memory session dies on restart and loses the run.
8. **Stream artifacts by reference.** Never feed decodes/index/large classes through state or the
   model; pass paths + excerpts (ArtifactService).
9. **Make it stateful across versions via the manifest**, so a feature decided once (block/toggle/
   ignore) is remembered and auto-ported next release instead of re-triaged from scratch.
10. **Keep signing/distribution human-gated** (guardrail callback). The pipeline produces an
    unsigned APK + a report; a human signs and ships.
