# Data2G-first Pixel QSO implementation plan

## Objective

Make Pixel QSO a card-exchange application using Data2G's supported host APIs by default. Retain local and other modem implementations behind an explicit experimental backend adapter. Share card assembly, preview, integrity decisions, and QSO handling across adapters.

## Wrap-up status — 2026-10-05

The Data2G-first application implementation is ready for final release
validation. Data2G is the default backend; Experimental is opt-in. Dynamic
mode discovery, adapter ownership, checked fragment assembly, complete-card
integrity, transmit queue/ACK handling, and both automatic QSO roles are
implemented. The source caller and responder flows completed and persisted
QSO logs against isolated command/KISS fixtures. The pinned Data2G host also
accepted frames and generated audio into an in-memory sink with PTT disabled.
These results establish software integration, not reception by another station.

A shutdown guard now prevents a queued startup callback from launching a
managed host after the window closes. GUI regression fixtures now use isolated
settings directories and pass the current session identity to receive callbacks.

Final source regression: `QT_QPA_PLATFORM=offscreen PIXELQSO_DATA2G_HOST=127.0.0.1 .venv/bin/python -m unittest discover -s tests` passed all 89 tests in 72.732 seconds. `git diff --check` passed.

The final Linux rebuild succeeded: `dist/PixelQSO` is 141,343,240 bytes and includes the shutdown guard. Bundled-host `--help`, `--list-modes --kiss-bw 500`, and `--list-audio-devices` each returned exit code 0. Device enumeration does not establish physical streaming or PTT.

The current Linux packaged GUI also passed the Experimental UDP-bind failure check under Xvfb/xcb with isolated settings, test audio, and CAT/PTT disabled. A Python UDP socket held the receive port; the GUI displayed “Could not open the local test-audio port …: The bound address is already in use” and `NOT LISTENING`, while remaining responsive. This establishes the packaged failure-reporting path, not successful audio reception.

A source GUI handoff check completed in both directions against isolated command/KISS listeners and a real local UDP test-audio listener. Switching Data2G → Experimental sent `BCAST CLOSE 7`, released the session, and started UDP reception (`LISTENING`, receive timer active). Switching back released the UDP socket, stopped the local receive timer, and reconnected Data2G. CAT and PTT stayed inactive throughout. This closes the remote-fixture/UDP handoff path; managed-host and physical audio/CAT ownership still need validation. GitHub CLI authentication currently fails because the configured credential is invalid; no current cross-platform CI result was retrieved.

The existing local-only two-host integration scripts (`tools/test_data2g_python_hosts.py` and `tools/test_data2g_native_hosts.py`, ignored by Git) now discover common dynamically named modes, require both hosts to accept `BCAST MODE`, and size fragments to the smaller selected host frame limit. Their obsolete fixed Robust ID/256-byte assumptions would have prevented validation after the catalog migration. Syntax checks passed; neither audio integration script was run in this wrap-up. No received peer capture sidecars were found in the checkout, and both environment and stored GitHub CLI credentials failed authentication.

Remaining release gates:

- Exercise managed/remote backend ownership handoff through actual audio/CAT.
- Complete Windows/macOS builds and smoke checks.
- Exercise selected physical audio devices and PTT, then a complete exchange
  between two independent hosts, including disconnect/reconnect.
- Capture received peer frames and verify replay parity; perform controlled
  RF validation before claiming on-air readiness.

Experimental modem acquisition measurements and real audio/RF validation remain
experimental work. They do not establish Data2G readiness.

The phase details below retain the implementation history and evidence limits;
this summary is the current release checklist.

## Product shape

Pixel QSO should own the card/QSO experience and consume modem services through
backend adapters. **Data2G is the normal backend** and its supported host API is
the integration boundary. **Experimental modems** is a separate, opt-in backend
choice for Pixel QSO's local implementations and future compatible adapters.
The mode list then shows only modes belonging to the chosen backend. This keeps
backend ownership (audio, receive, radio/PTT, and lifecycle) distinct from the
selected modulation mode.

“Compatible with any experimental modem backend” means a backend can be added
when it can carry Pixel QSO's card protocol and report the adapter capabilities
and integrity evidence described below. It does not promise that arbitrary
modems, unknown wire formats, or remote applications can decode Pixel QSO cards.
Start with an internal adapter registry; define and document the contract
before considering third-party plugin loading.

## Starting behavior observed before implementation

- `data2g_transport.py` reads the host's full `MODES` response. However, `card_backends.host_mode_backends()` selects at most two presets: a QPSK “robust” mode and a 16-QAM “fast” mode. It prefers `qpsk-r1/2` and `16qam-r1/2`, with fallbacks within those families. Other advertised modes are not exposed.
- That selector also requires capacity for a 256-byte application frame plus Data2G's two-byte frame length. The transmitter independently hardcodes the same 256-byte cap. Smaller frames could make additional modes usable.
- Local Resilient 100/50/25 modes are still nonexperimental, and `resilient_100` is the normal fallback default.
- Data2G owns its radio decoding, audio, and PTT. Pixel QSO supplies application fragments through command/KISS APIs; it does not add a CRC per Data2G application fragment. Each fragment repeats a whole-raster CRC32 and content digest.
- Data2G delivers frames assembled from checked radio codewords. Those fragments can be trusted at the transport level while the complete image remains provisional until its application CRC passes. Failed radio codewords are not exposed through this API.
- Local Resilient uses independently FEC/CRC16-checked image blocks and can expose unchecked candidate pixels for previews. Its current image identity does not provide the same whole-raster checksum contract as the Data2G application format.
- `CardTransferReceiver` already retains out-of-order fragments and interleaved images, including across CQ/exchange stages. Its cache is bounded and in memory; conflicting overlapping bytes are rejected. The UI separately caches/merges previews in `MainWindow._merge_live_preview` and participates in completion decisions. Data2G connection setup currently replaces its receiver object.

