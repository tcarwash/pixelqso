# FSK receiver acquisition, timing retries and frequency tracking

These receiver changes use the existing training, pilots and on-air formats. They add no transmission time, new mode IDs or wire versions. The shared common-header changes apply to all announced local experimental modes. Timing and frequency changes cover native experimental 8FSK LDPC, 16FSK LDPC and 8FSK outer RS, including the grouped and compressed LDPC families.

## Priority and implementation

1. **Opening header acquisition.** Detect the known preamble and also its combination with ten magic-byte tone symbols already scattered through the RS body. This gives acquisition evidence outside the preamble during a fade. Retain the original coarse hypotheses and refine promising starts by fractional-symbol timing and frequency offset. Soft RS recovery ranks legal version-2 and version-3 prefixes, including all compressed coding profiles, and tries at most twelve aided prefixes. Identity and CRC bytes are never guessed. RS, CRC32, supported mode/version, sample-length and copy-count checks remain mandatory.
2. **Timing candidates.** Keep three distinct pilot-ranked start/clock hypotheses instead of discarding every candidate except the top one. Try the original single/cumulative-copy policy first, followed by at most twelve alternate decode attempts. Each attempt chooses at most one interpretation of a physical transmission: alternate timing hypotheses cannot masquerade as extra copies. Identity isolation remains in force.
3. **Frequency drift tracking.** Fit a bounded linear residual frequency model from known training and distributed pilots. The noncoherent receiver uses correlation energies; it does not require phase continuity through fading. Search up to ±40 Hz/second drift and ±30 Hz residual offset, limited to a 240 Hz modeled correction over a payload. Retain static hypotheses first and retry tracked candidates after failure. Announced payloads also retain the narrow ±20 Hz local acquisition before trying a wider ±120 Hz coarse search with fine refinement, accommodating offset accumulated by later copies.

Retries add receive CPU work. Their budgets are fixed, and the existing LDPC/RS, original packet CRC, metadata and protected content-identity checks still gate verified card release. The linear model handles a slow frequency ramp within each copy; arbitrary rapid Doppler or severe multipath can still defeat acquisition or decoding.

## Matched synthetic results

The −6 dB comparison uses the same example cards, seeds 1000–1002, six copies, TX RMS 0.1, and 2500 Hz SNR reference as the earlier compressed-family validation. Every count below requires exact identity and pixel recovery plus receiver verification.

| Mode | Channel | Before | Final receiver |
|---|---|---:|---:|
| 8FSK compressed | AWGN | 0/3 | 0/3 |
| 8FSK compressed | Mild fading | 0/3 | 1/3 |
| 8FSK compressed | Moderate fading | 0/3 | 0/3 |
| 16FSK compressed | AWGN | 3/3 | 3/3 |
| 16FSK compressed | Mild fading | 0/3 | 1/3 |
| 16FSK compressed | Moderate fading | 0/3 | 0/3 |

Overall exact verified recovery improves from **3/18 to 5/18**. Header misses drop from **10/18 to 9/18**; timing retries produce the two additional mild-fading decodes. Drift tracking preserves those successes in this no-ramp corpus. Stage results are in `work/fsk-receiver-20261006/header`, `timing`, and `final`; the before results are `work/compressed-fsk/final-six/results.json`. The six-copy example transmissions remain **16.32 seconds for 8FSK** and **18.28 seconds for 16FSK**.

For native payloads with random pixels, one copy, +6 dB AWGN, 40 Hz/second drift and seeds 1000–1001:

| Mode | Before | Final receiver |
|---|---:|---:|
| Native 8FSK LDPC | 2/2 | 2/2 |
| Native 16FSK LDPC | 0/2 | 2/2 |

The native 16FSK successes select the tracked receiver with fitted slopes of 40 and 35 Hz/second. A separate protected compressed/raw-fallback check at +6 dB and 20 Hz/second drift passes 2/2 on each modem both before and after. These isolate different receiver conditions; they are not a sensitivity sweep.

A controlled header-only test erases the complete 32-symbol preamble and adds 0 dB AWGN, seed 31. The old receiver advertises no copy; the new receiver acquires the correct protected mode, identity and copy count using the surviving scattered magic tones. Its available capture contains only a prefix of the first payload, so this verifies header acquisition rather than image recovery.

Noise-only controls in the final six-copy benchmark and both drift comparisons produce zero false verified cards and no backend errors. These are small, deterministic synthetic sets, with no RF or sound-device validation.

## Application and regression checks

Both compressed modes pass the real Qt transmit/UDP/receive-all paths with six copies and exact pixels: `work/fsk-receiver-20261006/live/live-link.json`. The example airtime is unchanged. Dedicated receiver tests cover recovery beyond hard RS capacity, a completely faded preamble, alternate-copy identity isolation, retry budgets, and exact native 16FSK decoding under both positive and negative drift. Channel tests independently check chirp direction, reproducibility and SNR metadata.

The complete suite passes **156 tests** after the receiver changes. The consolidated report and machine-readable verification record are `work/fsk-receiver-20261006/report.md` and `validation.json`; the suite log is preserved alongside them.

## Reproduce

```sh
OPENBLAS_NUM_THREADS=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/benchmark_fsk_receiver.py --modes experimental_8fsk_compressed experimental_16fsk_compressed --profiles awgn hf_mild hf_moderate --snr-db -6 --trials 3 --repeats 6 --card-pattern example --output work/fsk-receiver-replay/final
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/benchmark_fsk_receiver.py --modes experimental_8fsk_ldpc experimental_16fsk_ldpc --native-payload --profiles awgn --snr-db 6 --trials 2 --repeats 1 --drift-hz-per-second 40 --output work/fsk-receiver-replay/drift
.venv/bin/python tools/report_hf.py work/fsk-receiver-replay/final/results.json --output work/fsk-receiver-replay/final/report.md
```

To replay the pre-change receiver through the same TX/channel/scoring harness, add `--receiver-source work/fsk-receiver-20261006/baseline-src` to either benchmark command. The saved directory contains the three original receiver modules. `timing-src` preserves the intermediate timing-only receiver. Keep output directories separate. Receiver wall-time measurements depend on machine load; reliability and fixed transmission airtime are the comparisons above.
