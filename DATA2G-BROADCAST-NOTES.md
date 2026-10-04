# Data2G broadcast integration notes

The broadcast interface was accepted and merged in Data2G PR #37. These notes
describe the supported API at [the merge commit](https://github.com/arodland/Data2G/blob/77856c901dd8db2642d72fcf869c727918429e4f/docs/broadcast.md)
and list the interoperability checks still needed for PixelQSO. The app now
uses the command and KISS TCP interfaces and imports no Data2G modem internals;
the pinned Python host runtime runs in a separate process.

## Direction

Use the Data2G host as a **radio interface**. PixelQSO owns card identity, card format, fragment indexing, image assembly, deduplication, received-card persistence, and contact stages. Do not ask Data2G to understand PixelQSO's cards or maintain application-level objects. Open a named broadcast group such as `PIXELQSO`; send app-framed card chunks as KISS frames; rebuild cards from checked frames delivered to that group.

Named groups map to KISS ports, `MODES` reports bandwidth and capacities, the
host chooses transmit mode, and bursts are one-to-many without an ARQ session.
`HEARD`/`LOST` statuses report decoded group traffic. ACKMODE confirms a frame
was sent over the air, not that a peer received it. Incremental redundancy is
not part of PR #37.

## Application payload framing

Each KISS information frame should contain one independently useful PixelQSO fragment with a small versioned header:

- PixelQSO card/burst ID (collision-resistant across the receiver's retention window).
- Frame type / QSO stage, callsign and grid or a reference to card metadata.
- Fragment index and total fragment count (or byte offset and total length).
- Palette and canvas format needed to interpret the pixels.
- Fragment bytes and an application-level checksum; a whole-card checksum is checked after reassembly.

The app checksum protects card assembly and storage; Data2G's group-masked codeword CRC decides whether radio bytes are delivered. Never mark a card exact from an unchecked frame. Keep framing independent from KISS escaping and Data2G codeword boundaries. A received valid fragment may update the live preview; only the whole-card checksum makes a complete saved card.

The app should tolerate duplicate fragments across bursts and reject fragments with a different card ID, fragment count, metadata, or whole-card checksum. Use the available `MODES` capacities and airtimes when selecting a mode and chunk size; do not hard-code Data2G codeword sizes in the card format.

## Queueing, copies, and completion

The host shares its queue across ports and may pack compatible queued frames
into a burst. PixelQSO submits only one ACKMODE-tagged frame at a time and waits
for the ACK before submitting the next fragment or repeat, so each app frame
waits for the previous burst to finish. ACKMODE reports that the burst carrying
the frame finished transmitting; a `DROPPED` status without an ACK means it was
not sent. `HEARD` is only known at the end of a decoded burst, so preview is
burst-by-burst, not intra-burst. The app advances automatic QSO stages after
local ACKMODE completion and never treats it as remote receipt. Host behavior
under simultaneous groups still needs live measurement.

KISS frames can be split across codewords. The protocol specifies that a failed
codeword can lose the frames it touches and possibly later frames if a length
field is lost. Keep each PixelQSO fragment independently checksummed and small
enough that losses do not invalidate an entire image. Validate malformed lengths
and maximum frame sizes at both ends.

## Incremental redundancy follow-up

Incremental redundancy was explicitly left out of PR #37. If a later protocol adds it, the useful radio interface is an application-marked resend/RV extension, not Data2G object accounting. Define a stable retry identity that names the same encoded application frame across bursts, an RV value, mode compatibility rules, bounded soft-buffer lifetime/capacity, and a status for successful decode or expiry. Data2G should combine soft codeword information only when identity, payload, and coding layout match. PixelQSO then receives the recovered KISS frame through the ordinary group port and continues its existing fragment assembly.

Do not assume the planned phrase “the position-only scrambler already lets a resend combine from any slot” settles all wire details. Confirm exactly how a sender marks the frame/RV, how receiver state is keyed, what happens on a mode change, when RVs wrap, and what data lets a receiver that missed the first pass identify the retry. Measure the airtime and recovery benefit against sending an ordinary repeated frame.

## Host and radio ownership

The integrated `data2g-host` owns radio audio and PTT for this backend. PixelQSO
connects to a separately installed host, releases its local CAT/audio receive
path, and does not manage host process lifetime.

Statuses are delivered to command clients after broadcast setup. Group names
and CRC masks are routing/integrity aids, not encryption or authentication.

## Remaining interoperability questions

1. What is the actual maximum KISS information-frame size, including framing overhead? Does `MODES` capacity describe KISS information bytes or PHY data bytes?
2. How does the host batch queued application frames into bursts under concurrent groups and modes? ACKMODE identifies per-frame transmit completion, but live queue behavior still needs measurement.
3. Which host configuration and audio-device combinations provide reliable exclusive CAT/PTT/audio ownership for PixelQSO?
4. What should a future incremental-redundancy KISS extension identify and report? Can a late listener combine an RV if it missed RV0?

## Implementation gates

- Pin a Data2G revision whose broadcast protocol is implemented, then test against the host process rather than importing its Python internals.
- Verify two clients can exchange a `PIXELQSO` group frame without ARQ, and that another group does not receive it.
- Test one damaged/missing image fragment, duplicate frames across bursts, a late-start receiver, and corrupt/reordered app fragments. Verify preview improves only from valid fragments and final saves require the whole-card checksum.
- Verify the host-reported mode bandwidth and actual generated/captured airtime, including queue delay and PTT ownership.
- Treat RV soft combining as a separate follow-up gate; do not claim its reliability benefit until measured on impaired audio and then RF.

The direct PCM adapter described in [DATA2G-BACKEND.md](DATA2G-BACKEND.md) has
been removed. Synthetic protocol and Qt tests pass, but live host KISS exchange,
actual maximum frame sizing, radio ownership, and on-air WebSDR measurements
remain unverified.
