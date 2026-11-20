# Reception confidence: show what we heard

Pixel QSO intentionally shows **raw, provisional artwork**, even when decoded
pixels may be wrong. The receiver does not have to wait for whole-card integrity
before drawing a picture. Subsequent copies can improve or replace uncertain
pixels; a card is only final after its decoder/assembler reports whole-card
verification.

## Three different facts

| State | Meaning | How it is displayed |
| --- | --- | --- |
| Heard | A pixel was decoded and is available to show, but may be wrong | Raw color, possibly tentative |
| CRC-verified region | The receiver verified a portion of the artwork | Raw color, marked as checked |
| Whole card verified | The assembler confirms complete image integrity | **VERIFIED** status on the existing card |

**100% heard is not the same as 100% verified.** The UI can therefore show
`100% heard · 0% CRC` on an image that appears complete but is not trustworthy.
It never infers whole-card verification solely from an `exact` field or from
having all expected pixels. A whole-card verification event is the gate.

By default, gallery tiles and the live preview show the decoder's raw colors;
missing pixels use the receiver's existing neutral checkerboard. Enable
**Mark unverified pixels** in Operate to temporarily tint heard-but-unverified
pixels amber. Verified pixels are unchanged; missing pixels are not made up.
The toggle changes only display copies, never the stored image, modem output,
CRC result, or later retransmission behavior. While that quality overlay is
active, gallery animation is temporarily paused to preserve the confidence
map; disabling it resumes animation.

New receptions and the transition from provisional to verified receive a short
visual arrival pulse. Subsequent updates reuse the same gallery tile when the
receiver's card identity remains the same. Provisional artwork remains
available in the saved Recent Cards/Stations Heard collection.

## Benchmarking weak signals

The project already contains a real reproducible modem benchmark harness:

- `tools/benchmark_fsk_receiver.py` compares current and historical receiver
  sources through the same TX/channel/scoring harness.
- `tools/benchmark_hf.py` runs seeded AWGN/HF-fading tests with optional
  frequency offset, drift, clock error and replayed WAV captures.
- `tools/report_hf.py` renders the resulting JSON into a readable report.
- `docs/FSK-RECEIVER.md`, `docs/SNR.md` and `tools/HF_BENCHMARKS.md` document
  previous trials, reference SNR conventions and reproduction commands.

Example **synthetic** sweep (not a claim about hardware performance):

```sh
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/benchmark_fsk_receiver.py \
  --modes experimental_8fsk_ldpc experimental_16fsk_ldpc \
  --profiles awgn hf_mild hf_moderate \
  --snr-db -9 -6 -3 0 --trials 10 --repeats 1 \
  --output work/reception-quality/snr-sweep
.venv/bin/python tools/report_hf.py \
  work/reception-quality/snr-sweep/results.json \
  --output work/reception-quality/snr-sweep/report.md
```

Benchmark JSON now retains `heard_percent` and `crc_verified_percent` in
addition to `verified`, `exact_match`, `false_verified`, `coverage_pixels`,
receiver stage and runtime diagnostics. The Markdown report includes a
separate partial-coverage table and shows how many attempts actually produced
coverage data. A failed acquisition can leave these percentages unknown; no
value is fabricated when there is no card report.
These are coverage metrics, **not** proof of pixel correctness: the expected
card/identity and whole-card CRC checks remain the sole verified-pass criteria.

Always compare against the same seeds, SNR, card pattern, copy count, drift and
channel profile. Keep a noise-only false-verification control. If comparing a
prior decoder snapshot, use `--receiver-source` and a separate output directory
as described in `docs/FSK-RECEIVER.md`.

This UI work does not change over-the-air modulation, bit decisions, forward
error correction or remote acknowledgment semantics. Physical radio testing
should happen separately from the noVNC development container.
