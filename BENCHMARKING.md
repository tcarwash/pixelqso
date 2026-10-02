# Comparing modem options

`tools/modem_benchmark.py` runs the actual `card_backends` encoders and decoders without launching Qt or transmitting. It produces `results.json`, per-trial `trials.csv`, grouped `summary.csv`, and a standalone `report.html`. The default workload is a deterministic **32×32 eight-color card**. Use `--canvas 16` or `--canvas 64` for other generated sizes, or supply `--card` to use an exported application card with a populated callsign and grid.

Start with a small comparison:

```sh
.venv/bin/python tools/modem_benchmark.py \
  --backends fast_avatar fast_avatar_fec \
  --profiles clean awgn --snr-db 12 6 0 \
  --repeats 1 3 --trials 5 --output work/first-comparison
```

Compare all installed options across the full profile set:

```sh
.venv/bin/python tools/modem_benchmark.py \
  --profiles clean awgn tuning fading echo interference clipping late-entry mixed \
  --snr-db 18 12 6 0 -6 --repeats 1 2 3 --trials 20 \
  --output work/path-comparison
```

Data2G requires the optional extra described in the README. Missing optional backends are listed in run metadata. Encoding/configuration failures stop the run; decoder exceptions become failed trials with their error text. Completed rows are reported even if a later operation fails. Runs are sequential and can take considerable time, especially with Data2G and large cards. Use a new output directory for each run.

The new `experimental_qpsk_5s` backend supports one 32×32 eight-color burst.
Requested copy counts beyond a backend's declared maximum are explicitly
listed in metadata as skipped. See [the weak-signal implementation and
threshold results](WEAK-SIGNAL-MODEM.md) and run the five-card comparison with
`tools/weak_signal_campaign.py --output work/new-weak-campaign`.

## Channel definitions

Every waveform is normalized to 0.1 RMS before impairment. By default, **`--snr-db` references noise power in 2500 Hz**, with signal power referenced to that transmit level before fading, echoes, clipping or truncation. `--snr-reference-hz` makes the reference explicit and configurable; it is distinct from the receive filter width. Equal RMS represents equal average audio drive; peak drive, RF bandwidth and transmitter behavior can differ.

Synthetic signal, interference, and noise all pass through the same receive FIR before decoding. Its default nominal passband is **100–3100 Hz (3 kHz wide)**, using a 1025-tap Kaiser-windowed sinc bandpass. Set `--rx-low-hz`, `--rx-high-hz`, and `--rx-filter-taps` to change it. FIR group delay is compensated for this buffered benchmark; this is not a causal live-receiver timing test. The actual equivalent noise bandwidth is about 2952 Hz and is recorded in metadata, along with a coefficient hash.

Noise starts as real white samples at 48 kHz. With reference bandwidth `B`, the sample variance is `0.01 × (24000 / B) / 10^(snr_db / 10)`. The receive FIR removes out-of-passband noise before the modem's acquisition downsampling. Filtering only the noise would unfairly give the signal a different channel, so the FIR acts on every component.

Per-trial outputs distinguish:

- `snr_db` and `snr_reference_hz`: requested pre-impairment SNR and its explicit noise reference.
- `snr_db_2500`: requested SNR when the reference is 2500 Hz; otherwise null.
- `snr_db_sampled`: equivalent pre-filter SNR over the full 24 kHz Nyquist band.
- `measured_snr_db_reference`: filtered desired signal versus filtered noise, converted using the FIR equivalent noise bandwidth to the declared reference. Fading and clipping therefore reduce this measured SNR.
- `measured_snr_db_rx`: desired signal versus all noise passed by the receive FIR. With a 3 kHz passband this is about 0.72 dB lower than the 2500 Hz value.
- `rx_signal_power`, `rx_noise_power`, and `rx_interference_power`: separately measured components over the surviving transmit interval, excluding receive padding. Interference is not silently counted as desired signal or white noise.

