# Data2G broadcast integration notes

These notes track how PixelQSO should integrate with Andrew's **draft**, not a released or frozen API. Source reviewed: [`docs/broadcast.md` at `0a9dd37a189a9f413eab0039e5d68f0edc1d570c`](https://github.com/arodland/Data2G/blob/0a9dd37a189a9f413eab0039e5d68f0edc1d570c/docs/broadcast.md). Recheck the draft and negotiate any changed behavior before implementing against a newer revision.

## Direction

Use the Data2G host as a **radio interface**. PixelQSO owns card identity, card format, fragment indexing, image assembly, deduplication, received-card persistence, and contact stages. Do not ask Data2G to understand PixelQSO's cards or maintain application-level objects. Open a named broadcast group such as `PIXELQSO`; send app-framed card chunks as KISS frames; rebuild cards from checked frames delivered to that group.

The existing draft provides useful foundations: named groups map to KISS ports, mode enumeration reports bandwidth and capacities, the host chooses transmit mode, bursts are one-to-many without an ARQ session, and `HEARD`/`LOST` statuses report decoded group traffic. Andrew explicitly leaves incremental redundancy for later and proposes an application-marked resend with an RV, while Data2G retains soft information to combine it.

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

The draft says all ports share a queue; the next burst takes the first queued frame's port and mode, then adds queued frames for that group/mode that fit. This means enqueuing every copy immediately may not produce one separately observable copy per UI `Copies` count, and frames for multiple image fragments may share a burst. Before implementing copy scheduling, agree on one of:

1. A documented way for the client to pace frames so each intended pass forms a burst, or
2. A transmit status event that reports when a group's burst actually goes on air, its mode, frame/codeword count, and the application frame IDs it carried.

The current draft defines receive statuses (`HEARD`, `LOST`, and ambiguous `MISSED`) but no transmit completion event. PixelQSO needs completion information to count actual passes, estimate contact-stage completion, and avoid treating a queued frame as transmitted. `HEARD` is only known at the end of a decoded burst, so progressive preview is burst-by-burst, not intra-burst.

KISS frames can be split across codewords. The draft notes that a failed codeword loses frames it touches and possibly later frames if a length field is lost. Keep each PixelQSO fragment independently checksummed and small enough that losses do not invalidate an entire image. Validate malformed lengths and maximum frame sizes at both ends.

## Incremental redundancy follow-up

This is explicitly **not in the broadcast draft yet**. When added, the useful radio interface is an application-marked resend/RV extension, not Data2G object accounting. Define a stable retry identity that names the same encoded application frame across bursts, an RV value, mode compatibility rules, bounded soft-buffer lifetime/capacity, and a status for successful decode or expiry. Data2G should combine soft codeword information only when identity, payload, and coding layout match. PixelQSO then receives the recovered KISS frame through the ordinary group port and continues its existing fragment assembly.

Do not assume the planned phrase “the position-only scrambler already lets a resend combine from any slot” settles all wire details. Confirm exactly how a sender marks the frame/RV, how receiver state is keyed, what happens on a mode change, when RVs wrap, and what data lets a receiver that missed the first pass identify the retry. Measure the airtime and recovery benefit against sending an ordinary repeated frame.

## Host and radio ownership

The draft integrates command, data, and KISS ports in one `data2g-host`. PixelQSO currently has its own audio and CAT/PTT path. Pick a single owner for audio playback and PTT for this backend; do not let both apps key or play the same radio. If PixelQSO keeps CAT frequency control, establish how its PTT requests coordinate with the Data2G host. Decide whether the host runs as a managed child process or a separately installed service, and document startup, readiness, shutdown, and reconnect behavior.

The draft's statuses go only to ports opened on the command connection. Confirm notification delivery and reconnection semantics, plus group-port lifetime and client ownership. Group names and CRC masks are routing/integrity aids, not encryption or authentication.

## Questions to settle with Andrew before integration

1. Is the broadcast host protocol intended as a stable supported interface for third-party applications? How are versions negotiated?
2. How should PixelQSO pace frames or learn that a burst was actually transmitted?
3. How do clients choose a mode and avoid queue mixing when several groups or modes are active?
4. What are maximum KISS frame and burst sizes, including framing overhead? Does `MODES` capacity describe KISS information bytes or modem payload bytes?
5. What exactly will the incremental-redundancy KISS extension identify and report? Can late listeners combine an RV if they missed RV0?
6. What host configuration cleanly supports an application that already controls CAT/PTT or audio devices?

## Implementation gates

- Pin a Data2G revision whose broadcast protocol is implemented, then test against the host process rather than importing its Python internals.
- Verify two clients can exchange a `PIXELQSO` group frame without ARQ, and that another group does not receive it.
- Test one damaged/missing image fragment, duplicate frames across bursts, a late-start receiver, and corrupt/reordered app fragments. Verify preview improves only from valid fragments and final saves require the whole-card checksum.
- Verify the host-reported mode bandwidth and actual generated/captured airtime, including queue delay and PTT ownership.
- Treat RV soft combining as a separate follow-up gate; do not claim its reliability benefit until measured on impaired audio and then RF.

Until these gates pass, the direct Python adapter described in [DATA2G-BACKEND.md](DATA2G-BACKEND.md) remains the experimental implementation; the broadcast host draft is the intended integration direction, not a current dependency.
