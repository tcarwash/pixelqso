# Experimental orthogonal 8-FSK profile

Stable local experimental modem ID 19, key `experimental_8fsk_orthogonal`, adds an 8-FSK LDPC format at 200 baud with 200 Hz tone spacing, lowest tone 600 Hz, and the existing BG2 soft-LDPC card envelope. It reuses the existing experimental burst acquisition, pilot, CRC32/content identity, copy combining, and common-header machinery; the distinct stable mode ID and training seed make it a separately announced waveform. It does not change existing IDs or silently reinterpret old recordings. Both stations must have a receiver that knows ID 19. Older stations reject the unknown protected mode rather than decoding it as an existing mode.

The purpose is to test slower symbols and integer tone-spacing/symbol-rate ratio (1.0) against the current experimental FSK profiles whose symbol lengths are 1.67–2.5 ms and spacing/rate ratios are 0.375–0.5. The new profile therefore increases symbol integration time and makes ideal rectangular tone templates orthogonal. These are design properties, not proof of an HF sensitivity gain; fading, Doppler, frequency error, spectral occupancy, drift, and extra airtime may offset them.

A small diagnostic synthetic run at 0 dB, 2 copies, two example-card seeds recovered 2/2 orthogonal-profile cards in AWGN, mild, and moderate profiles. Current 8FSK LDPC and 16FSK LDPC also recovered 2/2 in AWGN/mild but 0/2 moderate; outer RS got 2/2 AWGN and 0/2 mild/moderate; equalized QPSK got 2/2 across all three. The new profile airtime was 35.63 s for two copies, versus 16.41 s current 8FSK LDPC and 15.64 s QPSK. TX RMS was normalized equally, not total transmit energy or airtime. This is a very small, confounded signal that justifies more tests, not a claim of improved sensitivity. Results: `work/onair-survivability-review/orthogonal-8fsk/results.json` (gitignored).

Run a better comparison from the project root, using one repeats count per benchmark invocation so airtime/energy can be matched deliberately:

```
OPENBLAS_NUM_THREADS=1 QT_QPA_PLATFORM=offscreen .venv/bin/python tools/benchmark_hf.py --modes experimental_8fsk_ldpc experimental_8fsk_orthogonal experimental_8fsk_outer_rs experimental_16fsk_ldpc experimental_qpsk_equalized --profiles awgn hf_mild hf_moderate hf_severe --snr-db -6 -3 0 --trials 10 --repeats 1 --output work/orthogonal-8fsk/repeat1
```

Repeat with other copy budgets in separate output directories. Compare total airtime/energy, not just success by mode. The new profile remains experimental; no RF validation has been performed.
