# Two-frame animations

## Drawing an animation

Open or create a card in the editor and choose **Animate this card**. The current
drawing becomes frame 1; frame 2 starts as an editable copy, including placed stamps.

- Click either frame thumbnail or **Flip ⇄** to switch the drawing canvas. **Alt+Left**
  and **Alt+Right** also flip frames while drawing. Each frame retains its own Undo/Redo,
  selected stamp, box selection and palette color.
- **Onion skin** overlays the other frame as a faint alignment guide. Use the opacity
  slider to adjust it. The guide never becomes part of saved or exported artwork.
- Use the existing **Box select** and **Move stamp** tools to move and resize artwork.
  **Copy selection → other frame** copies a box selection or selected stamp and switches
  to the destination. The copy is a movable stamp; drag it or nudge it with the arrow
  keys. Transparent stamp pixels stay transparent. Undo on the destination removes it.
- **Copy whole frame → other** replaces the other drawing, retaining editable stamps.
  Undo on that frame restores the previous drawing.
- The live framed preview updates as you draw. **Pause/Play** controls its playback;
  the timing control sets the shared duration of both frames, from 100 to 10000 ms.

**Save both frames** saves two normal card JSON files with the existing frame-2
reference. The first save asks for the first file's name and creates a separate
`-frame-2.json` file beside it, avoiding existing companion filenames. Both drawings,
editable stamp layers and timing are saved together. Opening either file restores the
pair when the matching companion is present in the same folder or My Cards. Missing or
changed companion artwork leaves the opened card as a single card with its reference
preserved. For an existing animation, editing on-air artwork or the pairing assigns new
affected card IDs and updates the reference. Canvas format controls are locked while
authoring a pair; choose the desired dimensions and palette before starting.

**Export GIF…** exports the live draft as a looping 320×400 animated GIF, including
the card border, callsign and animation caption. It uses the current timing and both
frames' artwork, without the onion skin, selection handles or grid. Saving the cards
first is optional. GIF timing has 10 ms resolution. PNG exports remain static.

## Linking two existing cards

In **My Cards**, select the second card, then choose **More → Make selected card frame 2…**.
Choose the first card from the native dropdown and preview the timing (100–10000 ms per
frame, default 500 ms). Both cards must have the same callsign and dimensions, different
card IDs, and the first card must have no animation reference. The first file is unchanged.
Send both cards normally in either order, using any supported transmitting mode.

The receiver waits for both whole-card verified images. It then presents one looping
tile in the receive wall and contact deck. Click the artwork or the deck details button
to see both thumbnails and each card's ID, callsign/grid, receive time, mode, frequency,
and measured/reported SNR. The deck also has Pause/Resume. Originals remain separate
reception records; grouping is a presentation operation. PNG export remains a static
image or contact sheet; framed animated GIF export is available in the card editor.

Saved received cards can supply the first frame across sessions, including after a
restart. An incomplete image, missing reference, mismatched artwork, or missing first
frame remains an ordinary static card. Repeated receipts retain the existing wall
identity. A missing companion is described on frame 2's back and in the deck details.
Changing an already-linked second card's pairing or timing allocates a fresh card ID.
**More → Remove animation reference** removes the reference from the local saved card;
it does not revoke an animation already received by another station.

## Persistent reference

Only the second card has an optional `animation` object:

```json
{
  "animation": {
    "first_card_id": 100,
    "first_digest": "0123456789abcdef",
    "frame_ms": 500
  }
}
```

The digest is BLAKE2s with an eight-byte result, over compact JSON
`[upper_callsign, card_id, width, height, palette_size]` followed by the raster's
shared-palette indices. JSON uses Python's `json.dumps(..., separators=(',', ':'))`.
Original RGB colors are mapped to the closest shared palette of the same size, with
squared RGB444 distance and lowest-index tie breaking, matching avatar transmission.
The fingerprint therefore identifies the artwork as quantized for transmission.
Grid, message stage and signal measurements are excluded, so normal CQ/exchange/73
progress and signal updates do not change animation membership. The second card's
fingerprint similarly binds its companion metadata to its actual decoded artwork.
This provides content association, not station authentication.

Receivers require matching first-card ID, station, dimensions, and fingerprint.
A first frame carrying its own animation reference is rejected to avoid chains/cycles.
Conflicting sidecars for the same second artwork suppress new association in the bounded
in-memory metadata cache. Metadata never promotes an unverified raster.

## Backward compatible companion metadata

Normal card image headers and raster bytes are unchanged. Metadata is optional and
uses a separately protected companion message. Losing or rejecting that message leaves
normal native decoding and verification available.

### Data2G

The sender adds a 29-byte companion application frame before the ordinary image
fragments on each repeated card transmission. Big-endian layout:

| Field | Bytes |
| --- | ---: |
| `PQAN` | 4 |
| Version (`1`) | 1 |
| First card ID | 2 |
| Duration per frame in ms | 2 |
| First artwork fingerprint | 8 |
| Second artwork fingerprint | 8 |
| CRC32 over preceding bytes | 4 |

Older PixelQSO receivers reject this unknown frame and continue receiving the unchanged
`PQI2` image fragments. A new receiver caches the reference and attaches it only after
verifying the matching second image. A late companion also updates matching saved QSO
reception metadata without adding a fictitious reception attempt. Existing Data2G
transport, fragment capacity, and radio ACK policy are unchanged; airtime estimates
include the additional frame.

### Local audio modems

Companions use the existing common-header training, tone permutation, RS protection,
and CRC32, with `PQCH` version `5`. The body retains `>4sBBI16s`:

- Mode byte: the normal mode ID, without the trailer bit.
- 32-bit field: first card ID in the high 16 bits, frame duration in the low 16 bits.
- 16-byte field: first fingerprint followed by second fingerprint.

Version 5 is metadata only: it never announces a payload or selects an acquisition.
Ordinary v1/v4 opening and closing headers still bracket each native payload. Companions
sit outside those boundaries, before and after each copy. Grouped modes retain their
ordinary v2 header and uninterrupted payload group, with companions outside the group.
Receivers collect references during existing header scans, retain them in the bounded
copy cache, and attach them by artwork fingerprint after verification. Air-time estimates
include both extra companion markers. Old header readers reject version 5 and still find
the ordinary supported markers and native image payload.

## Validation boundary

Automated coverage includes metadata CRC/binding, malformed links, quantized RGB/indexed
fingerprint parity, reversed/missing/partial reception, reused IDs and changed artwork,
Data2G normal-fragment parity and assembly, late metadata persistence, cross-session
pairing, save/load/TX snapshots/unlink, animated Qt presentation and clicks, and complete
local audio roundtrips with ordinary, grouped and interlaced copies. An older-header-reader
simulation recovers the same complete static image while rejecting the new sidecars.
Offscreen deck and combined-details renders were visually inspected. Physical audio,
CAT/PTT, RF propagation, a separate older installed client, and live Data2G two-host
animation transfer remain unverified. No running receiver or container was restarted.
