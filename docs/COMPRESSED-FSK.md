# Lossless compressed FSK family

Enable Experimental modems, then choose **8-FSK · compressed LDPC copies** or **16-FSK · compressed LDPC copies**. Use the existing **Copies per card** setting (1–20). Compression is automatic and the displayed airtime depends on the selected card. These modes carry the same exact 32×32 eight-color on-air card as the grouped family.

A transmission contains one protected 1.6-second version 3 group header, followed by independently synchronized copies of a shorter LDPC payload. There are no per-copy common headers or trailers. Existing v1 and v2 modes retain their wire formats and remain selectable. The new modes require updated receivers; the application card/message and verified-image APIs do not change.

## Validated plan

Merely compressing pixels inside the old 8192-bit codeword would save no airtime. This family therefore combines lossless source compression with six genuinely shorter coding profiles. TX chooses the smallest complete packet among:

1. The original packed three-bit pixels (raw fallback).
2. Raw DEFLATE of those packed pixels.
3. Pixel runs: one byte encodes a 1–32-pixel run and its three-bit color index.
4. XOR of each 12-byte packed row with the preceding row, followed by raw DEFLATE.

Ties prefer the lower codec ID, including raw. Compression applies before whitening, FEC and modulation. Packet size never exceeds the existing 422 bytes. High-entropy/random images fall back to the original full-size packet and have the same airtime as the corresponding grouped mode.

The smallest coding capacity that fits the selected packet is used. Unused packet bytes are zero padding. The existing BG2 mother graph and lifting are retained, trailing information columns are shortened to known zero, and rate matching transmits the following bounded number of bits. The original two leading lifted columns remain punctured. No new coding library or heavyweight dependency is required.

| Packet capacity (bytes) | Transmitted coded bits | 8FSK payload seconds | 16FSK payload seconds |
|---:|---:|---:|---:|
| 128 | 2496 | 1.740 | 1.970 |
| 192 | 3744 | 2.453333 | 2.780 |
| 256 | 4992 | 3.166667 | 3.580 |
| 320 | 6240 | 3.886667 | 4.390 |
| 384 | 7456 | 4.583333 | 5.170 |
| 422 | 8192 | 5.005 | 5.640 |

Payload durations include native training, guards, and distributed pilots. Add 1.6 seconds once per group. This is a new rate-matching experiment: comparable information/code-bit ratios do not prove identical coding gain across block sizes.

## Wire and integrity

Stable common-header mode IDs are **16** (`experimental_8fsk_compressed`) and **17** (`experimental_16fsk_compressed`). Common header version **3** retains the existing RS-protected 30-byte layout: the top five bits of the length field carry copy count minus one; the bottom 27 bits carry samples per payload at 48 kHz. Only lengths matching the six profiles of the announced mode are accepted. The profile is determined from this checked length, never guessed from a partial recording. Versions 1 and 2 cannot announce these modes. Trailers are not permitted.

A raw full-size packet remains `PQW1`. A compressed packet uses `PQC1`: byte 4 is codec ID (1 DEFLATE, 2 pixel runs, 3 row-XOR/DEFLATE), followed by bytes 5–33 of the original header. A big-endian uint16 encoded-image length follows that 34-byte header, then the encoded image and the original four-byte CRC32. Reassembly restores `PQW1` and original version byte 1 before verifying the entire original packet. Both the metadata and the exact reconstructed raster must pass the existing validation.

Inflation is limited to 385 output bytes and accepted only when it produces exactly 384 bytes, reaches stream end, and has no trailing input. Runs must fill exactly 1024 pixels without overflow. Unknown codecs/versions, invalid lengths, nonzero block padding, and invalid original CRCs are rejected.

The protected 16-byte group identity binds both canonical card content and the selected encoded packet:

```
BLAKE2s-128(canonical_content_digest || BLAKE2s-128(encoded_packet_without_padding))
```

