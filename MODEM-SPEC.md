# Pixel QSO Avatar Burst: Technical Specification

**Status:** implementation reference for the current source tree; not a ratified or frozen interoperability standard.  
**Scope:** Fast and Resilient avatar bursts (`fast_avatar`, `fast_avatar_fec`) carried by the current 8-FSK audio modem. Legacy packet/MFSK formats are out of scope.

This document records the implemented wire format in `cardmodem.py`. A compatible implementation must follow the bit ordering, field sizes, palette construction, synchronization sequence, and checks below. Where this text and source disagree, the implementation is authoritative until the format version is deliberately changed.

## 1. Protocol summary

Each transmission copy is a complete, independently identifiable image burst:

```text
FRAME_SYNC | PROTECTED_METADATA | PIXEL_DATA
```

Fast encodes the raster directly as 8-FSK tone values. Resilient encodes the raster as independently protected blocks. A configured repeat count concatenates complete copies:

```text
copy 1 | copy 2 | ... | copy N
```

There is no ACK, selective retransmission, or reverse-channel dependency. A receiver can synchronize to a later copy and combine valid pixels or blocks by their raster positions.

The protocol's logical tones are integers 0–7. The current audio modem maps those symbols to 8-FSK at 400 symbols/s. The avatar framing and the present audio waveform are implemented together; using a different modem or embedding transport requires a compatible symbol/framing adapter.

## 2. Constants and conventions

| Item | Value |
| --- | --- |
| Wire metadata version | 3 |
| Default canvas | 32 × 32 pixels |
| Supported dimensions in metadata | 1–255 per axis |
| Palette sizes | 8, 16, or 32 colors |
| Pixel-index bit width | 3, 4, or 5 bits, respectively |
| Avatar audio profile | 8-FSK, 400 symbols/s |
| Sample rate used by the audio implementation | 48,000 samples/s by default |
| Tone-index mapping | tone `t` is `tones_hz[t]`, for `t ∈ [0,7]` |
| Bit order | most-significant bit first throughout |
| Multi-byte integers | big-endian |
| Text identity | uppercase ASCII, NUL-padded in fixed-width fields |
| Image order | row-major, top row first, left to right |

The card's source palette is mapped to the nearest color in the selected standard palette using squared Euclidean distance in RGB444 channel space. The transmitted image contains palette indices, not arbitrary per-pixel RGB triples.

## 3. Standard palettes

Palette channels are 4-bit integers from 0 through 15. Palette IDs and index order are:

### Palette ID 0 — 8 colors

| Index | RGB444 |
| ---: | --- |
| 0 | (0, 0, 0) |
| 1 | (15, 15, 15) |
| 2 | (0, 0, 15) |
| 3 | (0, 15, 15) |
| 4 | (0, 15, 0) |
| 5 | (15, 15, 0) |
| 6 | (15, 0, 0) |
| 7 | (15, 0, 15) |

### Palette ID 1 — 16 colors

Generated in nested ascending order:

```text
for r in [0, 5, 10, 15]:
  for g in [0, 15]:
    for b in [0, 15]:
      append (r, g, b)
```

### Palette ID 2 — 32 colors

Generated in nested ascending order:

```text
for r in [0, 5, 10, 15]:
  for g in [0, 5, 10, 15]:
    for b in [0, 15]:
      append (r, g, b)
```

Palette iteration order is part of the wire format. Do not substitute another palette with the same number of colors.

## 4. Synchronization and copy boundary

The 24-symbol `FRAME_SYNC` is the 16-symbol alternating preamble followed by the 8-symbol sync word:

```text
PREAMBLE = (0, 3) repeated 8 times
SYNC     = (2, 1, 3, 0, 3, 2, 0, 1)
```

All values are 8-FSK tone indices. Each copy begins with this sequence and then a protected metadata header. There is no independent end marker or total-length field: metadata supplies the dimensions and palette, from which the decoder calculates the expected pixel and block counts. The next copy is found by searching for the next `FRAME_SYNC`.

## 5. Protected metadata

### 5.1 Uncoded fields

The metadata body is a packed 33-byte structure using Python `struct` format `>2s6BH12s8sBBb`. Field order and sizes are:

| Offset | Size | Field | Meaning |
| ---: | ---: | --- | --- |
| 0 | 2 | `magic` | ASCII `PQ` |
| 2 | 1 | `version` | `3` |
| 3 | 1 | `width` | Pixels per row, 1–255 |
| 4 | 1 | `height` | Rows, 1–255 |
| 5 | 1 | `palette_id` | 0 = 8 colors; 1 = 16; 2 = 32 |
| 6 | 1 | `callsign_length` | Number of meaningful bytes in `callsign` |
| 7 | 1 | `grid_length` | Number of meaningful bytes in `grid` |
| 8 | 2 | `card_id` | Unsigned 16-bit card identity |
| 10 | 12 | `callsign` | Uppercase ASCII, right-padded with NUL |
| 22 | 8 | `grid` | Uppercase ASCII, right-padded with NUL |
| 30 | 1 | `burst_code` | 1 = Fast; 2 = Resilient |
| 31 | 1 | `message_code` | 0 = card; 1 = CQ; 2 = exchange; 3 = 73 |
| 32 | 1 | `snr_db` | Signed integer dB; `-128` means not supplied |

Callsigns are limited to 12 ASCII bytes and grids to 8. SNR values, when supplied, are signed values from −127 through +127 dB. The caller's local measured SNR is separate from the value explicitly included in this metadata.

### 5.2 Checksum and header FEC

Append a 16-bit CRC-16/CCITT-FALSE of the 33-byte metadata body, most-significant byte first. Parameters are polynomial `0x1021`, initial value `0xFFFF`, no input/output reflection, and final XOR `0x0000`.

Convert the resulting 35 bytes to 6-bit values, MSB first, padding the final value with zero bits. This yields 47 data values. Encode them with shortened systematic RS(63,61) over GF(64), using the field polynomial `x^6 + x + 1` (`0x43`) and primitive element `α = 2`. Shortening omits 14 leading zero data values; append two RS parity values. The transmitted header contains 49 GF(64) values, each split into two 3-bit tone indices, for 98 tone symbols.

The encoder's two-parity recurrence uses generator coefficients `[1, 3, 2]`. The decoder corrects at most one 6-bit RS symbol error, then validates the CRC before accepting metadata. CRC failure rejects the header.

### 5.3 Metadata tone conversion

For each 6-bit GF(64) value `v`, emit:

```text
(v >> 3) & 7
v & 7
```

These are tone indices, not frequencies; map each through the selected modem profile.

## 6. Fast pixel payload

For each raster pixel, emit its palette index in 3, 4, or 5 bits, MSB first. Concatenate indices with no per-pixel padding. Split the bitstream into 3-bit groups, MSB first, padding the final group with zero bits if needed. Each group directly selects an 8-FSK tone index.

The Fast payload has no per-pixel FEC and no pixel checksum. The metadata remains protected as specified above. A decoder can report received pixel positions, but an unverified pixel cannot be distinguished from an incorrect hard tone decision.

For `P = width × height` pixels and `b = log2(palette_size)` bits/index:

```text
fast_payload_tones = ceil(P × b / 3)
fast_copy_tones    = 24 + 98 + fast_payload_tones
```

## 7. Resilient pixel payload

### 7.1 Block segmentation and packing

Raster indices are traversed once in row-major order. Each block carries at most 366 image bits (`61 × 6`), with a whole-pixel limit of `floor(366 / b)` pixels:

| Palette | Bits/index `b` | Maximum pixels/block | Data GF(64) values for a full block |
| ---: | ---: | ---: | ---: |
| 8 colors | 3 | 122 | 61 |
| 16 colors | 4 | 91 | 61 |
| 32 colors | 5 | 73 | 61 |

Within a block, concatenate pixel indices MSB first and pad with zero bits to a 6-bit boundary. Convert each 6-bit group to one GF(64) value. A final partial block is shortened to its actual data-value count; it is not padded to 61 values on the wire.

### 7.2 Block FEC and integrity check

Apply shortened systematic RS(63,61) to each block's `d` data values, where `1 ≤ d ≤ 61`: prepend `61-d` implicit zero values, compute two parity values, and transmit only the `d` data values followed by the two parity values. This corrects one 6-bit symbol error in each block.

For the block CRC, serialize its `d` data values as a continuous MSB-first bitstream, zero-pad the last byte if necessary, and calculate CRC-16/CCITT-FALSE over those bytes. Append the 16-bit CRC, most-significant bit first. Divide the CRC bitstream into 3-bit tone indices, padding the last tone with two zero bits. The CRC trailer is six tone symbols. A receiver RS-decodes the data, verifies the CRC, and accepts the block only when both operations succeed.

A block with failed RS decoding or failed CRC is rejected. Valid block data is written to its original pixel range, not the order in which the decoder happened to recover it. This preserves raster orientation while copies are combined.

For a block with `d` data GF(64) values:

```text
block_tones = 2 × (d + 2) + 6
```

The first term transmits data plus two parity values as pairs of 3-bit tones; the final six tones encode the CRC. Therefore:

```text
resilient_copy_tones = 24 + 98 + sum(block_tones for each block)
```

