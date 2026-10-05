# Data2G broadcast host integration

Pixel QSO uses Data2G's supported broadcast command and KISS interfaces from
the merged [broadcast API](https://github.com/arodland/Data2G/blob/77856c901dd8db2642d72fcf869c727918429e4f/docs/broadcast.md).
The Python host runtime is pinned to Data2G commit
`20bdb05c3cbb24d7d28b613a86841f51c00ba8f9`. Pixel QSO does not import its
modem, waveform, or framing functions into the application process.

## Responsibilities and radio ownership

Data2G owns modem operation, radio audio, channel access, and PTT. Pixel QSO
opens the shared `PIXELQSO` group and owns its card format, image assembly,
preview, persistence, repeats, and QSO stages. When a Data2G mode is selected,
Pixel QSO releases its local CAT and audio receiver before connecting to the
host. Local Resilient modes use Pixel QSO's own radio path.

Pixel QSO can start a managed Data2G child process or connect to a remote host.
Use a dedicated host connection: the host command interface is single-client
for the ARQ command/data service, and a second application can displace an
existing command owner. The default broadcast ports are command TCP 8300 and
KISS TCP 8100; the host's discovered group port is used dynamically.

## Host protocol

At connection, Pixel QSO checks `VERSION`, reads `MODES`, opens
`BCAST OPEN PIXELQSO FROM <callsign>`, and applies the selected transmit mode
with `BCAST MODE`. Mode names, bandwidth, codeword capacity, and airtime are
read from the host catalog. The host remains authoritative about its bandwidth
cap and supported modes.

The command connection carries CR-terminated text commands and asynchronous
host statuses. The KISS connection carries escaped frames and two-byte
ACKMODE tags. Pixel QSO waits for the ACK associated with each application
frame before queuing the next one. An ACK confirms local transmission
completion, not reception by a remote operator. Outstanding frames are not
replayed after a reconnect because their transmit status may be unknown.
The host's KISS broadcast wire protocol is version 2. Pixel QSO's separate
application frame uses the `PQI2` magic with application version 1; these are
independent version fields.

`HEARD`, `LOST`, `MISSED`, and `DROPPED` are host broadcast statuses.
`MISSED` may describe another group's traffic and cannot be treated as proof
that a particular Pixel QSO card was lost. Group names and CRC masks route and
check frames; they do not provide privacy, encryption, or authentication.
Broadcast mode changes do not narrow the host's receive mode set. The host API
does not expose arbitrary partial decoder symbols or intra-burst pixels.

## Pixel QSO card frames

The app-level protocol in [`card_transfer.py`](card_transfer.py) uses the
`PQI2` magic and version 1. Each frame carries card dimensions and palette,
callsign/grid, QSO stage, byte offset and total packed-raster length, a
whole-raster CRC32, and a 64-bit BLAKE2s content tag. Receivers retain
interleaved images by sender and image identity, accept duplicate or
out-of-order fragments, and reject conflicting overlap. Pixel coverage can
update the preview before the raster checks pass; a complete raster must match
both its CRC32 and content tag before the service emits a verified-complete
event.

Pixel QSO prefers application frames up to 256 bytes and lowers that limit for
Data2G modes with less codeword capacity. The limit accounts for the KISS
length prefix and reserves control codewords; the card protocol header is
included in the frame. The app frame size is independent of PHY codeword
boundaries. Native and pinned Python host-to-host tests transfer a full card
through paced audio links and verify its identity and CRC.

The former `PQD2` PCM waveform/application path is intentionally not supported
by the host/KISS receiver. Old waveform captures need the old decoder; saved
card JSON remains independent of that waveform format.

## Experimental local resilient wire versions

The established local Resilient modes continue to transmit version 3 so an
upgrade does not silently change their on-air format. The separate
**Resilient v4 · whole-image check** experimental mode transmits version 4 and
requires a receiver that understands that version. Version 4 identifies the
canonical raster after mapping pixels to the shared 8/16/32-color palette and
packing those indices in raster order. Protected metadata carries its CRC32
and a 32-bit BLAKE2s content tag. Each version 4 FEC block also carries its
block index and count under its RS and CRC protection. Checked blocks can
contribute verified regions, while the card is promoted as a complete verified
image only when every required block passes and the reconstructed canonical
raster matches both metadata checks. Version 3 remains decodable and retains
only its per-block CRC verification scope; version 3 and version 4 blocks never
share an assembly identity.

For a 32×32 8-color card, the new version 4 metadata and block-position fields
increase a clean cycle from 1,236 to 1,294 tones (about 4.7%). This is a symbol
count comparison, not an RF recovery or sensitivity result.

## Verified behavior and remaining checks

Protocol fakes cover KISS escaping, fragmented reads, dynamic ports, refused
modes, queue limits, ACK routing, disconnects, host statuses, and reconnects
without replay. Qt tests cover manual/automatic exchanges, cancellation
reporting, receive preview, and the companion API. Two isolated native hosts
and two pinned Python hosts have exchanged CRC-verified cards. The packaged
Linux app has connected to its managed host and discovered/applied a mode.

Still to verify with live equipment:

- Audio and CAT/PTT ownership with the operator's selected host and radio.
- Data2G Robust and Fast plus local Resilient 100/50/25 over the two-window
  WebSDR/radio path, with verified-card match results and UI responsiveness.
- Actual airtime and receive quality for the target host/audio/radio setup.

Incremental redundancy remains a separate future protocol feature. It is not
part of the merged broadcast API and Pixel QSO does not claim soft combining
across Data2G broadcast copies. Audio/PTT ownership and live RF performance
depend on the connected host and radio setup.
