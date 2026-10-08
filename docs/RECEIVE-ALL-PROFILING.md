# Receive-all acquisition and latency

For captures without a decoded common header, receive-all tries the selected
local modem first. If it does not return a verified card, remaining modes are
tried in stable order by the shortest audio window at which each can acquire
useful signal. A mode is skipped until its minimum window is present. The
legacy packet modem remains available when selected directly but is excluded
from blind receive-all. When a protected common header is found, its announced
decoder runs directly; the scan is reused rather than repeated by each native
modem. Card identity and checksum checks still control promotion.

For legacy packet audio above 8 kHz, the decoder resamples once to 8 kHz before
sync acquisition. Its modem tones remain within the new passband. Automatic
profile selection returns the evidence it already acquired on the prefix and
reuses it when possible, instead of fitting that profile a second time. The
sync template search batches equivalent correlations while keeping its tested
timing, clock, and carrier search ranges.

PCM receive audio remains in a bounded 48 kHz mono `bytearray`. Live Qt
chunks append efficiently; each worker gets a private immutable snapshot, so
live writes cannot race its decoder. Trimming, WAV export, waterfall updates,
and copy combining continue to use the same contiguous sample stream. PCM16
conversion normalizes the float array in place to avoid a second float-sized
allocation. A fixed or ring buffer would still need a contiguous snapshot for
the present modem decoders and would add wraparound and trim bookkeeping.

## Measurements

`work/rx-latency/pre-change.json` and `optimized.json` use one encoded example
16-FSK grid-mode transmission and the same growing audio prefixes. The selected
mode was held constant. cProfile traces showed that receive-all spent 18.9 of
19.4 seconds at one second of audio repeatedly fitting sync templates while
trying the legacy profiles. Filtering to 8 kHz, reusing profile evidence, and
batching sync scoring brought that prefix to 1.2 seconds. At 1.5 seconds the
measured time fell from 18.0 to 1.7 seconds. Neither partial capture contains
enough header to verify an image. At the complete 4.38-second signal, exact
image recovery took 0.81 seconds after 0.79 seconds before; the dominant gain
is avoiding expensive failed acquisition on short captures.

The isolated Qt/UDP live test verified the exact pixels and showed receive
activity in 5.92 seconds total for a 4.38-second waveform. It tests the live
transmit pump and receive-all routing, not sound-device timestamps or RF.

The bytearray conversion benchmark asserts bit-for-bit sample equality. For a
90-second capture, snapshot plus conversion took 13.84 ms before and 12.96 ms
after on this host. Storage-container replacement was not justified by that
small saving.

A seeded `resilient_25` pass through the AWGN channel at −6 dB verified the
exact card. On the same 60.92-second capture, warm receive-all decode fell
from 4.67 seconds to 3.36 seconds median across three measured runs. The common
header correlator now transforms the capture once and reuses its spectrum for
the tone bank; selected-mode live decode also avoids a duplicate header scan
when no copy has been cached yet. A cProfile run reduced `find_copies` from
5.10 to 3.22 seconds, though those instrumented timings include different
lazy-import costs and are less comparable than the warm end-to-end runs.

These are controlled software measurements; they do not establish HF receive
performance. Full traces, JSON, and test logs are stored under
`work/rx-latency/` for this checkout.

## Reproducing

Profile a mono, 16-bit WAV capture using the selected mode and receive-all:

```sh
.venv/bin/python tools/profile_receive_all.py capture.wav \
  --selected-mode experimental_16fsk_grid --include-experimental \
  --output work/rx-latency/capture-profile.json
```

Measure PCM snapshot and conversion costs:

```sh
.venv/bin/python tools/benchmark_receive_buffer.py
```
