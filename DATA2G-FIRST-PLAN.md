# Data2G-first Pixel QSO implementation plan

## Objective

Make Pixel QSO a card-exchange application using Data2G's supported host APIs by default. Retain local and other modem implementations behind an explicit experimental backend adapter. Share card assembly, preview, integrity decisions, and QSO handling across adapters.

This is an implementation plan; the changes below are not implemented by this document.

## Current behavior confirmed in the checkout

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
- Show Pixel QSO's direct CAT controls only for an experimental backend that requires application-owned radio control. Include: **“Used only by experimental local modems. Data2G modems control the radio through their host.”**
- Put managed-host audio and radio/PTT settings in a separate **Data2G host settings** section. These remain available before starting the managed host; remote-host radio configuration belongs to that host. Do not hide the managed host's required rigctld endpoint along with local CAT controls.
- Show local audio placement, local receive-all, and local waterfall controls only when supported by the selected adapter. Explain Data2G receive behavior rather than implying its receive mode follows the transmit selector.

Acceptance: fresh startup, migrated local selection, managed/remote startup, failed connection, and backend switching all produce an unambiguous UI. Switching releases the previous audio/PTT owner before starting the next; active TX prevents an unsafe handoff.

## 2. Expose the supported Data2G mode catalog dynamically

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

1. Backend selection, experimental visibility, and CAT/settings ownership.
2. Full Data2G catalog and capacity-aware framing.
3. Adapter contracts with wrappers for current behavior.
4. Shared assembly service and controller/UI migration.
5. Versioned experimental local integrity upgrade.
6. Packaging, integration validation, documentation, and operator RF checks.

Keep RLE, new FEC schemes, adaptive retransmission, and arbitrary plugin discovery out of this refactor. They can be evaluated independently once the shared interfaces and integrity rules are stable.