## 1. Introduce explicit backend selection and settings ownership

Implement this as the first independently reviewable change.

- Add a backend selector: **Data2G** by default; **Experimental modems** only after enabling experimental support. Keep backend selection separate from the selected transmit mode.
- Mark every local modem experimental, including Resilient 100/50/25 and legacy modes. Do not silently fall back to a local modem when Data2G is unavailable: show connection/setup state and disable transmission until ready.
- Default new installations to the managed Data2G host; preserve explicitly configured remote endpoints and port overrides.
- Migrate existing settings without losing the old local-mode choice. Remember it for experimental use, explain the new default once, and retain each backend's last selection separately.
- Show Pixel QSO's direct CAT controls only when **Experimental modems** is selected and the chosen adapter requires application-owned radio control. Include: **“Used only by experimental local modems. Data2G modems control the radio through their host.”** Hide or disable these controls when Data2G is selected, and do not let CAT auto-connect run in that state.
- Put managed-host audio and radio/PTT settings in a separate **Data2G host settings** section. These remain available before starting the managed host; remote-host radio configuration belongs to that host. Do not hide the managed host's required rigctld endpoint along with local CAT controls. Name ownership clearly so the managed host's rigctld endpoint cannot be confused with Pixel QSO's direct CAT connection.
- Show local audio placement, local receive-all, and local waterfall controls only when supported by the selected adapter. Explain Data2G receive behavior rather than implying its receive mode follows the transmit selector.

Acceptance: fresh startup, migrated local selection, managed/remote startup, failed connection, and backend switching all produce an unambiguous UI. Switching releases the previous audio/PTT owner before starting the next; active TX prevents an unsafe handoff.

## 2. Expose the supported Data2G mode catalog dynamically

Today the UI exposes only two selected presets, not every usable Data2G mode.
The host's `MODES` response is broader, but Pixel QSO filters it to Robust/Fast
families. This phase replaces those assumptions with capability-based discovery.

- Replace the two fixed Data2G registry entries with dynamically discovered entries keyed by exact host mode name and backend ID.
- List every advertised mode usable for application broadcast under the connected host's capabilities and bandwidth cap. Do not filter by QPSK/16-QAM name substrings. Validate broadcast applicability against the supported host API; do not assume an ACK/control-only mode is usable solely because its name appears in a catalog.
- Show exact mode name, bandwidth, and estimated card airtime. Robust/Fast may remain optional shortcuts, but must not replace the complete list or claim unmeasured reliability rankings.
- Preserve an explicitly selected supported mode. When unavailable, explain why and choose an available documented default without switching backend. Treat cached mode lists as stale until discovery succeeds.
- Centralize application-frame sizing for TX and airtime estimates. Start with a 256-byte preferred maximum, reduce it for smaller modes, and account for our header and the Data2G length prefix. If no image byte fits, disable that mode with a reason.
- Derive any further burst limits from the supported host contract; capacity calculations alone do not prove that the host will accept a mode/frame combination. Report `BCAST MODE` refusal accurately.
- Keep receive discovery distinct from transmit selection: Data2G determines which modes it receives.

Acceptance: fixture catalogs with additional modulation families, narrow profiles, insufficient capacity, changed names, and disappeared modes. Verify generated frames fit their selected mode and estimates use the same framing policy. Exercise catalog discovery and mode selection against a real host without RF transmission.

## 3. Define a backend compatibility contract

Introduce a small adapter interface rather than forcing every backend to implement audio encode/decode methods.

Common operations: discover capabilities/modes, connect/start reception, submit a card or frame sequence, request stop, and disconnect. Report explicit lifecycle and error events. A stop request must distinguish cancelling queued work from stopping an in-flight transmission.

Capabilities include radio/audio ownership, application-frame limits, frame transport versus image-block decoding, provisional-pixel availability, spectrum/audio availability, and transmit-status guarantees.

Two adapter forms feed the same application services:

1. **Frame transport adapter:** Data2G first, with room for other documented KISS or packet APIs. Receives integrity-checked application frames and passes them to the card protocol parser.
2. **Local image modem adapter:** wraps current waveform encoders/decoders and maps decoded blocks or candidate pixels into assembly events. Optional provisional output is allowed; it is not required of every modem.

Events carry identity, location/range, payload representation, evidence provenance, and verification scope. Distinguish candidate pixels, CRC-checked blocks/transport fragments, verified complete images, queued TX, local TX completion, and remote receipt. Avoid a single ambiguous `checked` boolean.

Compatibility means adapting a backend with a known framing/integrity contract. It cannot automatically make an arbitrary waveform or incompatible remote application understand Pixel QSO cards. Initially use an internal adapter registry; defer external plugin loading until there is a concrete consumer.

The initial registry should include the Data2G host transport adapter and one
experimental adapter for Pixel QSO's existing local waveform path. Additional
experimental adapters can be added independently if they meet the same card
framing, lifecycle, ownership, and integrity-reporting contract; the exchange
controller should not need modem-specific branches to support them.

Acceptance: adapters contain backend-specific knowledge; the exchange controller does not branch on mode-name prefixes. Unsupported features are represented as capabilities, and failures release ownership reliably.

## 4. Move assembly and verification into a shared service

