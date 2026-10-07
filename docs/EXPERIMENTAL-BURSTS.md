# Single-burst experimental modems

Current application TX adds a versioned common mode/copy header and trailer. All these
formats support 1–20 copies and soft combining, including the equalized receiver.
The timings below describe native payloads; add 3.2 seconds per copy for framing.
See [shared copy framing and receiver hardening](../tools/COPY_COMBINING.md).


Enable **Show experimental modem modes** in Station settings, select
**Experimental modems**, then select one of the four new modes. Each sends one
burst containing a 32×32 card in the canonical eight-color palette. Larger cards
are rejected with a size explanation. Existing modem IDs and formats remain
available.

| Mode ID | Modulation and correction | Burst airtime |
| --- | --- | --- |
| `experimental_8fsk_ldpc` | 8-FSK, 600 baud, soft LDPC | 5.005 s |
| `experimental_qpsk_equalized` | QPSK, soft LDPC, adaptive equalizer | 4.622 s |
| `experimental_8fsk_outer_rs` | 8-FSK, 400 baud, inner RS and outer erasure parity | 5.480 s |
| `experimental_16fsk_ldpc` | 16-FSK, 400 baud, soft LDPC | 5.640 s |

Times include training, pilots, and guards. All four enforce one burst, including
when an older saved Copies setting requests repeats. These are CPU implementations
using the existing optional SciPy/NumPy experimental-modem dependencies.

## Formats and decoding

The two FSK LDPC modes transmit 8,192 interleaved bits from the existing 3GPP BG2
LDPC code. The 422-byte card envelope contains 3,376 information bits, including
whole-card CRC32. Untransmitted code positions enter decoding as erasures. Gray
tone labels and soft bit likelihoods feed iterative decoding. The 8-FSK tone
centers span 450–2,550 Hz at 300 Hz spacing; the 16-FSK centers span
375–2,625 Hz at 150 Hz spacing. These are center-frequency ranges, not occupied
bandwidth specifications. Acquisition searches frequency offset, and distributed
pilots refine timing and clock drift. The FSK formats require matching new peers.

The QPSK mode retains the existing single-burst QPSK waveform. Its receiver adds
a 21-tap fractionally spaced equalizer with regularized training, pilot adaptation,
and gated decision-directed updates. If its likelihoods fail verification, it
tries the existing pilot receiver on the same burst. It does not combine copies.
Diagnostics identify which receiver succeeded.

The outer-parity mode pads the envelope to twelve 36-byte data shards and creates
four GF(256) parity shards. Any twelve checked shards reconstruct the envelope.
Each transmitted shard includes its index and CRC16, protected by a shortened
GF(64) RS(62,52) code that corrects up to five symbol errors. A bounded soft Chase
search provides additional recovery attempts. Tone centers span 800–2,200 Hz at
200 Hz spacing. Complete cards still require whole-card CRC32 verification.

All modes report synchronization activity before complete decoding. The
outer-parity mode can display checked shard pixels once the protected header is
available. LDPC modes display pixels only after whole-card verification. Partial
coverage and synchronization alone never count as a verified received card.

## Validation and limits

The source suite passed 112 tests. New tests cover exact card recovery, missing
shards, five-symbol inner RS correction, early activity and previews, wrong-mode
and noise rejection, damaged CRC rejection, saved WAV decoding, receive-all, and
Qt live-decoder completion. A deterministic echo/noise fixture exercises an
adaptive-equalizer recovery rejected by the original QPSK receiver.

A comparison run made 126 attempts across six modes, three cards, clean audio,
noise, tuning/clock drift, echo, fading, combined impairments, and noise-only
controls. It recorded zero false verified cards and zero backend errors. All
three new FSK modes failed the combined impairment profile; the outer-parity
mode recovered only one of three cards under the stronger 2 ms echo. Both QPSK
receivers recovered all cards in these small comparison fixtures. This does not
establish equalizer superiority or equal-energy performance across modes.

Two real Qt windows also exercised the transmit pump, PCM16 loopback UDP input,
live workers, shared assembly, saved cards, and session wall for all four modes.
Every card matched exactly; each showed early activity, and outer parity showed
partial pixels. This test uses temporary storage and no audio hardware, CAT, or
PTT. Source tests and loopback results do not establish HF, sound-device, or
frozen-bundle performance.

Reproduce the checks from the repository root:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -q
.venv/bin/python tools/benchmark_experimental_bursts.py --help
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/verify_experimental_burst_link.py
```

The local comparison artifacts are in `work/four-experimental-bursts-20261005/`:
`report.html`, `results.json`, `live-link.json`, and `live-received-card.png`.
More tones increase bits per symbol, but closer spacing, timing sensitivity,
bandwidth, and channel impairments constrain useful parity gains. These modes
are experiments rather than recommendations for field operation.