## 8. Repetition and receive combining

A repeat is another complete copy of the same image burst and metadata. `repeats` is a count of complete copies (not seconds and not retransmission requests). Every copy can be decoded independently; the receiver groups image data using the decoded identity and card fields, then combines data by pixel/block position.

- Fast receive combines pixel-index votes at the same raster positions. It has no correctness test for an individual pixel.
- Resilient receive accepts CRC-verified blocks and retains blocks already received from earlier copies. Repeated copies can contribute different blocks.
- A card is complete when every expected Resilient block has verified. The application saves the complete card and suppresses subsequent reception of the same card identity.
- There is no way for the receiver to ask for a missing block. If all copies lose a block, the resilient image remains incomplete.

The repeat-combine policy is application behavior around the wire decoder. It does not change the encoded copy format.

## 9. Current audio modulation and demodulation

The avatar profile is:

| Parameter | Value |
| --- | --- |
| Modulation | 8-FSK |
| Symbol rate | 400 symbols/s |
| Tone indices | 0 through 7 |
| Tone frequencies | 900, 1100, 1300, 1500, 1700, 1900, 2100, 2300 Hz |
| Nominal bandwidth label in application | approximately 1.8 kHz |
| Default PCM sample rate | 48 kHz, mono for generated WAV |
| Transmit amplitude | 0.72 full scale in the reference synthesizer |

The current synthesizer emits a phase-continuous sinusoid at the selected frequency for each symbol. It does not apply pulse shaping. The receive path correlates each symbol window against each configured tone and selects the highest-energy tone. Acquisition searches for the preamble/sync, estimates start time, carrier offset, and symbol period, then demodulates tone decisions.

The avatar path currently makes hard tone decisions before metadata and pixel decoding. Although the metadata/block codes correct errors after that decision, avatar decoding does not currently feed soft tone likelihoods into its Reed–Solomon decoder. The separate legacy 4-FSK packet modem uses convolutional coding, interleaving, and soft-decision Viterbi decoding; those mechanisms are not part of this avatar wire format.

The 1.8 kHz value is the application's nominal profile label, not a measured occupied-bandwidth result. RF filtering, audio-chain distortion, frequency error, and regulatory limits must be measured separately.

## 10. Duration examples

At 400 symbols/s, ideal encoded burst duration is `copy_tones / 400`. For 32×32 cards, including sync and metadata:

| Palette | Fast symbols/copy | Fast seconds/copy | Resilient symbols/copy | Resilient seconds/copy |
| ---: | ---: | ---: | ---: | ---: |
| 8 colors | 1,146 | 2.865 | 1,236 | 3.090 |
| 16 colors | 1,488 | 3.720 | 1,616 | 4.040 |
| 32 colors | 1,829 | 4.5725 | 1,984 | 4.960 |

Actual captured airtime can differ due to sample rounding and implementation overhead. These values are protocol symbol counts, not measured end-to-end application or radio timings.

## 11. Interoperability notes and limitations

1. Palette ordering, metadata version, tone bit ordering, sync, block boundaries, and CRC parameters are wire-visible and must not change silently.
2. Metadata version 3 identifies the current format. A future incompatible change must use a new version or another explicit negotiation mechanism.
3. Older avatar metadata versions are accepted by parts of the current decoder for backward compatibility; this specification describes version 3 only.
4. The 8-FSK audio tone map is currently required by the avatar audio decoder. A future FSK, PSK, packet, or in-band transport needs a defined adapter that preserves logical symbol order and burst boundaries, or a versioned replacement mapping.
5. Synthetic impairment and local loopback results establish software-path behavior only. They do not establish weak-signal performance, real-radio interoperability, occupied bandwidth, or regulatory compliance.

## 12. Source mapping

| Specification area | Implementation entry points/constants |
| --- | --- |
| Palette definitions and sync | `AVATAR_PALETTES`, `PREAMBLE`, `SYNC`, `FRAME_SYNC` |
| Metadata | `AVATAR_META_STRUCT`, `_avatar_metadata`, `_encode_avatar_metadata`, `_decode_avatar_metadata` |
| Fast encode/decode | `minimal_avatar_symbols`, `decode_minimal_avatar_symbols_progress` |
| Resilient encode/decode | `_rs64_encode`, `_rs64_decode`, `minimal_avatar_resilient_cycle_symbols`, `decode_minimal_avatar_resilient_symbols` |
| Audio profile and waveform | `MINIMAL_AVATAR_PROFILE`, `synthesize`, `acquire`, `demodulate_metrics` |
| Automatic burst selection | `decode_minimal_avatar_audio_auto` |