- Extract preview accumulation and completion decisions from `MainWindow` into a Qt-independent assembly service. Have the UI consume immutable snapshots rather than mutable decoder state.
- Preserve bounded in-memory caching, switching between recently heard images, out-of-order delivery, duplicate tolerance, and refinement across later CQ/exchange stages. Disk persistence is not required.
- Retain assemblies across transport reconnects within the application session unless explicitly cleared. Do not reset the cache simply because a new Data2G connection discovers modes.
- Separate candidate pixels, verified regions, and whole-image verification. Checked Data2G fragments and checked local blocks remain authoritative; candidate data cannot overwrite them. A partial card is not wholly untrusted just because its final CRC is pending.
- Preserve rejection of conflicting verified overlaps. Validate a complete incoming fragment before mutating an assembly so rejection cannot leave a partly applied fragment.
- Use stable image identity, dimensions, palette/encoding, protocol version, and sender identity where available. Do not include CQ/exchange stage. Isolate legacy identities from newer content-addressed images and avoid inventing sender identity not supplied by the transport/header.
- The service emits completion only under the relevant protocol's verification rule. A UI progress percentage or union of pixel coverage must never promote a card on its own.

Acceptance: interleaved images, later CQ refinement, duplicates, changed stages, reused numeric card IDs, conflicting verified bytes, cache eviction, reconnect, and candidate-to-verified replacement. Noise/candidate-only input must not trigger a verified card, QSO action, or automatic reply.

## 5. Strengthen the experimental local wire format

- Version a new local format carrying a whole-raster checksum/content identity in protected metadata. Define the canonical raster, dimensions, and palette interpretation that the identity covers.
- Keep independently checked blocks with explicit positioning. Validate each block before accepting it as a verified region; retain failed-block candidates solely for preview.
- Require all required data and the final raster checksum before marking a new-format card verified complete.
- Support legacy reception through a separate adapter/protocol path with its actual verification scope. Do not claim legacy cards passed a whole-image checksum they never transmitted, or combine legacy blocks into new-format assemblies.
- Document old/new interoperability and any TX choice. No silent on-air wire-format replacement.

Acceptance: two image revisions with reused card IDs cannot form a hybrid accepted card; mixed blocks fail final verification; clean round trips and damaged/repeated-block recovery work. Measure added metadata airtime, acquisition, and RF recovery separately from architectural correctness.

## 6. Connect the UI and exchange controller to the shared services

- Render candidates in the compact preview and report received versus verified coverage explicitly. Promote to the wall only on the assembler's verified-complete event.
- Preserve latest-image switching and reuse cached progress when an earlier image returns.
- Remove UI checksum/acceptance logic and duplicated assembly caches after adapter parity is demonstrated.
- Preserve existing card library, export, QSO logging, and quick-draw flows. Drive automatic exchange from verified messages and explicit TX events.
- Retain existing no-replay behavior after an uncertain Data2G transmission. ACKMODE remains local host completion, not remote receipt.
- Surface host failure causes and capability gaps, including unavailable PTT support, instead of reporting them as generic modem failures.

Acceptance: Qt UI tests cover backend switching, connection states, partial/verified rendering, controls appropriate to each adapter, and completion-triggered actions. Also inspect the rendered settings and preview layouts.

## 7. Validate and document the Data2G-first release

- Add API contract fixtures for discovery, mode refusal, frame delivery/loss, TX status, disconnect, and reconnect.
- Verify managed host packaging includes the intended audio/PTT capabilities on supported platforms. An importable runtime alone is not proof of radio control.
- Exercise two isolated app/host instances with distinct ports and controlled audio before an operator-run RF test.
- Compare existing and refactored assembly behavior using the same captured frames and local recordings. Then measure live card completion and airtime; architecture changes alone are not a sensitivity improvement.
- Update README and settings help around Data2G setup, exact mode names, experimental adapters, integrity levels, and wire-version compatibility.

## Suggested delivery sequence

1. **Backend choice and settings ownership:** add the Data2G / Experimental
   modems selector; default new installs to managed Data2G; migrate the saved
   local mode into the experimental selection; hide local modes until the
   experimental toggle is enabled; make CAT and local audio controls conditional;
   separate managed-host rig/PTT settings; make connection failure visibly
   unavailable rather than silently switching to local modulation.
2. **Data2G capabilities and modes:** enumerate all broadcast-usable discovered
   modes; model frame capacity and bandwidth; derive a safe fragment size and
   airtime estimate; preserve a valid selection or explain why it disappeared.
3. **Adapter boundary:** define lifecycle, capabilities, transport/integrity
   events, and TX status; wrap Data2G and current local modem paths; move backend
   branching out of QSO control and mode-name checks.
4. **Shared assembly and verification:** consolidate fragment/block assembly,
   provisional preview, image identity, and whole-card acceptance into one
   service retained across reconnects.
5. **Experimental local wire format:** add versioned whole-image identity and
   checksum while preserving explicit legacy reception and interoperability.
6. **Product and release validation:** finish settings/help and README, exercise
   managed and remote hosts, two isolated app instances, captured-frame parity,
   and controlled audio before operator RF testing.

Keep RLE, new FEC schemes, adaptive retransmission, and arbitrary plugin discovery out of this refactor. They can be evaluated independently once the shared interfaces and integrity rules are stable.

## Implementation progress

