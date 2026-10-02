# Experimental weak-signal modem

Select **Weak signal · 32×32 · experimental** in Transmission type. Install the
existing optional dependency set with `uv sync --extra data2g` first. The mode
uses Data2G's pinned LDPC construction and encoder, NumPy for decoding, and
SciPy for filtering/resampling. It does not use Data2G's OFDM waveform or Torch
for decoding. Production Resilient remains the default.

This mode carries one complete **32×32 eight-color** card. Colors are mapped to
the same shared palette used by production. Callsign, grid, card ID, message
stage, optional signal report and all pixels share one codeword and CRC32.
Unsupported sizes, color counts and multiple copies are rejected. The UI limits
the copy count to one when this mode is selected. There is no partial preview:
the card appears after its parity checks, CRC and metadata checks succeed.

## Waveform and duration

| Component | Allocation |
| --- | ---: |
| Known QPSK preamble | 512 symbols / 0.256 s |
| LDPC data | 7040 symbols / 3.520 s |
| Distributed pilots | 880 symbols / 0.440 s |
| Pulse-shaping tail | 0.006 s |
| Audio guards | 0.400 s |
| Generated audio | **4.622 s** |
| Existing app PTT lead | 0.350 s |
| Nominal keyed interval | **4.972 s** |

The pulse shape is root raised cosine at 2000 symbols/s, rolloff 0.25, spanning
12 symbols. Internal sample rate is 8 kHz, carrier is 1500 Hz, and output is
48 kHz mono audio. Nominal spectral edges are 250–2750 Hz. The first example
waveform measured 99.9921% of its power within these edges, with a 99% occupied
bandwidth of approximately 2.20 kHz. Finite pulse shaping has small sidelobes.
The regression requires at least 99.9% in the 2.5 kHz interval.

The keyed interval is a software timing budget, excluding unmeasured audio
device/CAT scheduling delays. The IC7300 passband suitability is accepted as
requested; it has not been measured here. Two full bursts would exceed five
seconds and are not offered by this profile.

## Frozen frame version 1

Header format is big-endian `>4s6BH12s8sBb`, 34 bytes:

1. Magic `PQW1`, version 1, width 32, height 32, palette ID 0.
2. Callsign and grid byte lengths, then unsigned 16-bit card ID.
3. Uppercase ASCII callsign padded to 12 bytes and grid padded to 8 bytes.
4. Message code: card=0, CQ=1, exchange=2, 73=3.
5. Signed signal report in dB; −128 means absent.

The packed raster is 384 bytes, three bits per pixel, most significant bit
first. CRC32 follows as four big-endian bytes and covers header plus raster.
Total information is **422 bytes / 3376 bits**. Whitening precedes LDPC encoding.
NR base graph 2 with lifting size 352 supplies **14080 transmitted bits**, with
its native punctures, filler zeros and rate matching. The code construction is
checked against a frozen matrix hash at runtime. This is a custom modem using
that code; its frame is not a 5G radio protocol.

Pilot and preamble sequences, whitening and interleaving come from the exact
ordered NumPy PCG64 calls in `constants()`, seeded with `0x50515731`. Their
canonical array hashes (complex128 or int64, little endian) are:

| Array | SHA256 |
| --- | --- |
| Preamble | `afb0b022e01b13d309d947f2ef55e258c8d52e7fcba2bf2360d1d48c2cd7c8f8` |
| Pilots | `20694608c77587bc0af4d114197100f0f6e2df15a0401eeb28178736be8287c7` |
| Interleaver | `11b15a8003ccc6caa7cac13af2de18cc7fee0f5bca0ec425d7733083664984c1` |
| Whitening | `47cf26c22bf2f1e36747bf549534f3bda6f88091a82b4d40c0b7c8131f3869c4` |

Pairs of interleaved coded bits select QPSK real and imaginary signs, positive
for bit zero, each axis scaled by 1/√2. After the preamble, each of 110 blocks
contains eight known pilots followed by 64 data symbols.

## Receiver

Acquisition searches all four sample phases and frequency offsets ±100 Hz,
then refines the frequency estimate against the known preamble. Pilot coherence
selects fractional timing and clock error over ±140 ppm. Local pilot estimates
track complex gain; a nine-block smoother reduces noise before soft bit
likelihoods are formed. The CPU sum-product decoder has a 100-iteration bound.
Parity success alone does not release an image: CRC32 and structural checks
must also pass. Acquisition receives no expected card, start time or channel
parameters from the benchmark.

