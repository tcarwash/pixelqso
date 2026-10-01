# Proposal: Data2G unconnected object service

This is a discussion draft for Data2G's author, not a description of an existing Data2G API or an agreed on-air format. PixelQSO is the first client, but the service should carry arbitrary application bytes. It should require neither an ARQ session nor AX.25/KISS framing.

## Desired behavior

An application submits one immutable byte object and a repeat count. Data2G divides it into independently checked pieces, sends complete passes without waiting for acknowledgments, and uses successive redundancy versions (RVs) for repeated codewords. A listener can start on any pass. It retains soft information for matching pieces across passes, reports verified pieces as soon as they decode, and reports a verified complete object once all pieces are present. No received transmission causes an automatic reply.

The caller owns application meaning (image, text, avatar, contact stage). Data2G owns burst scheduling, modulation, acquisition, FEC, RV selection, soft combining, checksums, and duplicate suppression. A client should not need to know Data2G's Python module layout or codeword sizes to send bytes.

## Host boundary, version 1

Prefer a versioned local process interface over imports into a client's Python runtime. A length-prefixed binary message envelope with structured control fields and byte payloads works on localhost TCP on Windows, macOS, and Linux. The exact encoding (for example, CBOR or protobuf) can be chosen by Data2G. A single service can multiplex clients by `service_id`; registrations must not give one client another client's payloads. The first response supplies protocol version and capabilities. Unknown fields can be ignored, while unsupported major versions are rejected clearly.

Requests:

| Operation | Required fields | Result |
| --- | --- | --- |
| `HELLO` | protocol version | server version, supported features and limits |
| `MODES` | optional bandwidth cap | stable mode IDs, occupied bandwidth, usable object limit, estimated duration, sample format/rate |
| `SEND` | service ID, client TX ID, object bytes, mode ID, repeat count | accepted job ID or an explicit size/mode error |
| `CANCEL` | job ID | cancellation status; never silently substitute a partial object |
| `SUBSCRIBE` | service ID | receive and transmit events |

Events:

| Event | Required fields |
| --- | --- |
| `TX_STARTED`, `TX_PASS`, `TX_FINISHED` | job ID, pass number, RV, mode, timestamps/status |
| `RX_PIECE` | service ID, object ID, byte offset, checked bytes, total length, mode, first/last reception time |
| `RX_COMPLETE` | service ID, object ID, checked complete bytes or durable reference, total length, object checksum |
| `RX_EXPIRED` | service ID, object ID, recovered byte count, reason |
| `CHANNEL` | busy/idle and optional signal measurements, explicitly marked as estimates |

Events need monotonic per-connection sequence numbers so a client can detect a dropped subscription. `RX_PIECE` may be delivered again after reconnect; object ID plus offset makes that idempotent. The service should bound memory with documented object-size, active-object, and retention limits. A client must be able to query or discard a pending object after reconnect. The application, not Data2G, decides when a partially recovered object is useful.

For an initial implementation, Data2G can own the sound device and optional PTT, as its host already does. An optional PCM endpoint can follow for applications that own audio routing. If provided, its sample format, rate, framing, clocking, and transmit-ready behavior must be explicit. The two ownership models should not be mixed implicitly.

## Minimum on-air information

Every pass needs a reliably acquired identifier independent of the object's data codewords: format version, service ID, object ID, mode/layout, and RV (or an unambiguous RV sequence). The receiver needs these *before* combining undecoded soft bits. Repeated codewords must retain the same information payload, codeword identity, interleaving/CRC mask, and compatible submode across RVs; changing them would combine unrelated bits. A short whole-object digest guards against ID collision and mixed passes. Data2G should choose the least costly protection and placement for this information, potentially reusing its existing control-codeword machinery.

The exact on-air header, service-ID size, checksum, and RV scheduling are Data2G design choices. A useful goal is that a receiver missing pass 0 can still identify and use later passes. RVs may cycle after the distinct parity versions are exhausted; the service should report the actual RV used. The header's airtime cost must be included in mode/duration estimates.

## PixelQSO use

PixelQSO would submit its packed card bytes with `service_id = PixelQSO`, set the UI's Copies control as the repeat count, and consume checked `RX_PIECE` events for its live preview. `RX_COMPLETE` would finalize the image and QSO log. PixelQSO's callsign, grid, contact stage, and card ID stay in the application payload. Data2G does not need to parse them.

Today's experimental adapter directly imports `data2g.modem`, `data2g.tnc`, `data2g.codes`, and `data2g.config`, and decodes rolling snapshots. It repeats RV0 bursts and combines only CRC-passing fragments. Once this service exists, PixelQSO can replace that adapter without changing its editor, cards, or log. The current `PQD2` fragment header can then be simplified or removed where Data2G supplies equivalent object framing.

## Questions for the Data2G author

1. Can the existing control-codeword path carry the pass identifier robustly without a large airtime penalty? What is its minimum overhead?
2. Which existing PHY modes and CRC masks can soft-combine across RVs in unconnected operation, and must an object's mode remain fixed across all passes?
3. Should Data2G expose verified pieces, completed objects, or both? We need pieces for live progressive display.
4. What are reasonable limits for object bytes, active soft-combine buffers, and expiration time on a busy channel?
5. Would a local service that owns audio/PTT be the best first interface, with an optional PCM endpoint later?

## Acceptance checks for a first prototype

- Two independent clients can exchange an opaque object without opening a connection or receiving an ACK.
- A receiver starting after pass 0 can recover the object from later passes.
- A receiver combines at least two different RVs of the same codeword and improves recovery on an impaired audio path relative to repeating RV0; the test reports channel conditions and decode counts.
- Unrelated objects, colliding short IDs, and changed payloads are never combined or reported as complete.
- Verified pieces arrive before object completion and stay stable as later passes are heard.
- A fresh process can connect through the documented host protocol without importing Data2G Python modules.