- **Complete for phase 1, pending runtime verification:** the UI now has a separate Data2G / Experimental modems selector;
  Data2G and managed-host startup are the new-install defaults; local modem modes
  are classified as experimental; mode choices are stored per backend; the
  managed host has separate audio and rigctld settings; direct CAT and
  experimental audio controls are hidden or disabled on the Data2G backend.
  An unrecognized saved backend value is normalized to Data2G on startup, so
  malformed settings cannot persist a stale backend selection.
  A legacy saved local mode is retained in the separate experimental-mode
  setting, and Data2G-default upgrades receive a one-time notice explaining
  how to select it.
  A saved local audio-only test preference no longer changes the Data2G TX
  label, beacon controls, or status banner, so it cannot imply that host PTT is
  disabled. During Data2G shutdown, backend/mode switching and host settings
  remain locked until the session close callback completes, preventing a rapid
  switch back from reusing a session that is already closing. The Receive panel
  now distinguishes Data2G host reception, local audio reception, and the
  Data2G-to-local handoff state. Backend selection stays locked while a managed
  Data2G host is starting, and its delayed connection step checks that the app
  still targets Data2G before proceeding. Once connection discovery starts,
  backend selection and the option that reveals Experimental remain available;
  choosing Experimental closes the pending session before CAT/audio resume and
  is reported as a handoff rather than a host failure. Modulation choices remain
  locked until discovery or handoff finishes, and backend switching remains
  locked during active transmission. The Connect control and handler also
  reject duplicate connect requests until managed-host startup completes.
  If discovery completes at the same time as a requested handoff, the UI keeps
  Data2G ownership until the close callback confirms release. Switching during
  managed-host startup waits for that process to stop. Both paths resume local
  CAT and audio only after Data2G relinquishes ownership, including when the
  connection attempt fails after the backend switch.
  Station call-sign editing is locked while the Data2G connection is opening,
  active, or closing, so the host group identity cannot diverge from card identity.
  The transport owns one station-call normalizer enforcing the pinned host's
  10-character packed-call and single-token `FROM` requirements; both app
  preflight and direct session/group creation use it.
  All Data2G sends now reject a card call sign that differs from the `FROM`
  identity used to open the broadcast group.
  The transfer summary now explains when Data2G mode discovery is needed and
  shows the host's refusal/capacity reason for an unusable selected mode.
- **Remaining in phase 1:** exercise backend switching and verify ownership
  handoff with both managed and remote hosts. The packaged GUI now visibly
  reports both remote connection refusal and managed-host command-port conflict.
  An offscreen startup smoke check
  confirmed a fresh window selects the Data2G adapter and Data2G mode by default.
  A Qt regression test now opens Station settings and verifies CAT, local audio,
  and receive-all controls hide for Data2G and return for Experimental modems;
  Data2G connection and managed-server controls now sit together in a backend-
  specific group. Offscreen renders of both settings states were inspected;
  this is source-level UI evidence, not an interactive user-session check.
  The host command port is now checked with its adjacent data-listener port and
  the KISS port before CAT is handed off, with the listener layout explained in
  Station settings. Malformed saved or environment-provided Data2G integer
  settings fall back to usable defaults instead of aborting GUI startup. Starting
  the managed local host no longer overwrites the separately saved remote-host
  endpoint, and connecting to a remote host persists the Remote source choice
  without requiring a later Station settings save. Managed-host startup now
  reports data-directory or host-log creation failures in Station settings
  instead of allowing the launch callback to raise. `_connect_data2g_host()`
  now rejects its queued startup callback after window shutdown begins. An
  offscreen lifecycle smoke that closed the default-local-host window before
  processing the first Qt event confirmed no host process or log was created.
- **Complete in code for phase 2, with source API fixtures passing and real-host
  verification pending:**
  discover all catalog modes and validate each with `BCAST MODE` on the opened
  Pixel QSO group; show disabled entries with host refusal or insufficient
  capacity reasons; size fragments and airtime estimates to mode capacity.
  The shared frame-capacity helper also rejects a configured preferred frame
  ceiling too small to hold the transfer header plus an image byte, keeping
  mode availability consistent with the TX chunker and airtime estimator.
  The transport now rejects duplicate mode names and catalog records with
  invalid bandwidth, codeword-capacity, or airtime values before mode selection.
  If a saved dynamic mode disappears or becomes unusable, discovery now reports
  the reason and the Data2G fallback mode in the status bar. Replacing a host's
  discovered catalog also removes its vanished dynamic backend entries while
  retaining static pre-discovery fallback entries. A failed discovery clears
  that catalog too, so a disconnected host's old modes cannot remain selected
  for transmission estimates or retries. Adapter discovery now also fails
  closed if the host's per-mode `BCAST MODE` validation results are incomplete
  or malformed. Dynamic host-mode key creation,
  decoding, recognition, and cleanup now share one key-format helper instead
  of duplicating the prefix check in UI code.