Diagnostic SNR is a pilot residual estimate referenced to 2500 Hz; it is not
the benchmark's authoritative known-component measurement. Decoder diagnostics
also include acquisition score, carrier offset, clock estimate and iteration
count. Buffered acquisition is repeated for live snapshots in a Qt worker.

The first implementation has no adaptive multipath equalizer, no combination
of separate bursts, and no search beyond the stated carrier/clock bounds. Deep
fades and severe mixed channels still require further work. Synthetic AWGN
results do not establish real HF path performance.

## Reproduce measurements

```sh
OPENBLAS_NUM_THREADS=1 .venv/bin/python -m unittest discover -s tools -p test_weak_signal_modem.py -v
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/test_weak_signal_app.py
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/weak_signal_campaign.py --output work/new-weak-campaign
```

The app test exercises mode selection, the application's encoder, two windows'
normal UDP audio timers, int16 audio, live decoding, the received image and the
saved checked card at −3 dB. It also waits for the saved-WAV decode and detects
exceptions in Qt callbacks. Station data is temporary and hardware is bypassed.

The diverse campaign uses five cards (solid, patterned, tiled, random and
concentric), different identities and 20 independent noise seeds per card at
each level. It tests production Resilient alongside the new mode, preserves
source snapshots, and records calibration and individual failures. The SNR
reference and receive filter follow [BENCHMARKING.md](BENCHMARKING.md).

The initial 20-seed, single-card bracket is in
[weak-signal-v1-bracket](work/first-comparison/weak-signal-v1-bracket/report.html):
20/20 checked recovery at −3 and −4 dB; 2/20 at −5 dB; zero at −6 through −8 dB.
Use the diverse campaign for stronger conclusions. Promotion to production
remains separate from making this experimental modem available.

## Completed measurements

The [five-card campaign](work/first-comparison/weak-signal-diverse-v1/RESULTS.md)
contains 1300 attempts, with 100 independent noisy attempts per reported setting:

| SNR in 2500 Hz | Experimental checked recovery | Production Resilient, one copy |
| ---: | ---: | ---: |
| −3 dB | 100/100 | 0/100 |
| −4 dB | 100/100 | 0/100 |
| −4.5 dB | 100/100 | 0/100 |
| −5 dB | 16/100 | 0/100 |
| −5.5 dB | 0/100 | 0/100 |
| +5 dB | Not tested in this campaign | 6/100 |
| +6 dB | Not tested in this campaign | 49/100 |
| +7 dB | Not tested in this campaign | 82/100 |

The 95% Wilson lower bound for 100/100 is 96.3%. Thus the main −3 dB target and
the −4.5 dB AWGN stretch target meet the plan's confidence requirement on this
fixed test corpus. Both the 50% and 90% empirical transitions are bracketed
between −5 and −4.5 dB; no finer threshold has been measured. Production's 50%
transition lies near +6 dB in this corpus and its 90% transition is above +7 dB.

The new burst lasts 4.622 s versus 3.090 s for production. Equal average power
therefore gives it 49.6% more energy (1.75 dB); the measured difference includes
that energy, acquisition, waveform and coding changes. It is not a measurement
of coding gain in isolation. Maximum requested-versus-measured AWGN SNR
deviation was 0.118 dB. DSP source hashes match the current implementation.

The [240-attempt impairment campaign](work/first-comparison/weak-signal-v1-stress/report.html)
used the original patterned card, with 20 attempts per setting:

| Profile | 0 dB | −3 dB | −4.5 dB |
| --- | ---: | ---: | ---: |
| +80 Hz tuning, +100 ppm clock | 20/20 | 20/20 | 19/20 |
| 2 ms echo at amplitude 0.6 | 20/20 | 20/20 | 0/20 |
| Deep sinusoidal fading | 3/20 | 0/20 | 0/20 |
| Mixed tuning, fading and echo | 8/20 | 0/20 | 0/20 |

These labels are pre-impairment SNR; fading changes measured receive SNR.
No false verified card occurred in the combined 1540 attempts. This does not
prove a zero false-acceptance probability. The deep-fading results are a clear
limit of this implementation and motivate an equalizer, improved channel
tracking and fade-oriented interleaving before any production promotion.

Nine modem regressions and the two-window Qt/UDP test pass. The latter used
approximately −2.947 dB measured reference SNR, rendered the live image, saved
the full identity and pixels, and completed the separate WAV decode. Logs are
in `work/first-comparison/weak-signal-regressions.log` and
`work/first-comparison/weak-signal-app-check.log`.
