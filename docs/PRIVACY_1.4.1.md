# DFInsta 1.4.1 Privacy Audit

This document describes reconstructed oracle behavior. Reproducing historical behavior and choosing future product policy are separate decisions.

## Recommendation

- Remove unconditional Amplitude startup telemetry from the hardened baseline.
- Remove the inherited ACRA implementation, or replace it later with a maintained, opt-in, minimal crash-report flow.
- Do not port either subsystem to Instagram 430 by default.

Both systems are reachable oracle behavior, not dead code. Removal requires a rebuild plus startup/crash-path verification.

## Policy Status

The user approved the recommendation. The maintained 1.4.1 baseline removes both systems, and hardened DEX verification rejects their symbols. A clean build, in-place installation, cold startup, and settings regression passed. The historical analysis below remains as oracle evidence; neither subsystem should be ported to 430.

## Amplitude Startup Event

`InstagramAppShell.onCreate()` constructs `AmplitudeEventsSender` and calls `sendEventsAsync()` after initializing app context and ACRA. The wiring is recorded in:

- `dfinsta_source_1.4.1/oracleDeltas/host/com__instagram__app__InstagramAppShell.diff`
- `dfinsta_source_1.4.1/patches/anchored_patches.json`

Each invocation reads `Settings.Secure.ANDROID_ID` and sends JSON containing:

- Embedded API key `6c9787488577e23b3dc87a6227b5f5f9`
- `device_id`: Android ID
- `event_type`: `dfinsta_start`
- `app_version`: DFInsta version, currently `1.4.1`

Implementation: `dfinsta_source_1.4.1/newCode/com/dfinstagram/AmplitudeEventsSender.smali`.

Destination:

`https://api2.amplitude.com/2/httpapi`

The sender uses `HttpURLConnection`, not Instagram Tigon, so DFInsta's Tigon blocker does not mediate it. It has no user consent, visible toggle, opt-out, connect/read timeout, durable queue, or status validation. Exceptions are printed from a background thread. Transport also exposes ordinary connection metadata such as source IP to the service.

Android may instantiate the application class in more than one app-owned process. Static analysis cannot establish the exact event count per apparent launch.

### Policy

Remove it. A persistent device identifier is transmitted on startup for analytics unrelated to distraction blocking, without a user control. Oracle fidelity is not sufficient justification for carrying this into a future release.

## ACRA Crash Reporting

`InstagramAppShell` carries a runtime `@ReportsCrashes` annotation and calls `ACRA.init()` during startup. Configuration:

- Destination: `bugs@distractionfreeapps.com`
- Interaction mode: `TOAST`
- Transport: external email `ACTION_SEND`, not ACRA HTTP
- Toast resource: `bug_report`

The bundled ACRA code installs `ErrorReporter` as an uncaught-exception handler. With the observed mail configuration, the selected default report fields are:

- User comment
- Android version
- Application version name
- Device brand
- Phone model
- Custom data
- Stack trace

The configuration does not select ACRA's broader device-ID, settings, preference, or log fields. Stack traces can still contain sensitive values embedded in exception messages.

On a fatal Java crash, ACRA persists a local report, shows the configured toast, and starts an external email composer addressed to the project email. The user can inspect, edit, cancel, or send the draft, but report content has already been handed to the email application. The worker treats launching the composer as successful delivery and deletes its local report even if the user later cancels.

ACRA supports hidden `acra.enable`/`acra.disable` preferences, but DFInsta exposes neither. Users therefore have no practical in-app opt-out. The bundled version also has weak failure/retry behavior and is not a maintained product dependency.

### Policy

Remove it from the hardened baseline. If crash diagnostics are later required, implement an explicit opt-in flow that previews exact fields, redacts stack traces, exposes deletion/disable controls, and uses a maintained dependency.

## Runtime Evidence Still Missing

- Whether Amplitude still accepts the embedded key.
- Exact startup-event count across processes.
- Request headers, redirects, response status, and TLS behavior.
- Whether later Instagram initialization replaces ACRA's exception handler.
- Whether a current Android email app opens successfully and the exact rendered report body.

These questions do not block the remove recommendation. They matter only if historical network behavior must be fully characterized.