These measurements use known synthetic components, not the modem's estimated SNR. Finite noise realizations cause small deviations from the requested value. The receive filter describes audio input to the decoder; it does not establish the transmitted waveform's occupied bandwidth.

### Earlier result files

Schema-version-1 runs used total sampled-band noise as their SNR reference and had no receive FIR. Their labels retain that meaning. For their white-noise generation, `SNR_2500 = old_snr_db + 9.823 dB`. Applying this arithmetic to the labels does not reproduce reception through the new receive filter. Keep those reports as historical results and use new output directories for schema-version-2 runs.

### Negative-SNR bracket with the calibrated channel

```sh
.venv/bin/python tools/modem_benchmark.py \
  --backends fast_avatar fast_avatar_fec --canvas 32 \
  --profiles awgn --snr-reference-hz 2500 \
  --rx-low-hz 100 --rx-high-hz 3100 \
  --snr-db 9 8 7 6 5 4 3 0 -1 -2 -3 -4 -5 -6 \
  --repeats 1 2 3 --trials 20 \
  --output work/first-comparison/calibrated-snr-32
```

For 32×32 eight-color cards, two and three full copies exceed the five-second design target. `within_5s` labels whether generated transmit audio meets that limit; it excludes receiver padding and does not include unmodelled radio/PTT delays. Repetition runs remain useful diagnostics but do not qualify as five-second operating choices.

`tools/two_client_loopback.py` uses the same `tools/synthetic_channel.py` noise calibration and receive FIR. Its `--snr-reference-hz`, `--rx-low-hz`, `--rx-high-hz`, and `--rx-filter-taps` options have the same defaults. Loopback noise is referenced to input waveform power before synthetic impairments; for `--audio-loopback`, this is captured audio power, not an independently calibrated RF transmit level. The JSON reports the reference and filter explicitly. If the optional example JSON is absent, the script uses the built-in example card.

Numerical regression checks for the shared channel can be run with:

```sh
.venv/bin/python -m unittest discover -s tools -p test_synthetic_channel.py -v
```

The completed 2026-10-01 rerun contains 1680 trials at 84 settings: both native backends, 1/2/3 copies, and 20 seeds at each listed SNR. See [the calibrated report](work/first-comparison/calibrated-snr-32/report.html) and [recovery curves](work/first-comparison/calibrated-snr-32/recovery-curves.svg). Neither backend recovered a complete card at 0 through −6 dB (0/840 attempts). At +6 dB, Resilient recovered 9/20, 17/20, and 17/20 for 1/2/3 copies. Requested-versus-measured reference SNR deviated by at most 0.091 dB. These results use one patterned card, not a diverse image corpus.

The saved campaign driver, `work/first-comparison/calibrated-snr-32/run_bracket.py`, shards independent backend/copy cases across four processes with BLAS threads limited to one, then merges reports. Its source snapshot and per-case invocation/logs are retained. Decode timings from this campaign reflect CPU contention and should not be compared directly with standalone timing from earlier serial runs. The benchmark CLI itself remains sequential.

| Profile | Impairment before noise |
| --- | --- |
| clean | No impairment or noise |
| awgn | White Gaussian noise only |
| tuning | +80 Hz carrier offset, +100 ppm sample clock |
| fading | Sinusoidal amplitude fade at 0.7 Hz, minimum gain 0.05, seeded phase |
| echo | One delayed copy at 2 ms and 0.6 amplitude |
| interference | 1500 Hz tone at the same RMS as the desired transmit signal |
| clipping | Hard limit at 40% of transmit RMS |
| late-entry | Receiver misses first 15% of the entire transmission |
| mixed | +40 Hz, +50 ppm, 0.5 Hz fading with minimum gain 0.2, 1 ms echo at 0.4 amplitude |