- **Phase 3 in progress:** `backend_adapters.py` defines the initial Data2G frame
  transport and experimental local image modem contracts, with explicit radio,
  audio, receive, spectrum, provisional-pixel, TX completion, and integrity
  capabilities. App discovery, mode selection, frame submission, disconnect,
  session creation/state/port access, audio encode, and decode now pass through
  adapters; the exchange controller no longer imports or inspects the concrete
  Data2G session type. Data2G stop reports queued cancellation separately from
  an in-flight frame that may still transmit. The
  UI's remaining transport-versus-local decisions now read adapter capabilities
  and mode-registry ownership instead of downcasting selected backends in QSO
  controls. Data2G's discovered-mode metadata remains supplied by its frame
  transport backend, and card packing/fragment sizing now live in the Data2G
  frame adapter instead of `MainWindow`. The runtime UI and host handoff still
  need direct exercise.
  internal registry accepts compatible adapter implementations. Local input
  acquisition, UDP loopback binding, startup errors, and capture cleanup now
  belong to the experimental adapter; an owning handle is released on stop,
  transmit handoff, and window close. The app no longer constructs or stops
  QAudioSource/QUdpSocket directly. Audio source/start and test-socket setup
  exceptions now become typed receive-start errors after partial resources are
  released. If an adapter reports or raises a capture-stop failure, the app
  clears its local handle references and reports the failure in the exchange
  log rather than unwinding the handoff. Kind-specific transport/local adapter
  protocols are explicit and registry insertion now checks their required
  operations. Qt tests verify local audio acquisition, no-device error display,
  cleanup on transmit handoff, and shared decode-failure presentation. The
  experimental local adapter now returns typed retryable no-match versus
  backend-error outcomes, and both saved/live image decode workers consume that
  contract. Decode workers resolve adapters through each mode's registry entry;
  compatibility tests prove both selected-mode and receive-all decoding use a
  newly registered local adapter. A stale Data2G mode-prefix exception was
  removed from the QSO/UI decode handler, and live receive now distinguishes a
  retryable no-match from a backend fault in the status line. The stateful WAV
  decoder and live packet decoder now return typed adapter outcomes, including retryable misses,
  backend failures, and the legacy END-marker completion rule. Selected legacy
  mode and receive-all fallback enter through the same local adapter; the old
  direct packet parsing branch has been removed from the live worker. Adapter
  tests cover normalized legacy success, no identity, and completion evidence;
  a worker test confirms a complete raster without the END marker is not
  reported as a complete legacy transmission. Remaining: exercise UDP-bind
  failure in the packaged running UI. Local host connection
  failures now distinguish likely audio startup errors and occupied ports by
  inspecting a bounded tail of the host log, while keeping raw log contents out
  of the app UI. The sanitizer also recognizes Data2G's exact “no input/output
  device matching” startup message and reports an audio-device hint. A direct
  source helper invocation with Data2G's emitted “no input device matching”
  message returned that sanitized audio hint. The packaged UI presentation of
  this failure was later verified with the bundled host child below. If the managed local host exits before
  listeners open, its
  sanitized startup hint now appears in the QSO receive status and exchange log
  as well as Station settings. A packaged GUI run with its command port occupied
  confirmed the actionable hint on the main screen. Closing the frozen GUI
  during the local-host retry now exits without the deleted-signal or QRunnable
  exception observed before the worker cancellation fix. A remote closed-port
  failure also appeared in the activity area. The Station settings modal did
  not render in that WM-less Xvfb session. A later source offscreen render of
  the dialog is recorded below; the experimental UDP-bind failure still needs
  direct exercise.
  This turn moved resilient audio-placement capability into mode metadata and
  the backend factory; receive and TX paths no longer branch on the
  `resilient_*` name prefix. All registered local image backends now use the
  same adapter decode route, and the QSO TX fallback is limited to the legacy
  packet backend rather than duplicating avatar wire-format selection.
  Managed-host audio choices now come from PyAudio's PortAudio catalog and
  pass decimal device indexes to Data2G, whose CLI resolves numeric selectors
  directly. This avoids relying on Qt device IDs matching host API names, an
  assumption that happened to hold for the enumerated devices on this Linux
  machine but is not guaranteed across audio backends. Existing saved Qt IDs
  are decoded and matched against the PortAudio catalog where possible, so an
  upgrade retains the selected device instead of silently switching to the
  system default. The system-default choice remains available, and missing
  optional PyAudio enumeration does not prevent connecting to a remote Data2G
  host.
  A stale Data2G audio-device environment or saved selection no longer raises
  during main-window startup or silently starts the managed host on another
  device. Station settings identifies the missing variable or saved device,
  leaves the selection blank, and disables Connect until the operator chooses
  an available device. Offscreen source UI runs confirmed the visible hint,
  that no local host process started, and that choosing System default clears
  the error and re-enables Connect. A stale saved device name remains intact
  until the user chooses a replacement.
  Backend adapter resolution now rejects unknown product backend values
  instead of routing them to the experimental local adapter, preserving the
  Data2G default if persisted or caller-provided selection data is malformed.
  Local audio export/transmission encoding now uses the selected local adapter;
  the Data2G frame-transport backend reports that its host generates waveforms
  and cannot export a Pixel QSO-side WAV. The existing one-shot beacon action
  now queues a card through Data2G at the host-configured frequency, without
  local CAT tuning or creating a contact QSO log; the local frequency picker
  stays available only for Experimental modems. Experimental legacy packet
  audio generation also now dispatches through the selected adapter.
