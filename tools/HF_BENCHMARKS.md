# Offline HF modem benchmarks

Run from the repository root with the existing virtualenv:

```sh
.venv/bin/python tools/benchmark_hf.py --modes experimental_qpsk_equalized fast_avatar_fec_v4 --profiles awgn hf_mild hf_moderate hf_severe --snr-db -6 0 6 12 18 --trials 10 --output work/hf-sweep
```

This invokes the real local TX waveform encoder, passes audio through the channel, then invokes the local RX decoder and card assembly. Success requires receiver verification plus exact identity and all 1024 pixel indices. Noise-only controls detect false acceptance. Each trial uses seeded random card pixels. Modes must support local waveform encoding/decoding; the Data2G host/KISS transport is not connected by this harness.

`results.json` includes individual attempts, diagnostics, channel settings, arguments and summaries. `summary.csv` compares recovery counts and decode CPU time. Add `--save-wavs` to retain float32 RX captures. `--repeats 3`, `--cfo-hz 25` and `--clock-ppm 50` add repeated transmissions, receiver tuning offset and clock error. Defaults are one repeat, zero tuning/clock error. Larger sweeps may take substantial CPU time; recovery counts from small samples are not reliability estimates.

For any other modem with WAV TX output and WAV RX input:

```sh
.venv/bin/python tools/hf_channel.py tx.wav rx.wav --profile hf_moderate --snr-db 6 --seed 42 --cfo-hz 25 --clock-ppm 50
```

Feed `rx.wav` into that modem's decoder. Inputs must be mono; integer PCM and floating point WAVs are supported. Output is floating point WAV to avoid clipping or silently scaling noise. A `.channel.json` sidecar records parameters. Hardware players may clip values outside ±1; this workflow is for offline decoding.

The channel uses an analytic audio signal and two equal-ensemble-power, independently fading paths. Complex Gaussian gains have approximately Gaussian Doppler power spectra, generated at a lower rate and interpolated to the audio sample rate. Total ensemble path power is unity; individual captures retain their fades rather than being normalized. Echo tails and 0.3-second noise guards are retained. CFO affects the signal before receiver noise; clock error resamples the signal before noise is added.

| Preset | Path delay | Doppler standard deviation per path |
|---|---:|---:|
| awgn | 0 | no fading |
| hf_mild | 0.5 ms | 0.1 Hz |
| hf_moderate | 2 ms | 0.5 Hz |
| hf_severe | 4 ms | 2 Hz |

These are illustrative severity presets inspired by the Gaussian-scatter tapped-delay architecture described in [ITU-R F.1487](https://www.itu.int/rec/R-REC-F.1487-0-200005-I/en), not certified implementations of its named test profiles. Doppler values here explicitly mean Gaussian **standard deviation**, avoiding ambiguity with other spread conventions. Delay is rounded to an audio sample. Low-rate interpolation and finite Gaussian filtering approximate the requested spectrum.

SNR is input waveform mean power divided by noise power in a 2500 Hz reference bandwidth, before fading. Real white noise variance is `input_power * 10**(-SNR/10) * sample_rate/(2*reference_bandwidth)`. The WAV tool allows `--bandwidth-hz`; the card sweep fixes 2500 Hz. All modes use TX RMS 0.1; different airtimes still imply different transmit energies. Seeds are paired across profiles/SNR/modes, but differing capture lengths mean differing noise realizations. There is no receiver bandpass, AGC, clipping, interference or impulse noise model.

Verification:

```sh
.venv/bin/python -m unittest tests.test_hf_channel
.venv/bin/python tools/benchmark_hf.py --modes experimental_qpsk_equalized --profiles awgn hf_moderate --snr-db 30 --trials 1 --save-wavs --output work/hf-smoke
```

This verifies synthetic offline audio and card recovery. It does not exercise audio devices, a packaged GUI, a Data2G host or RF propagation.

The default sweep now selects every mode marked experimental in the local mode registry and uses −6 dB. It includes `standard` through the legacy compact-packet adapter. A shared fixed palette preserves identical source indices across modem formats. The table distinguishes receiver verification from exact source matches for the unchecked raw mode.

Create a Markdown table from a completed run:

```sh
.venv/bin/python tools/report_hf.py work/hf-sweep/results.json --output work/hf-sweep/report.md
```

All experimental backends now support 1–20 copies with common protected framing.
`--repeats` applies uniformly, and reported airtime includes the 3.2-second framing overhead per copy.
See [copy framing and receiver hardening](COPY_COMBINING.md) for wire compatibility and live-cache behavior.

The default mode catalog also includes `experimental_8fsk_grouped` and `experimental_16fsk_grouped`. Their airtime is one 1.6-second header plus the selected number of native LDPC payloads; see [grouped FSK](../docs/GROUPED-FSK.md).

For lossless compression tests, use `--card-pattern example`, `flat`, or `stripes`. The default `random` corpus generally exercises raw fallback. The new `experimental_8fsk_compressed` and `experimental_16fsk_compressed` modes appear in the default mode catalog; see [compressed FSK](../docs/COMPRESSED-FSK.md) and `tools/benchmark_compression.py` for measured byte sizes and waveform airtime.

Use `--drift-hz-per-second` to apply a linear frequency ramp in addition to `--cfo-hz`. Drift is measured from the beginning of the guarded capture, before sample-clock interpolation. The native experimental FSK modes also support `--native-payload` to isolate payload tracking from common-header acquisition; group/compressed modes require their protected headers and reject that flag. The report records both options. See [FSK receiver validation](../docs/FSK-RECEIVER.md) for matched before/after results and source-snapshot replay commands.
