# Experimental UTC-grid compressed 16-FSK

Select **Experimental modems → 16-FSK · compressed · UTC start grid**.
Enable experimental modes on both clients. Every message sent with this mode
(card, CQ, exchange, or 73) uses the same 500 ms UTC start grid: .000 and .500.
Other modem selections keep their existing transmit behavior.

This is compressed 16-FSK with a distinct stable key `experimental_16fsk_grid`
and protected v3 common-header mode ID **18**. It retains automatic lossless
compression of the eight-color raster, six shortened LDPC profiles, 1–20
independently synchronized copies, drift tracking, timing alternatives,
identity-bound combining, and whole-card CRC32 verification. The opening
header is still required. Payload/header lengths are unchanged; scheduling
adds leading silence rather than repeating headers. An example one-copy card
has 4.38 seconds of waveform, plus alignment silence.

## Timing and clock setup

The OS owns UTC synchronization. Configure its normal time service; the app
neither changes the system clock nor queries a hard-coded Internet NTP server.
A bounded `chronyc -c tracking` check at TX reports estimated clock error or
unknown/unsynchronized status in the exchange log. Chrony is optional; Windows
and other time services currently report unknown. No dependency on Internet
connectivity or a shared NTP endpoint is introduced.

After PTT confirmation and the existing 350 ms radio settling interval, or
immediately in audio-only mode, the app opens output and computes the next
boundary allowing 40 ms for initial audio submission. It prepends sample-counted
silence and immediately primes the transmit pump. Alignment silence is roughly
40–540 ms. This extra preparation margin means average added silence is about
290 ms before accounting for a configured device delay. Hardware startup and
PTT settling are additional. Silence extends the playback deadline but does
not change the estimated modem waveform airtime.

**Grid TX audio delay** is the measured delay from initial audio submission to
physical radio audio. The app schedules earlier by this value. A buffer's
capacity is not its latency. **Grid RX audio delay** corrects the approximate
capture-start timestamp relative to return from opening input. Both are
persistent millisecond settings, default zero. Measure on each sound device
with a timestamped loopback; zero is a test default, not an accuracy guarantee.
The controls appear under transmission settings.

RX maps input startup time and sample offsets to UTC. This is an approximate
software timestamp, not an ADC timestamp. It prioritizes common-header
candidates whose waveform start is within ±100 ms of a grid boundary and admits
up to 24 extra weaker training candidates in those windows. Extra candidates
must decode specifically as mode 18 and pass the existing RS, CRC, length,
version, count, and identity gates. The original candidate budget and soft
retries remain available outside the windows. No mode, identity, or image is
accepted merely because of timing. Buffer trimming advances the UTC origin;
UDP test streams and saved WAVs have no UTC origin and use ordinary acquisition.

Clock steps, capture startup uncertainty, uncalibrated devices, and scheduling
jitter can defeat the timing prior; full acquisition remains available. This
experiment does not provide collision avoidance or alternating TX/RX slots.
Old receivers do not recognize mode 18; updated receivers still read prior
modes. Exported WAV audio contains the modem waveform without a scheduled UTC
start because a file has no live transmission time.

## Verification

Run focused tests:

```sh
.venv/bin/python -m unittest discover -s tests -p test_grid_fsk.py
.venv/bin/python tools/benchmark_grid_fsk.py
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/verify_experimental_burst_link.py \
  --modes experimental_16fsk_grid --receive-all --card-pattern example --copies 1 \
  --output work/grid-fsk/live/live-link.json
```

The benchmark compares identical synthetic header captures at −6 dB SNR in a
2,500 Hz noise reference bandwidth, with no timing hint and hints offset by
0, 50, and 150 ms. It checks AWGN, mild HF, moderate HF, and noise-only controls.
It measures header recovery, not verified image recovery. The live Qt/UDP check
exercises scheduling, mode routing, and exact image verification, but has no
hardware capture timestamp and therefore does not test UTC-aware RX or RF.

Initial three-seed results at −6 dB were identical with no timing hint and with
0, 50, or 150 ms hint error: AWGN **3/3**, mild HF **1/3**, moderate HF **0/3**.
The noise control admitted no headers. These trials show fallback compatibility,
but no measured acquisition benefit yet. The additional weak candidates can
increase decode CPU time when acquisition fails.

The final source suite passed **161 tests**. The isolated two-window Qt/UDP
trial verified the exact example image with receive-all enabled: 4.38 seconds
of modem waveform and 19.10 seconds from request through live verified display.
This elapsed time includes scheduling, pumping, and decoder work; it is not a
measurement of grid alignment precision. One repeated trial timed out during
concurrent benchmark/suite load; its log is retained. The isolated repeat passed.
Artifacts are under `work/grid-fsk/`, including `validation.json`, benchmark
results, live results, and logs. Hardware audio calibration and RF testing remain
open before considering a default for all transmissions.