- **Phase 4 in progress:** `CardAssemblyService` now owns both Data2G fragment
  reassembly and cross-transmission preview accumulation, preserves its caches
  across host reconnects, and returns immutable preview snapshots to the UI. It
  applies explicit whole-raster CRC/content-tag and all-checked-block completion rules;
  candidate coverage cannot promote an image; conflicting checked overlaps,
  conflicting raster CRC metadata, and out-of-range transport fragments are
  rejected before assembly or cache recency changes.
  Tests now cover bounded preview eviction,
  refinement after switching to another image and back in a later receive
  window, and transport-fragment retention across a receive-window restart.
  Preview identity now includes protocol version and stable content identity,
  while excluding CQ/exchange stage; tests confirm legacy stage refinement and
  isolation from version 4 content-addressed images.
  A Qt integration test exercises the Data2G receive handler and confirms it
  waits for the service event before wall promotion, saving, or contact handling.
  A manual offscreen app-path smoke on 2026-10-05 then fed two synthetic
  transport fragments through the active-session callback. After fragment one,
  the session wall and QSO log remained empty; after fragment two, the UI
  promoted one card, created one received-card QSO entry and one QSL capture,
  and showed the whole-card CRC status. A repeated frame from a stale session
  was ignored. This verifies the synthetic receive-to-app completion path, not
  interoperability with a real Data2G peer. A second manual app-path smoke on
  2026-10-05 delivered the first half, invoked the actual host-close callback,
  attached a fresh session on a different KISS port, then delivered the second
  half. The partial preview survived disconnect and completed after reconnect,
  producing one session-wall card, QSO receive entry, and QSL capture. This
  closes the running-app cache-retention check with synthetic frames; it does
  not prove real-peer reconnect behavior. A complete automatic responder flow
  against an isolated protocol fixture is recorded below; real-peer exchange
  and reconnect behavior remain open.
  A third offscreen smoke on 2026-10-05 completed a synthetic CQ through the
  same callback with the window shown. It exposed the manual `Reply to W7PXQ`
  action only after whole-card verification, mapped the reply to the exchange
  stage, and left automatic contact progression idle. This verifies CQ reply
  action readiness; the subsequent responder-flow check below verifies the
  automatic path against a protocol fixture.
  A fourth offscreen app smoke on 2026-10-05 armed the automatic responder and
  delivered a synthetic CQ in two fragments. The partial fragment left the
  peer and contact stage untouched; verified completion paired the peer, moved
  to `send_exchange`, recorded the received card, and scheduled
  `transmit_exchange` after the 600 ms guard. This confirms automatic exchange
  gating and progression at the app boundary; no frame was transmitted.
  It now attaches an `assembly_event` with content identity, integrity scope,
  candidate/verified pixel counts, and the authoritative completion decision.
  Live and saved receive flows use that event for wall promotion, logging,
  contact handling, and on-air verified-capture evidence. Data2G completion
  validates both the full-raster CRC32 and transmitted BLAKE2s content tag; the
  service sanitizes incoming completion flags before merging pixel evidence.
  Checked Data2G transport fragments now mark fully received pixels as verified
  regions before whole-raster completion; candidate pixels cannot overwrite
  those regions. A regression test proves partial checked coverage stays
  provisional at the card level and authoritative at the pixel-region level.
  Completed Data2G capture filenames now sanitize remote callsigns and are
  atomically published; QSL write failures are reported without unwinding the
  live frame handler. Active QSO logs now use atomic replacement for received
  and sent cards and completed contacts. QSO log directory creation failures
  are reported in the UI and no longer escape receive/contact callbacks during
  gallery refresh; filesystem failure can still prevent a log from being saved.
- **Phase 6 progress:** the compact receive preview now reports received pixel
  coverage separately from verified-region coverage, while labeling the card
  complete only when the assembly service verifies the whole image. Contact
  handling, QSO updates, session-wall promotion, and duplicate saved-card
  refreshes now use that same completion decision; startup QSL deduplication
  recomputes verification from saved evidence instead of trusting a stored
  `exact` flag. Data2G TX queue state is now committed only after synchronous
  mode selection succeeds, so a stale or overloaded session cannot leave the
  UI stuck in a phantom transmission state.
- **Phase 5 in progress:** experimental Resilient v4 now carries protected
  whole-raster CRC32/content identity and explicit checked-block positions;
  every block carries the image tag so checked blocks from a different raster
  cannot be attached to its header.
  Complete-card verification requires every block and both raster checks;
  legacy version 3 resilient and raw bursts remain decodable with their actual
  integrity scope. Existing Resilient mode keys continue transmitting v3;
  v4 has a separate explicitly selected experimental mode, avoiding a silent
  on-air format change. Tests cover clean v4 raster identity, mixed-raster
  block rejection, damaged-block
  preview, v3 raw and FEC decoding, and content identity across changed numeric
  IDs, repeated v3/v4 cycles, and mixed-copy block rejection. The 32×32
  eight-color symbol count is measured at 1,236 tones for v3 and 1,428 for v4.
  Remaining: capture acquisition metrics and validate against real audio and
  RF.
