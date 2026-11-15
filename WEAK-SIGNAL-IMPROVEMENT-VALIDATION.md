# Weak-signal receiver experiments

Paired QPSK comparisons use the committed `HEAD` receiver and the working-tree
receiver on identical seeded captures. The simulator defines SNR over its
2.5 kHz reference bandwidth. These are synthetic results, not on-air claims.

## Results

| Idea | Implementation or experiment | Evidence and disposition |
|---|---|---|
| SNR-weighted MRC | Keep combining calibrated demapper LLRs by addition. | Each QPSK block LLR already includes its pilot-noise estimate; multiplying by SNR again double-counts reliability. Unit coverage verifies direct LLR addition. Do not apply a second SNR weight. |
| Per-block QPSK noise | Shrink local pilot variances toward the global variance on flat channels; transition to local estimates when pilot gain varies. | AWGN at −4 dB: `HEAD 20/20`, current `20/20`. HF mild: 0 dB `7/10` both, +4 dB `10/10` both. HF moderate: 0 dB `3/10` both, +4 dB `HEAD 6/10`, current `8/10`. Unit tests cover flat-channel shrinkage and fading activation. |
| More LDPC iterations after combining | Keep the 200-iteration budget for multi-copy evidence, with syndrome early exit. | Two-copy QPSK at −6 dB decoded in 12 iterations with both 100 and 200 budgets. At −8 dB both budgets failed. No gain observed in these trials; the larger budget remains available for slower-converging inputs. |
| FSK LDPC bit permutation per copy | Add deterministic per-copy code-bit interleaving and undo it at receive. Advertise it with protected header version 10; retain version 4 block-only decoding. | Both FSK LDPC modes round-trip at copy indices 0 and 1. End-to-end interlaced 8FSK with protected headers verifies. With identical 60% per-block burst erasures in two copies, the unpermuted control fails and bit-permuted combining recovers the exact packet. Direct noisy `decode_copies` trials at 4, 6, and 8 dB did not decode in either the permuted or unpermuted control path. |
| Lower distributed-pilot threshold | Compare the proposed `.35` to `.25` threshold on noisy timing-echo captures. | Pilot coherence was about `.22–.25` at −10 dB and `.33–.37` at −6 dB. Lowering to `.25` admitted more candidates at −6 dB but produced no exact recovery in 3/3 seeds at −10, −6, or −4 dB. Keep `.35` for now. |
| FSK sub-Hz carrier refinement | Prototype a final 0.5 Hz pass and compare with the 2 Hz pass. | At 4 dB AWGN, 5/5 exact decodes for both versions at CFO 0.3 and 1.3 Hz. The fine fit improved the 1.3 Hz estimate but moved the 0.3 Hz estimate farther from truth. Reverted the refinement; no packet gain demonstrated. |
| Outer RS shard erasures | Use absent or unchecked shards as erasures in the existing 12-of-16 outer recovery. | Existing round trips pass with four missing data shards and with complementary checked shards across copies. No additional format change needed. |
| Copy compatibility threshold | Measure aligned LLR correlations for repeated QPSK copies. | At −10, −8, and −6 dB, measured correlations were `.087`, `.137`, and `.205`; the gate was `.025`. The copies were already admitted, so lowering the threshold would not fix those failures. |

## Low-SNR paired checks

Single-burst QPSK in AWGN at −10, −8, and −6 dB produced `0/5` exact decodes
for both `HEAD` and current. At −10 dB, single-burst mild and moderate HF were
`0/3` for both. Two identical QPSK bursts in one capture were `0/5` at −10 and
−8 dB, and `5/5` at −6 dB for both versions.

## Validation boundary

Focused QPSK noise-estimator tests and eight targeted experimental-burst tests
pass, including FSK permutation, versioned headers, legacy v4 decoding, and
outer-RS erasure recovery. `py_compile` and `git diff --check` pass. The full
experimental-burst unittest module also reported five Qt live-worker airtime
assertion failures (each differed by 6.4 seconds); those assertions are outside
these decoder changes and remain unresolved.