RX hashes the actual recovered representation, rather than recompressing it. Thus different lossless representations of the same pixels cannot be soft-combined accidentally, and a different zlib encoder implementation can interoperate. Copies of different coding capacities are also decoded separately. The content digest includes mode, identity metadata, on-air colors, message type and SNR report. This preserves the existing accidental-mixing check; it is not sender authentication.

A decoded card is released only after LDPC convergence, bounded decompression, original packet CRC and metadata checks, and protected group-identity matching. Growing live snapshots replace earlier snapshots of the same copy. The existing bounded cache, receive-all selection, and saved-WAV routes are used. Losing the opening header still prevents grouped decoding.

## Measured airtime

These are actual generated waveform lengths, with one group header and three copies:

| Card | Mode | Existing grouped | Compressed | Copies fitting the old three-copy budget |
|---|---|---:|---:|---:|
| Example artwork | 8FSK | 16.615 s | 8.960 s | 6 |
| Example artwork | 16FSK | 18.520 s | 9.940 s | 6 |
| Flat/striped fixtures | 8FSK | 16.615 s | 6.820 s | 8 |
| Flat/striped fixtures | 16FSK | 18.520 s | 7.510 s | 8 |
| Random image | 8FSK | 16.615 s | 16.615 s | 3 |
| Random image | 16FSK | 18.520 s | 18.520 s | 3 |

The example packet shrinks from 422 to **192 bytes**, including compression metadata and CRC, and uses the 192-byte profile. Flat/striped packets are 50/59 bytes but use the minimum 128-byte coding profile. These are synthetic/example fixtures, not a survey of user artwork.

## Decode checks

The full unit suite passes **156 tests** after the [FSK receiver improvements](FSK-RECEIVER.md). Both compressed modes also pass exact-image TX/UDP/RX loopback with six copies: 16.32 seconds for 8FSK and 18.28 seconds for 16FSK. These exercise the Qt application paths without radio hardware.

In the initial −6 dB comparison before the receiver improvements, three six-copy 16FSK compressed AWGN trials decoded exactly (3/3), while the three-copy grouped 16FSK baseline decoded 0/3; 8FSK decoded 0/3 in both runs. All tested noise-only controls had zero false verified cards. Those initial mild/moderate fading presets did not decode; the later receiver comparison adds one mild-fading success per compressed modem. The sample is deliberately small and the six-copy case carries more repeated signal energy, so this is an early operational check of the airtime/copy tradeoff, not evidence of a coding-gain improvement. See `work/compressed-fsk/final-six/results.json`, `work/compressed-fsk/baseline-three/results.json` and [FSK receiver validation](FSK-RECEIVER.md) for exact conditions and current results.

## Reproduce

```sh
OPENBLAS_NUM_THREADS=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/benchmark_compression.py
OPENBLAS_NUM_THREADS=1 QT_QPA_PLATFORM=offscreen .venv/bin/python tools/verify_experimental_burst_link.py --modes experimental_8fsk_compressed experimental_16fsk_compressed --receive-all --card-pattern example --copies 6 --output work/compressed-fsk/live-six/live-link.json
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/benchmark_hf.py --modes experimental_8fsk_compressed experimental_16fsk_compressed --profiles awgn hf_mild hf_moderate --snr-db -6 --trials 3 --repeats 6 --card-pattern example --output work/compressed-fsk/final-six
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/benchmark_hf.py --modes experimental_8fsk_grouped experimental_16fsk_grouped --profiles awgn hf_mild hf_moderate --snr-db -6 --trials 3 --repeats 3 --card-pattern example --output work/compressed-fsk/baseline-three
```

Compare total airtime, source content, and verified recovery, not compression ratio alone. The existing HF tool defaults to random pixels, which deliberately exercise raw fallback; use `--card-pattern example`, `flat`, or `stripes` to test compressible content. Results are synthetic offline channels and real Qt/UDP application paths, not RF or sound-device validation.
