# Experimental grouped FSK family

Enable **Station settings → Options → Experimental modems**, then select **8-FSK · grouped LDPC copies** or **16-FSK · grouped LDPC copies**. Set **Copies per card** to 1–20. The selected copy count and displayed airtime apply to the entire group. Receive-all recognizes the protected opening header and selects the matching decoder.

```
[protected v2 group header][native payload 1][native payload 2] … [native payload N]
```

Each native payload includes short training, guards, and distributed pilots. Copies are independently aligned, frequency-corrected and converted into soft bit evidence; evidence is combined before LDPC decoding. The original whole-card CRC32 and the common content digest must both match before completion. The application card/message format is unchanged.

The opening marker lasts 1.6 seconds. There are no common headers or trailers between payloads. Payloads reuse the existing 8FSK/16FSK LDPC formats; this experiment changes framing, not their code rate or data content. It sends identical copies, not additional parity. Existing per-copy-framed modes remain selectable with their original stable IDs.

| Mode | Stable wire ID | Payload seconds | Three copies, old framing | Three copies, grouped |
|---|---:|---:|---:|---:|
| `experimental_8fsk_grouped` | 14 | 5.005 | 24.615 s | 16.615 s |
| `experimental_16fsk_grouped` | 15 | 5.640 | 26.520 s | 18.520 s |

## Version 2 group header

The waveform, training, byte layout, interleaver and RS parity remain those of the common header. Version 2 defines the 32-bit length field as:

- Bits 31–27: copy count minus one (0–19 valid).
- Bits 26–0: samples per payload at 48 kHz, with the existing 1800-second maximum.

Only stable group IDs 14 and 15 may announce version 2. The trailer bit is forbidden. RS protection and CRC32 cover mode, version, count, length and the 16-byte content digest. Bounded soft Chase recovery tries alternatives at uncertain GF64 positions when hard RS decoding fails; an alternative must still pass RS, CRC, version and mode checks. For the fixed grouped formats, recovery also tries 40 known-prefix hypotheses (two modes × 20 copy counts), constraining magic, version, mode and payload length while leaving the content identity unknown. Every result must pass the original CRC32 and native card integrity checks. The v2 payload sizes are fixed at 240240 and 270720 samples at 48 kHz. Header acquisition limits soft attempts to the 16 highest-scoring candidates.

Copies derive their nominal boundaries from the admitted opening header. Their own training refines alignment independently. The grouped receiver widens its local timing search with copy index to accommodate accumulating clock error up to approximately ±200 ppm; there is no new pilot airtime. The existing receiver also searches constant clock corrections through ±160 ppm. These bounds are implementation search ranges, not guaranteed channel tolerance.

Live capture snapshots replace earlier partial copies rather than adding duplicates. The existing bounded cache retains independently admitted copies and keeps different content identities separate. Truncated groups can decode from their available complete copies. A receiver may finish before all copies arrive; the transmitter still sends the configured group.

There is no late-entry guarantee. Losing the opening header prevents explicit grouped-mode decoding. Receive-all may still recover an intact native payload through its historical native fallback, but cannot rely on that to reconstruct the group. Old receivers do not understand version 2 grouped framing. This mode requires updated software at both ends for reliable group combining.

## Reproducible verification

```
OPENBLAS_NUM_THREADS=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests
OPENBLAS_NUM_THREADS=1 QT_QPA_PLATFORM=offscreen .venv/bin/python tools/verify_experimental_burst_link.py --modes experimental_8fsk_grouped experimental_16fsk_grouped --receive-all --copies 3 --output work/grouped-fsk/live-link.json
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/benchmark_hf.py --modes experimental_8fsk_grouped experimental_16fsk_grouped --profiles awgn hf_mild hf_moderate hf_severe --snr-db -6 --trials 3 --repeats 8 --output work/grouped-fsk/hf-eight-copies
```

Compare both total airtime and copy count. A shorter waveform samples a different portion of a fading process, even with the same simulator seed. HF results are synthetic offline audio, and the Qt/UDP test exercises the app without sound devices or RF. Neither proves on-air performance.

## Initial results

The three-card −6 dB comparison with eight copies recovered 1/3 AWGN cards with 8FSK and 3/3 with 16FSK, for both old and grouped framing. Neither framing recovered the moderate-fading sample. Grouped airtime was 41.64 and 46.72 seconds, versus 65.64 and 70.72 seconds with per-copy common markers. Twelve comparison noise-only controls produced no false verified cards or backend errors. These small synthetic samples establish airtime savings with comparable recovery in these cases, not general RF sensitivity.

Both grouped modes also passed the real two-window Qt/UDP receive-all test with three copies and exact verified images. Recorded WAVs passed both selected and automatic application decode routes. Two cards per mode passed 20 dB AWGN with 60 Hz carrier offset and 160 ppm clock error. Tests explicitly cover damaged headers, checksum rejection, invalid copy counts, partial snapshots, identity mismatch, missing opening headers, noise rejection, and skipping grouped decoders during blind native acquisition.

Artifacts are under `work/grouped-fsk/`: `validation.json`, `live-link.json`, `comparison-final/report.md`, `hf-eight-copies/report.md`, and `tuning-clock/report.md`. Validation used a passing 137-test full suite, followed by 9 final grouped/application tests and 13 strict header tests for the last changes.
