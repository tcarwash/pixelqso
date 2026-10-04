# Data2G host integration and mode cleanup plan

Status: in progress. Mode policy, a versioned transfer protocol, the managed
command/KISS session, host mode discovery, manual and automatic host-backed TX/RX,
on-air event reporting, and removal of in-process Data2G modem calls are
implemented. The pinned Data2G host runtime is a standard dependency, with
Torch kept out of the runtime install. Synthetic Qt, protocol, and weak-signal
compatibility tests pass. Isolated native and pinned Python two-host KISS/card
loopbacks pass; standard and weak-signal Linux frozen builds pass their
bundled-host checks, and the packaged UI connects to the managed host. Live
radio/on-air testing remains. The operator's Data2G GUI is
in use, so its single-client ARQ
command and data ports must not be probed by opening a replacement command
client that could interrupt that session.

## Target behavior

PixelQSO uses an independently installed Data2G host through its supported command
and KISS TCP interfaces. Data2G handles its modem, sound devices, channel access,
and PTT. PixelQSO handles cards, image assembly, previews, repeats, and QSO state.

The normal transmission menu contains Data2G modes plus our 100-baud-and-below
Resilient modes. Our higher-speed Resilient, Fast, and Weak signal modes move
behind one Experimental option.

| Existing mode | Planned visibility | Execution |
| --- | --- | --- |
| Data2G Robust / Fast | Normal; selectable when a compatible host is connected | External Data2G host |
| Resilient 50 baud | Normal | Local PixelQSO modem |
| Resilient 25 baud | Normal | Local PixelQSO modem |
| Resilient 100 baud | Normal | Local PixelQSO modem |
| Resilient (`fast_avatar_fec`, 400 baud) | Experimental | Local PixelQSO modem |
| Fast (`fast_avatar`, unverified) | Experimental | Local PixelQSO modem |
| Weak signal, single and combined copies | Experimental | Optional local experimental modem |
| Legacy packet / MFSK modes | Experimental | Local PixelQSO modem |

Prefer the host's robust broadcast mode as the fresh-install default when a
compatible host is available. With no host configured, default to Resilient
100 baud. Preserve an explicitly selected normal mode. If a saved experimental
mode is hidden, migrate to a normal mode and explain the change once.

The confirmed cutoff is `<=100 baud`: Resilient 100, 50, and 25 baud are normal modes.

## Verified upstream baseline

GitHub's live API reports [Data2G PR #37](https://github.com/arodland/Data2G/pull/37)
merged at `2026-10-04T00:45:48Z`, merge commit
`77856c901dd8db2642d72fcf869c727918429e4f`. The cached PR page can still show Draft;
use the live API and the merged source when implementing.

