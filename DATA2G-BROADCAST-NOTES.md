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
whole-raster CRC32, and a 64-bit BLAKE2s image identity. Receivers retain
interleaved images by sender and image identity, accept duplicate or
out-of-order fragments, and reject conflicting overlap. Pixel coverage can
update the preview before the whole-raster CRC passes; only a complete exact
raster is accepted as verified.

Pixel QSO currently sends application frames up to 256 bytes and lets the host
map them onto its own codewords. The app frame size is independent of PHY
codeword boundaries. Native and pinned Python host-to-host tests transfer a
full card through paced audio links and verify its identity and CRC.

The former `PQD2` PCM waveform/application path is intentionally not supported
by the host/KISS receiver. Old waveform captures need the old decoder; saved
card JSON remains independent of that waveform format.

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