These are controlled stress cases, not calibrated ionospheric channel models. Profiles are defined in `PROFILES` for easy extension. Positive clock error shortens the received waveform. One second of leading/trailing channel noise allows acquisition and receiver flushing. Seeds are shared across backends; waveform lengths differ, so this is reproducible but not identical temporal exposure. Clean trials are deterministic repetitions and their interval should not be interpreted as independent channel evidence.

## Reading results

- `full_recovery`: callsign, grid, dimensions and every expected pixel match, with full reported coverage. The expected pixels use the same palette mapping as the app.
- `verified_recovery`: full recovery plus the receiver's `exact` integrity result. The unchecked fast backend can match perfectly while having zero verified recovery.
- `false_verified`: receiver reports integrity success but the received card does not match ground truth. Inspect every occurrence.
- Pixel accuracy counts missing pixels as incorrect; coverage counts received pixels independently of correctness.
- Goodput is the expected packed pixel bits in fully recovered cards divided by **all transmitted airtime**, including failures. It excludes metadata bits and receive padding. It measures useful image delivery, not physical bitrate.
- Decode seconds measures wall time for the complete buffered decode on the recorded machine. It is not live UI latency. Encode timing is measured once and reused across channel trials.
- Wilson 95% intervals show uncertainty in full recovery rates. Use at least 20 independent noisy trials for exploration, and substantially more near a decision threshold.

Compare equal cards and copy counts first. Then compare practical operating choices such as one fast burst versus several resilient copies using reported airtime. A future fixed-airtime scheduler would be needed to enforce exactly equal airtime budgets. Each decode starts from a fresh receiver and processes all copies together; this tool does not measure time to first preview or time to first complete card.

`results.json` records the Git revision, dirty-worktree status, benchmark script hash, platform, NumPy/Python versions, invocation, SNR reference, receive filter, channel parameters and waveform hashes. Preserve the source checkout and environment lockfile with important results; a revision alone does not reproduce uncommitted edits.

## Recorded on-air paths

Record receive audio and preserve the exact exported transmitted card. Use mono or stereo signed 16-bit PCM WAV; stereo is averaged. The decoder accepts the WAV's sample rate. Captures should include the entire planned receive window with enough trailing audio to flush reception. Include failed/no-signal attempts, not only successful captures.

Create a JSON manifest with paths relative to its directory:

```json
[
  {
    "backend": "fast_avatar_fec",
    "path": "40m-night-300km",
    "wav": "rx/resilient-attempt-001.wav",
    "card": "cards/transmitted-card.json",
    "repeats": 3,
    "airtime_seconds": 12.5,
    "snr_db": null,
    "notes": "Illustrative entry: replace airtime with actual TX duration; record UTC, stations, band, power, antennas, filter width, AGC and receiver gain"
  }
]
```

```sh
.venv/bin/python tools/modem_benchmark.py \
  --captures measurements/manifest.json --output work/field-comparison
```

The full manifest, including extra measurement notes, is retained in run metadata. `airtime_seconds` must be actual transmit duration, not recording length. SNR may remain null; if supplied, document how it was measured and optionally add `snr_reference_hz` to the entry. An omitted reference remains unknown; capture SNR is never assumed to use 2500 Hz. Replay does not add synthetic impairments or apply the synthetic receive FIR. Grouping uses source, modem, path label, SNR/reference, copy count and expected-card hash; give materially different operating setups different path labels. Trials at nearby times can be correlated, so a statistical interval does not establish independence.

For useful on-air comparisons, alternate modem choices in randomized blocks over the same path, card, transmit power and receiver settings. Repeat across time of day and bands, preserve every attempt, and record UTC plus setup notes. Compare recovery, airtime and partial image delivery before selecting a default.

`--save-audio` saves synthetic receive WAVs for inspection. WAV output clips samples outside ±1 and quantizes to 16 bits; in-memory benchmarks use float audio and their hashes identify that float input. Very noisy saved WAVs may therefore decode differently from the reported float trial. Saved files are not automatically replay manifests.
