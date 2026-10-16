# Provisional replies and reception reports

**Send Exchange** and **Send RR73 with report** both carry the receiver's current
progress for the card being replied to. Saved-card and quick-draw replies use the
same report path. An identified provisional CQ, Exchange, or ordinary card can be
replied to manually. The action is labeled provisional; replying does not mark the
received image verified or permit adding unverified artwork to My Cards.

A typical report reads:

> Their reception of K1ABC card 1234: 384/1024 pixels received · 2/10 blocks passed CRC · RETRY NEEDED · card not verified

Counts use the accumulated receive snapshot, not the number of painted placeholders,
a percentage rounded back to pixels, or the SNR. Received pixels and CRC-passed
blocks are separate observations. Even 1024/1024 candidate pixels require a retry
when whole-card integrity has not passed. Missing CRC-block telemetry is reported as
unavailable, rather than zero failed blocks. Data2G reports distinct checked transport
fragments; it cannot report the host modem's internal CRC-block count or an unadvertised
fragment total. Those counts are explicitly labeled **CRC-checked transport fragments**.
Repeated copies of the same fragment range do not increase that count.

The report identifies the target callsign and card ID. It appears with signal reports
in incoming details, wall backs, contact decks and saved QSO reception/sent snapshots.
A checked report can be shown while the reply image itself remains incomplete: it is
associated with checked image identity, and never supplies image verification evidence.
Late metadata updates existing saved QSO receipts without adding a reception attempt.

Provisional replies are explicit user actions. Automatic exchanges retain whole-card
verification requirements. An incomplete report about the current outgoing card does
not advance automatic Exchange/RR73 sequencing, and an RR73 reporting incomplete
reception does not offer final 73. Existing automatic timeout/retry behavior remains
responsible for any retry; receiving a report does not introduce a new radio ACK mode.
A report-bearing RR73 is recognized even when a measured SNR was unavailable.

## Transmit lifetime

Reception reports are transient properties of a prepared outgoing card. The app copies
current evidence when preparing Exchange or RR73 and records that sent snapshot. A saved
My Cards file does not acquire somebody else's receive progress. CQ, ordinary card and
final-73 sends clear transient feedback. The image-content identity excludes the report,
so new progress can accompany another copy of unchanged artwork. Estimated airtime includes
metadata overhead.

## Companion format

Normal native image headers and raster bytes remain unchanged. Data2G adds an optional
40-byte `PQRP` application frame before the ordinary image fragments on each repeated
transmission. All integers are big endian:

| Field | Bytes |
| --- | ---: |
| `PQRP` magic | 4 |
| Target callsign, ASCII padded with NUL | 12 |
| Target card ID | 2 |
| Flags | 1 |
| Format version (`1`) | 1 |
| Binding to the outgoing reply image | 8 |
| Pixels received | 2 |
| Total pixels | 2 |
| CRC-passed blocks/fragments | 2 |
| Total blocks/fragments | 2 |
| CRC32 of the 32-byte body after the magic | 4 |

Count value `65535` means unavailable. Flag bit 0 means whole target card verified,
bit 1 identifies an RR73 report (otherwise Exchange), and bit 2 identifies checked
transport fragments instead of raster CRC blocks. Other flag bits are rejected.
Malformed counts, inconsistent verification claims and damaged report CRCs are rejected.

For Data2G, the binding is eight-byte BLAKE2s over compact JSON containing the outgoing
checked header's uppercase callsign, card ID, image ID, width, height, palette ID,
message stage and reported SNR. Receivers can calculate this from a valid image fragment
before the raster finishes. Image CRC/content-tag promotion remains independent.

For local audio, the binding is the first eight bytes of the existing protected copy
content identity. Common-header versions **6** and **7** carry the first and second
16-byte halves of the same report body. Both retain the existing training, RS protection
and CRC32. The mode byte is the normal mode ID; the common header's 32-bit field is the
CRC32 of the complete report body and associates its halves. The 16-byte identity field
carries one half. Incomplete, corrupt or mismatched halves do not form a report.

Each complete two-marker report sits before and after the normal payload framing.
Grouped and interlaced copies retain their existing payload boundaries. Incremental
header scans retain partial report halves in a bounded cache. Metadata-only headers
never announce a payload or select a modem acquisition. Animation companions can be
carried alongside reception reports.

Older clients reject the optional companion and still decode the ordinary static
image/SNR exchange. **They cannot interpret the new incomplete-reception report.**
A missing report is not evidence of successful reception; peers without this extension
retain their existing exchange behavior. Check compatibility before relying on partial
progress reports across older clients.

## Validation

Focused coverage includes both report-bearing send actions, provisional/manual gating,
quick-draw replies, frozen snapshots, rejected overclaims, pixel/CRC separation,
Data2G fragment counting and partial-image association, full local audio roundtrips,
legacy-header fallback, incremental metadata halves, late QSO persistence and automatic
contact gating. Grouped/interlaced audio also exercises reports together with animations.
Offscreen rendered UI inspection is recorded in NEXT.md. Physical audio, PTT/CAT, RF and
live two-host Data2G behavior remain separate validation boundaries. No running receiver
or deployment was restarted.