- **Phase 7 in progress:** transport tests exercise the supported command and
  KISS APIs, mode discovery without an `OK` terminator, host mode refusal, lost
  connections, reconnect without replay, ACKMODE/frame delivery, and status
  events, including a command-session fixture that validates every advertised
  mode and keeps the session alive when one broadcast mode is refused. A
  `BCAST OPEN` response is now checked against the host's supported KISS
  broadcast port range (1–15) before the session accepts the port; `BCAST MODE`
  mode names must be a single command token. A
  BUSY-aware ACK wait now allows the pinned host's default 60-second continuous
  channel wait, scales its clear-channel wait to the selected mode's maximum
  airtime, and keeps a five-minute watchdog for a stuck BUSY state. The UI
  reports when the host is holding a queued frame. A simultaneous two-session
  fixture uses distinct command/KISS ports and
  verifies that each client's ACK and received frame stay with its own host.
  Connected host status now explicitly says audio/PTT readiness is unverified,
  including the receive-only case when no broadcast mode fits.
  The managed-host log monitor surfaces recognized rigctld PTT-on and PTT-off
  failures in the app without copying raw log lines or endpoint details into
  the UI. A managed-host PTT fault now blocks further Data2G transmissions and
  stops automatic exchange; the fault persists across app restarts and host
  reconnects until the operator explicitly confirms the radio is unkeyed before
  the next transmission or CAT handoff. Automatic CAT reconnect stays
  suppressed while the fault is unresolved. Fault and recovery writes are
  synchronized immediately to settings. Explicit recovery also resets the
  log monitor's deduplication state so a later same-class PTT failure relatches
  the interlock and refreshes the receive status hint after the operator clears
  it. Automatic exchange startup refuses an unusable or undiscovered Data2G
  mode before arming receive/transmit state, and the Start control reflects
  host/mode readiness while retaining access to the PTT recovery prompt. The
  CAT connection operation itself now rejects connection attempts while a
  Data2G session is active or taking ownership, even if invoked outside the UI
  controls. An unexpected host close now stops automatic exchange even if it
  was waiting for an incoming card with no frame queue active. Host disconnect
  status retains the last failure cause until the
  operator retries connection, and stale discovery, frame, ACK, status, error,
  or close callbacks from an older session cannot mutate a newer connection.
  A close during mode discovery is retained until its discovery result arrives,
  preserving the actionable connection failure instead of dropping it as stale.
  A KISS socket send failure now tears down
  the failed host connection;
  host-reported send errors end the Pixel QSO frame queue
  immediately, identifies any in-flight frame as uncertain, and prevents
  automatic exchange from advancing on an incomplete transmission. The KISS
  reader rejects malformed ACKMODE acknowledgements and preserves the specific
  connection failure reason so the active send can report it without waiting
  for its ACK timeout; the MODES catalog parser also rejects malformed numeric
  values, duplicate names, and command-unsafe mode tokens before discovery can
  proceed. Command responses now have 16 KiB line and 256-line queue limits
  (4 MiB maximum payload); this leaves room for the pinned host's 48-mode
  catalog. An overlong or flooded response fails closed instead of growing
  memory without limit. Pending commands surface the recorded failure
  immediately even when a full response queue cannot accept its shutdown
  sentinel.
  Fatal session errors are surfaced once before shutdown.
  Session shutdown now bounds both control-queue insertion and thread join by
  its timeout, including when close is requested from the session worker.
  `pixelqso.spec` now explicitly collects the Data2G and PyAudio package trees
  and binaries. The Linux frozen host's
  `--run-data2g-host --list-audio-devices` command successfully enumerated
  PyAudio devices on 2026-10-05; ALSA/JACK emitted missing-device/server
  warnings in this container, but enumeration completed with exit code 0.
  Enumeration alone does not prove that a selected device can be opened or
  streamed through, or that PTT works. A local loopback smoke on 2026-10-05
  then launched the packaged host with ALSA's discard-only `null` PCM for both
  input and output and rigctld PTT disabled. Pixel QSO's `Data2GSession`
  connected on ephemeral localhost ports, discovered 20 modes, and got host
  acceptance for a 200 Hz mode. It sent a KISS ACKMODE frame and received the
  matching port/tag acknowledgement. This exercises packaged audio stream
  startup and the command/KISS transmit path without acoustic output or radio
  transmission;
  it does not establish operation through an actual device or RF reception.
  The host settings now clarify that command/KISS connectivity does not prove
  host audio or PTT operation; remote host readiness must be checked there.
  The exchange activity document retains at most 2,000 entries so long-running
  host status reporting cannot grow the UI log without limit.
  Assembly and Qt tests exercise Data2G completion gating.
  Opt-in on-air recording now saves each received Data2G application frame as a
  local `.bin` sidecar with byte count and SHA-256 in its event record; a remote
  collector receives only the event metadata and frame hash. No frame captures
  are present in this checkout yet. `tools/replay_data2g_capture.py` checks
  sidecar hashes, compares direct fragment assembly with the shared assembly
  service, and compares replayed card references with the references recorded
  by the app. The recorder writes a final summary only after queued capture jobs
  drain; post-close events stay outside the capture window. The replay command
  reports incomplete sessions, recorder drops, missing references, or write
  failures as inconclusive; real-host replay parity still requires a captured
  session.
  `.github/workflows/build-desktop.yml` now runs host help, mode-catalog, and
  PyAudio device-enumeration checks on Linux, Windows, and macOS bundles, and
  push triggers now include the active `d2g_first` branch. A
  fresh PyInstaller build from the current
  worktree completed on Linux on 2026-10-05 and produced `dist/PixelQSO`
  (141 MB). That executable passed
  `--run-data2g-host --help` and `--run-data2g-host --list-modes --kiss-bw 500`
  with exit code 0. Host options require the `--run-data2g-host` prefix;
  passing `--list-modes` directly to Pixel QSO starts the GUI instead. The same
  executable also passed `--run-data2g-host --list-audio-devices` with exit
  code 0 and listed 34 devices; ALSA/JACK emitted 29 diagnostics in this host
  environment. This covers PyAudio initialization and enumeration, not opening
  a selected physical device or PTT. Cross-platform CI results remain pending.
  The packaged GUI also started on 2026-10-05
  under Xvfb with an isolated data directory and a deliberately closed remote
  host port; Data2G remained the selected backend and the refused connection
  appeared in the exchange activity area. The Station settings modal did not
  render under this Xvfb session without a window manager. A separate packaged GUI run held its
  managed-host command port open; the bundled host failed before audio opened,
  the sanitized occupied-port hint appeared on the main screen, and the app
  exited cleanly. Closing a packaged GUI during that connection retry also
  returned exit code 0 without an unhandled worker-signal error. The current
  session transport now interrupts its managed-host startup retry as soon as
  shutdown begins. A direct runtime check closed a session retrying a refused
  local port in under 1 ms and joined its startup worker immediately; this
  removes the prior six-second wait when the window closes during discovery.
  After the final recorder-drain summary change, a fresh PyInstaller build from
  the current worktree completed on Linux with Python 3.13.6 and PyInstaller
  6.22.3, producing `dist/PixelQSO` (141,337,880 bytes). On 2026-10-05 that
  exact executable passed `--run-data2g-host --help`, `--list-modes --kiss-bw
  500`, and `--list-audio-devices`, each with exit code 0. The mode command
  listed 20 modes; device enumeration completed despite ALSA/JACK diagnostics
  in this environment. The build console warned that `scipy.special._cdflib`
  was not found, but the generated PyInstaller warning report does not list
  it. The build environment's SciPy 1.18.1 also has no importable
  `scipy.special._cdflib` module (`find_spec` returns `None`), so the console
  warning is for an absent optional/module name rather than a missing installed
  SciPy component. This closes the current Linux host CLI smoke gate; selected physical
  device streaming, Windows/macOS bundle success, and the remaining hardware
  gates are still open.
  After changing managed-host audio selectors to PyAudio device indexes and
  adding saved-Qt-ID migration, a fresh Linux build completed on 2026-10-05.
  That executable passed host `--help`, mode listing (20 modes), and audio-device
  enumeration (33 listed devices) with exit code 0. The packaged GUI initialized
  under Xvfb with an isolated data directory and remained running until the
  10-second smoke timeout; PortAudio emitted ALSA/JACK diagnostics in this
  container. A source offscreen render of the Station settings dialog was then
  inspected on 2026-10-05. It showed the Data2G host section, System default
  choices, and the PortAudio catalog (19 input / 31 output devices), with
  selected items carrying the matching decimal host indexes. The bundled
  packaged dialog itself remains unverified; physical audio path is not
  established by enumeration.
  A packaged run then selected the PortAudio `pulse` input and output devices
  through the new settings path. The managed host opened its command, data, and
  KISS listeners, opened the `PIXELQSO` broadcast group, accepted 49
  `BCAST MODE` checks, and closed the group cleanly when the app exited. The
  pinned host opens its PyAudio input/output streams before logging the listener
  startup, so this verifies PortAudio index selection, virtual PulseAudio stream
  startup, packaged Data2G discovery, and orderly disconnect without RF. It
  does not verify a physical radio audio interface, PTT, or reception.
  The latest frozen build also passed host `--help` and 500 Hz mode listing
  (20 modes). With a removed Data2G input device seeded in `settings.ini`, the
  packaged GUI remained running through an eight-second Xvfb smoke window,
  created no `data2g-host.log`, and preserved the removed device name and ID.
  This confirms it did not launch with an unintended fallback device. The
  source offscreen Station dialog visibly presents the unavailable-device hint;
  choosing System default clears it and enables Connect. The packaged dialog
  has not yet been captured visually.
  A source `MainWindow` then launched the bundled `dist/PixelQSO
  --run-data2g-host` child with an input selector changed to invalid PortAudio
  index 999 after catalog enumeration. The packaged host exited before opening
  audio streams, and the UI showed the sanitized audio-startup hint in host
  status, receive status, and exchange activity. The child was reaped and no
  host process remained. This exercises the frozen host's selected-device
  failure and app presentation together; it does not verify the frozen parent
  GUI's dialog rendering.
  During an unintended app-to-host smoke on 2026-10-05, the app's queued
  startup callback ran before the script replaced its default ports and
  connected to an existing managed host. The app submitted one frame but
  received no ACK before shutdown; the real transmission outcome is unknown.
  The isolated managed host was then stopped with SIGTERM through its graceful
  shutdown handler; its process exited and a read-only rigctld query returned
  PTT state 0. This attempt is not counted as transmit validation. It exposed
  the closed-window callback race fixed above. A later controlled app
  transport smoke used an isolated fixture and did not contact the configured
  radio host.
  After suppressing the queued startup callback during fixture setup, a
  controlled source-app transport smoke on 2026-10-05 connected to an isolated
  command/KISS protocol fixture on dynamically assigned localhost ports. The
  app discovered two modes, opened `PIXELQSO`, selected a mode, generated six
  234/256-byte Pixel QSO application frames, and received six unique ACKMODE
  acknowledgements on broadcast port 7. The UI completed the queue and stated
  that local ACKs do not confirm remote card receipt. This verifies the app's
  Data2G mode-selection, frame submission, queue, and ACK path against a
  protocol fixture only; it does not exercise the pinned host, audio waveform,
  PTT, or RF.
  A separate source-app smoke then connected to the installed pinned Data2G
  `serve()` implementation with an in-memory PortAudio shim: capture supplied
  paced zero samples and playback counted generated float audio without opening
  an OS audio device. The host advertised all 48 modes, opened the group, and
  transmitted three repeated CQ application frames; the app received three
  ACKMODE acknowledgements and completed its queue. The sink observed
  1,093,809 nonzero samples at peak 1.0. The host rigctld port was 0, disabling
  PTT. This verifies Pixel QSO's supported host API and the pinned waveform
  generation/queue path against a software sink, not a real PortAudio stream,
  radio, or RF.
  A source-app automatic responder smoke on 2026-10-05 then connected through
  `Data2GSession` to an isolated command/KISS fixture. After an injected
  verified CQ, the app sent its exchange and reached `await_report73`. After an
  injected verified 73 with SNR, it sent final 73, completed the contact, and
  persisted a `Complete` QSO log with sent stages `exchange`/`73` and received
  stages `cq`/`73`. Four application frames received four ACKMODE acknowledgements.
  This verifies the complete app responder state machine and persistence with
  synthetic peer frames and fixture acknowledgements; it does not verify a
  remote peer or RF.
  A caller-role smoke against the same isolated fixture also completed on
  2026-10-05. `start_auto_exchange()` sent CQ, a verified synthetic exchange
  advanced the app to `send_report73`, the report send reached
  `await_final73`, and a verified final 73 completed the contact. The persisted
  QSO log was `Complete` with sent stages `cq`/`73` and received stages
  `exchange`/`73`; all four application frames were ACKed. This verifies both
  automatic roles at the app/API boundary with synthetic peer cards, not a
  real Data2G peer or RF.
  Windows/macOS bundle success, operation through selected physical audio
  devices, actual PTT, independent two-host audio operation, and RF reception
  remain release gates.