The [broadcast specification at that commit](https://github.com/arodland/Data2G/blob/77856c901dd8db2642d72fcf869c727918429e4f/docs/broadcast.md)
defines `MODES`, `BCAST OPEN/CLOSE/MODE`, KISS ports, and ACKMODE. The
[host implementation](https://github.com/arodland/Data2G/blob/77856c901dd8db2642d72fcf869c727918429e4f/data2g/host.py)
and [KISS implementation](https://github.com/arodland/Data2G/blob/77856c901dd8db2642d72fcf869c727918429e4f/data2g/tnc.py)
are the reference for command framing and TCP behavior.

Important boundaries:

- ACKMODE confirms completion of local transmission, not reception by another station.
- The host receives all supported modes for an open group; transmit mode does not
  select a receive filter.
- `HEARD`, `LOST`, `MISSED`, and `DROPPED` are broadcast status notifications.
  `MISSED` can refer to another group's burst and is not proof of a lost PixelQSO card.
- Broadcast incremental redundancy and a promiscuous-listening command are not
  implemented by this PR. Do not claim soft combining across Data2G broadcasts.
- Named groups route traffic. Their CRC masks do not encrypt or obscure payloads.
- This PR changes KISS broadcasts to wire version 2. Our existing direct Data2G
  waveform adapter is not evidence of host/KISS interoperability.

## Step 1 — Centralize mode policy

Progress: shared visibility/default helpers drive the desktop picker and
automatic receive scan. Experimental modes are hidden unless enabled; hidden
saved selections move to Resilient 100 with a one-time status message. Data2G
Robust/Fast presets are selected from the host's compatible QPSK/16-QAM modes,
preferring 1.2-kHz profiles and adapting to narrower bandwidth caps. Modes that
cannot carry a maximum-size PixelQSO fragment are excluded. The host's supported
preset names are cached, and explicit normal mode choices are preserved.
Resilient 100/50/25 remain normal choices. `MODE_REGISTRY` now defines each
mode key, dynamic display label, experimental visibility, execution path, and
availability requirement. Desktop selection, receive-all filtering, benchmark /
replay mode enumeration, and saved-mode migration use the shared registry policy.
The companion API accepts no mode override; it transmits only the desktop
selection, so it cannot bypass experimental visibility.

The Station setting is now **Show experimental modem modes** and controls every
experimental mode in the table. Experimental modes remain installed and testable
where their optional dependencies are present, but are hidden from ordinary mode
selection and receive-all processing by default.

Files: `card_backends.py`, mode selection in `app.py`, and mode lists in the
benchmark and on-air replay tools. The companion API has no mode override route.

Acceptance: Experimental off exposes only host Data2G choices and Resilient 100/50/25;
turning it on restores the old modes. Restarting with a hidden saved mode produces
a valid normal selection. API requests cannot bypass the same policy silently.

## Step 2 — Add a supported Data2G transport client

Progress: `data2g_transport.py` provides a managed background session, host
`VERSION`/`MODES` discovery, shared-group open/close, dynamic ports, mode commands,
ACKMODE sends, frame delivery, asynchronous statuses, disconnect detection, and
a bounded work queue. It opens the group with the configured station callsign
using `FROM`, and protocol fakes cover KISS escaping, split frames, dynamic ports,
sends, ACKs, HEARD/LOST/MISSED/DROPPED statuses, queue overflow, reader timeouts,
disconnects, refusal, and session ownership. A closed `Data2GSession` can be
restarted with a fresh queue; tests confirm uncertain frames are not replayed.
The operator's active host is left undisturbed because its ARQ command port
replaces an existing client;
the separate native-host loopback below verifies the supported API instead.

The current upstream checkout is at `20bdb05` (a descendant of merged PR #37).
Against its built native `data2g-host`, the new
`tools/test_data2g_native_hosts.py` integration test launches two isolated
hosts on temporary ports, connects them with paced float32 audio pipes, opens
`PIXELQSO FROM` groups, and verifies ACKMODE, HEARD status, and a CRC-exact
PixelQSO card transfer. This exercises the native host/modem path without
touching the operator's active Data2G instance or claiming RF validation. It
also exposed and fixed a transport bug: the TCP connect timeout had remained
active on reader sockets, incorrectly disconnecting otherwise healthy idle
sessions after three seconds.

The managed local startup path now retries only `ECONNREFUSED` responses for
up to six seconds while Data2G opens its audio backend and binds its listeners.
Remote-host connections remain fail-fast. A regression test simulates delayed
listeners so an ordinary startup race does not tear down the managed server.

Add a module such as `data2g_transport.py` with two asynchronous connections:
CR-terminated command lines and a binary KISS stream. Make host, command port,
and KISS port configurable; use upstream defaults only after checking the pinned
host version. Keep connection work and callbacks off the Qt UI thread.

On connect, query `MODES`, parse the complete response, and open a shared public
application group such as `PIXELQSO` with `BCAST OPEN`. Use `FROM <callsign>` when
the selected mode supports it. Retain the callsign in every PixelQSO card payload
regardless. Apply the returned port number; never assume that it is always 1.
Set transmit mode with `BCAST MODE n mode` and handle `WRONG` explicitly.

Discover mode names, bandwidth, codeword capacity, and airtime from `MODES`.
Offer robust and fast presets based on the available compatible modes rather
than importing Data2G's Python submode tables. Keep the host's bandwidth cap and
broadcast control restrictions authoritative.

Implement KISS escaping, port nibbles, incremental parsing, ACKMODE's two-byte
tags, interleaved command statuses, reconnects, bounded queues, and timeouts.
Do not automatically replay outstanding frames after reconnect: their actual
transmission status may be unknown.

Acceptance: a protocol fake covers split/coalesced reads, escaped bytes,
non-default ports, refused modes, disconnects, missing ACKs, and loss/drop statuses.
The pinned Python host and native host both pass command and KISS interoperability tests.

## Step 3 — Separate card framing from waveform generation

Progress: `card_transfer.py` now defines a versioned, byte-offset fragment header
and content-CRC assembly independent of Data2G and DSP. Assemblies retain progress
by sender/card/content identity, accept variable-sized out-of-order fragments, and
reject conflicting overlap. It reports per-pixel provisional coverage and only
marks a raster exact after full-image CRC32 verification. Tests cover duplicate,
interleaved, resumed, corrupted, and variable-chunk delivery. The old `PQD2`
waveform/application path has been removed; the replacement host path uses the
new versioned KISS application frames. Frames include a 64-bit content identity
plus whole-raster CRC32.
The compact RX preview has a progress bar below the card; it shows provisional
pixel coverage and changes to “CRC verified” at full integrity.

Extract the existing card serialization/reassembly responsibilities from
`Data2GBackend` in `card_backends.py` into a transport-independent card protocol.
Keep callsign/grid, message stage, dimensions, palette, card identity, fragment
index/count, duplicate suppression, and whole-image integrity checks in PixelQSO.

Define a versioned KISS frame payload with a magic/application version and an
image-content identity. Make fragment sizes fit the actual host's accepted frame
limits; `MODES` bytes-per-codeword alone is not a complete KISS frame-size contract.
Test host overhead and limits rather than assuming one application fragment must
always equal one PHY codeword.

Cache assemblies by image content and sender. A later CQ with the same image can
refine its existing assembly even when a transmission/session identifier changes.
A different image switches the compact RX preview to the newest assembly; returning
to an earlier image resumes its retained progress. Keep incomplete cards clearly
provisional and publish a fully accepted card only after complete integrity checks.
The supported KISS API supplies received frames, not arbitrary partial pixels from
failed Data2G codewords: preview granularity is therefore application fragments.

Acceptance: out-of-order, duplicate, missing, corrupted, restarted, and interleaved
image transfers preserve identity and never promote an incomplete card to verified.
Decide explicitly whether old `PQD2` application frames need read compatibility;
that does not make the old PHY compatible with the new host's broadcasts.

## Step 4 — Integrate host operation into the app

Progress: Station settings now configure host address and ports and start/stop
mode discovery. The mode picker offers host presets only after discovery (or from
a prior host catalog), and Data2G selection releases PixelQSO CAT and local audio
ownership. Manual TX submits one <=256-byte application frame at a time and waits
for its ACKMODE tag before queuing the next; timeout/stop leaves an in-flight
status explicitly unknown without replay. KISS RX feeds the transfer assembler,
compact preview/progress, QSO log, and verified-card wall. Automatic CQ, exchange,
73, and final-73 sequencing now use host KISS frames, with local ACKMODE advancing
stages and a CRC-verified peer frame driving each reply. Missing host SNR telemetry
is kept unknown; a 73 can omit it without stalling the exchange. A headless Qt
harness passes manual TX/RX and the complete automatic exchange using protocol fakes.
It also runs the HTTP companion API's quick-draw, stage, and transmit routes
through Qt dispatch and the host-backed TX path.
Mode timing estimates account for the number of PHY codewords needed by each
application fragment. Two native hosts and two pinned Python hosts have each
exchanged and CRC-verified a full PixelQSO card over isolated paced audio-pipe
links using the advertised 1.2-kHz robust mode. Radio ownership with the operator's configured devices and live RF/on-air
behavior remain pending. Both local and remote Data2G connection paths now wait
for the CAT socket to reach Unconnected before opening the host session, and a
headless test exercises this handoff. The operator's configured Qt device IDs
were verified against the Data2G/PyAudio device list after fixing a startup
failure caused by passing Qt display labels instead of PortAudio device names.
The packaged-app smoke log confirms the child host opened both listeners,
completed `VERSION`/`MODES`, opened `PIXELQSO FROM AG7SU`, selected the discovered
fast preset, and shut down cleanly when the smoke test ended. The operator
confirmed the connection UI now reports connected. No over-air card transfer
has yet been verified.

Add Data2G connection configuration and clear connected/unsupported/disconnected
status in Options. Discover the host's modes when connecting, and surface useful
bandwidth and timing information in the transmission UI.

Replace the current Data2G `encode/decode PCM` branch with frame submission and
frame/status callbacks. Track queued, transmitted, partial receive, and verified
receive separately. Implement repeats as fresh application-frame submissions;
ACKMODE completes each local send, and an operator reply remains the remote receipt.

When Data2G is active, release PixelQSO's radio audio and CAT/PTT ownership before
the host uses them. Local 100/50/25-baud operation retains PixelQSO's remembered device
settings. Switching execution paths must stop/release the previous owner first.
Use supported host radio controls where available; do not invent a CAT or tuning
command that PR #37 does not provide.

Audit stop/cancel behavior against the real host. Its ARQ `ABORT` command must not
be assumed to cancel broadcast queues. Test port-close/drop behavior, show pending
or unknown status honestly, and document what an already-started burst can do.

Preserve the compact newest-image preview and session wall. Keep the waterfall
for local audio modes. For host Data2G, only display audio/signal diagnostics that
a documented host recording or telemetry interface actually supplies. Do not
retain internal DSP imports merely to populate the old waterfall or SNR fields.

Acceptance: protocol fakes cover manual and automatic host transfer; two real
host-backed clients must still exchange cards. Mode switches cannot produce
simultaneous audio/PTT owners; disconnect and cancel handling leave UI and
transport state consistent. Exercise the real Qt and phone/API paths.

## Step 5 — Remove Data2G code dependencies

Progress: removed imports of `data2g.codes/config/modem/tnc`, the direct PCM
Data2G adapter, and custom clock correction. The pinned `data2g[tnc]` runtime
is now a standard dependency for the managed host process; upstream Torch remains
only in its training extra and is not installed or bundled. The
weak-signal modem now uses an independent encoder/graph and attributed 3GPP table;
its construction digest and deterministic transmitted-bit vector are preserved.
The generated matrix is Python source so it ships in wheels and frozen builds.
SciPy remains a Data2G host runtime requirement and is optional for PixelQSO's
experimental DSP path. The standard frozen app collects Data2G for the managed
child host while excluding Torch, Triton, NVIDIA packages, and optional
weak-signal modules. `PIXELQSO_FREEZE_WEAK_SIGNAL=1` includes the experimental
encoder/table and SciPy. Desktop CI now builds both variants on its Linux,
Windows, and macOS runners; only the Linux variants have been built and checked
locally in this workspace.

Acceptance: focused modem tests pass and deterministic bits match the old
construction. The wheel includes the generated table module and declares the pinned Data2G host runtime but no Torch dependency. Rebuild
and inspect the standard and weak-signal frozen apps with the bundled host. Recursive archive inspection confirms
the standard Linux one-file build includes Data2G/SciPy and PyAudio for managed
hosting. Its frozen host CLI lists modes successfully, and archive inspection
found no Torch, Triton, or NVIDIA packages. Rebuild and inspect the weak-signal
variant on each supported target OS. Native host
interoperability is covered by the two-host loopback described in Step 2; radio
and RF interoperability remain to be checked.

## Step 6 — Update on-air tools and documentation

Progress: the two-window launcher retains the WebSDR loopback flow and requested
AG7SU/CN85 station default. On-air session events now record host mode discovery,
KISS frame hashes, transmit queue tags and ACKMODE completions, host status/error
messages, and received card identity/CRC state without copying raw card frames to
the collector. Session reports correlate host TX starts with CRC-verified host RX
rasters by sender/card/pixel identity, not by event timing. Headless tests check
the event hooks and raster matching. Audio benchmark and replay CLIs now exclude
host modes, which accept KISS frames rather than local PCM. Real WebSDR/host
measurements remain.

Teach `on_air.py`, `tools/on_air_test.py`, and `tools/open_two_clients.py` about
host command/KISS events and application fragment traffic. Log discovered modes,
queued tags, transmission ACKs, loss/drop notices, and verified image hashes.
Preserve the existing local two-window WebSDR workflow for 100/50/25-baud tests.
For Data2G WebSDR tests, route browser audio into the receiving Data2G host and
connect PixelQSO to that host; PixelQSO no longer decodes Data2G WAVs itself.

Use supported upstream recording/replay facilities for Data2G PCM analysis, or
report their absence as a limitation. Keep recorded audio and sensitive connection
configuration out of committed fixtures. UTC event proximity alone must not be
presented as a matched on-air transfer; use sender/image identity and raster hashes.

Update `README.md`, `ON-AIR-TESTING.md`, packaging instructions, and benchmark tools.
Document the external host installation, radio ownership, group name, mode policy,
300-Hz lowest-tone setting for local narrow modes, and version-2 migration.
Data2G audio placement comes from host capabilities/configuration; do not promise
that the local narrow-mode 300-Hz control configures Data2G.

Acceptance: documented fresh-install commands work; the launch script uses AG7SU /
CN85 as requested; reports distinguish synthetic tests, replay, live host operation,
and verified on-air recovery.

## Step 7 — Clean up after the replacement passes

Progress: removed the direct adapter and old install/build instructions, replaced
its detailed stale documentation with a migration pointer, and removed Data2G
host modes from PCM benchmark/replay choices. No benchmark code imports Data2G
internals. Keep useful local modem tests and archived RF evidence.

Preserve the current dirty worktree and existing card/session data. Make the
transport replacement and deletions separate commits so removal is reviewable.
Do not delete the old adapter before the supported replacement passes the tests
below.

## Step 8 — Validate and promote

Run focused protocol and assembly tests, then run
`uv run python tools/test_data2g_native_hosts.py --host-binary /path/to/data2g-host`
against a built native host. Protocol and Qt tests cover KISS queue limits,
repeats, refusal, loss/drop status, disconnects, ACK routing, fresh reconnects
without replay, and mobile API transmission. Radio-owner switching with the
operator's actual devices remains a separate hardware check. Build and launch
the packaged app with the bundled Data2G host subprocess; command/KISS transport
remains the only app-to-host boundary.

Finish with the user's two-window WebSDR/radio path: Data2G robust and fast, then
local Resilient 100, 50, and 25 baud. Record verified raster matches, incomplete attempts,
time to preview/verification, and UI responsiveness. A merged API and passing
synthetic tests do not establish live RF behavior.

Delivery is complete when the normal menu matches the table, Data2G uses the
supported host APIs exclusively, dependency removal is verified, progressive card
behavior and QSO flows work, and live tests establish the intended operation.

## Suggested commit sequence

1. Central mode policy and experimental gating.
2. Command/KISS transport client plus real-host contract tests.
3. Versioned card framing and reassembly independent of DSP.
4. App transport, radio ownership, and preview integration.
5. On-air tooling and real-host validation.
6. Direct-adapter removal and package/build cleanup.
7. Weak-signal LDPC dependency extraction and final import removal.
8. Documentation, packaged-app checks, and live RF acceptance results.
