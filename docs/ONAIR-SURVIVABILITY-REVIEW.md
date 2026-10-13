# Experimental modem survivability review

## Scope and evidence

Review of source at `dd76bbf`, with no modem implementation or wire-format changes. New formats are permitted for proposals. No RF transmission or deployment was performed. This review concentrates on the four experimental burst modems and shared framing, not a complete sensitivity comparison of Resilient 25/50/100, legacy, and Data2G.

Fresh baseline: `work/onair-survivability-review/baseline/results.json` (gitignored). Four modes, two example-card trials (seeds 1000–1001), two framed copies, 0 dB input SNR referenced to 2500 Hz, TX RMS 0.1. HF profiles are illustrative two-path Gaussian-scatter simulations, not certified channel models. Mild uses 0.5 ms delay and 0.1 Hz Doppler sigma; moderate uses 2 ms and 0.5 Hz. Noise is added after fading; input SNR is not instantaneous received SNR. Different waveform lengths sample different fading histories and use different total energy, even with the same seed.

| Mode | AWGN | Mild fading | Moderate fading |
|---|---:|---:|---:|
| 8FSK LDPC | 2/2 | 2/2 | 0/2 |
| 16FSK LDPC | 2/2 | 2/2 | 0/2 |
| 8FSK outer RS | 2/2 | 0/2 | 0/2 |
| Equalized QPSK | 2/2 | 2/2 | 2/2 |

Counts require exact pixels, identity, and verified card assembly. Eight additional noise-only controls produced no false verified cards; all 32 attempts had zero backend errors. The failed LDPC cases reported nonconvergent parity checks. Outer-RS failures returned activity with zero pixel coverage and one or two announced copies. Do not attribute these failures exclusively to acquisition, timing, or coding without per-stage probes. QPSK is a useful positive control here, not a proven universally superior HF modem.

Targeted checks: 13 experimental-burst tests and four HF-channel tests passed. Qt/audio-device discovery emitted ALSA/JACK warnings; the suite still passed. The full suite was not run.

## Prioritized work

### 1. Preserve checked outer shards across copies (receiver-only)

`experimental_burst_modem.py:583` builds a new checked-shard dictionary for every attempt; `:879` tries individual and combined soft evidence and retains the best partial report, rather than the union of checked shards. Complementary successful shards can therefore be discarded between attempts.

Retain checked shard bytes by protected content identity and shard index, reject conflicting values, and attempt outer recovery once enough distinct shards exist. Preserve whole-card CRC and protected content validation. Keep soft combining as an additional recovery route. First regression: individually incomplete copies with complementary checked shards; add conflicting identities/values and alternative timing hypotheses. This is a source-grounded opportunity, not a measured gain from the baseline.

### 2. Prototype a slower orthogonal 8FSK profile (new wire format)

Current formats (`experimental_burst_modem.py:65`) use 600 baud/300 Hz spacing, 400 baud/150 Hz spacing, and 400 baud/200 Hz spacing. Symbol lengths are 1.67–2.5 ms, comparable to the moderate profile's 2 ms echo delay. The FSK detector integrates individual symbols without an equalizer (`:242`). Current spacing/symbol-rate ratios are 0.5, 0.375, and 0.5, so adjacent templates are not orthogonal over its symbol window. This is a design tradeoff, not proof of a bug.

Start with 8FSK at 200 baud and 200 Hz spacing; compare a 100-baud variant after that. Longer symbols reduce relative delay spread and integer spacing/rate makes ideal rectangular-window tone correlations orthogonal. Neither guarantees recovery in fading. Lower rates cost airtime, increase exposure to fades/clock drift, and can reduce Doppler tolerance if spacing shrinks. Compare equal-airtime and equal-energy budgets, not equal copy counts. Keep separate stable IDs; do not silently alter existing modes. Tone-center span is not occupied bandwidth.

### 3. Independently recoverable mini-frames and repeated identity anchors (new format)

Ungrouped framing already supplies protected prefix/trailer markers; grouped framing has just one opening marker (`modem_envelope.py:155–176`). Do not generalize the single-header weakness to all modes.

Specify bounded mini-frames with local synchronization, protected mode/version, content identity binding, frame/shard index, payload length and redundancy version. Repeat full identity anchors periodically so a receiver can join late or recover after losing the start. Short identifiers require explicit collision handling; training alone cannot establish identity. Keep final whole-card integrity mandatory.

Avoid blindly attaching today's 1.6-second common marker to every tiny shard: that would dominate airtime. Prototype compact protected headers and measure acquisition strength versus overhead. This direction is especially valuable for grouped formats and burst erasures.

### 4. Send new parity rather than only identical repeats (new format)

Current FSK sends the first 8192 positions of a 14080-bit mother-code output (`experimental_burst_modem.py:115–122`, `weak_signal_modem.py:15`). RX treats untransmitted positions as erasures (`experimental_burst_modem.py:482–499`). Existing repeat framing transmits identical payloads.

Specify redundancy versions with explicit deterministic codeword-position mappings. Later frames can supply previously omitted parity and selected repeats, retaining soft combining for repeated positions. Validate the LDPC mother-code mapping/rate-matching schedule before committing to a specification. Parity-only frames may not decode independently; self-identifying frames and retained evidence are prerequisites.

For the shard family, extra outer parity rows are another option, but the current 12-data/4-parity matrix and receiver index limit are fixed (`experimental_fec.py:144–170`), so this also needs a versioned format. Do not imply unlimited fountain behavior from the current RS code.

### 5. Local channel reliability and timing (receiver experiments)

FSK estimates one noise/signal pair per burst (`experimental_burst_modem.py:444–464`); QPSK uses one final pilot-error variance (`:706–710`). Test local pilot-derived reliability weighting, weak-evidence erasures, and robust interference handling so fades or interferers do not contribute disproportionately confident evidence. Also test piecewise timing tracking: FSK currently selects one start/clock hypothesis per burst over a ±160 ppm grid (`:252–299`).

These are hypotheses. Preserve the current static path as fallback and bound CPU cost. Do not weaken CRC, identity, or FEC acceptance to manufacture gains. Known good evidence must not be counted twice through alternative timing hypotheses.

## Interleaving and QPSK cautions

LDPC already has whole-codeword interleaving. Outer-RS words are contiguous (`experimental_burst_modem.py:131–141`); shallow interleaving and deterministic ordering/tone-map changes between redundancy versions are worth comparing. Deep interleaving is not automatically better: it can spread a recoverable few-shard erasure into failures across all shards.

Keep QPSK as a serious candidate: it passed all six signal trials here and has a trained/adaptive 21-tap equalizer (`experimental_burst_modem.py:660–715`). Test recovery after fades, impulsive interference, clipping/ALC and timing discontinuities before favoring it for radio operation. Slower QPSK or independently trained subframes are separate design experiments, not simple constant changes.

## Acceptance plan

1. Establish matched saved-capture and synthetic baselines; keep existing exact-card regressions.
2. Implement checked-shard retention first, with complementary-copy and identity/conflict tests.
3. Compare current FSK against slower orthogonal FSK before combining it with new framing/coding; change one factor at a time.
4. Add mini-frame acquisition/late entry, then redundancy versions as separate experiments.
5. Expand beyond this small baseline: SNR sweeps, many independent seeds/cards, fading/Doppler/delay, selective notches and CW interference, impulsive noise, clipping/AGC, clock errors, sample slips and lost prefixes. Test random/poorly compressible cards as well as example artwork.
6. Score header acquisition, checked shards, whole-card recovery, false accepts, CPU/real-time latency, airtime and energy separately. Use fixed airtime budgets and report uncertainty.
7. Finally validate user-operated RF transmissions with synchronized audio and exact mode/start/end markers. No synthetic result alone establishes on-air survivability.

Reproduce baseline:

```
OPENBLAS_NUM_THREADS=1 QT_QPA_PLATFORM=offscreen .venv/bin/python tools/benchmark_hf.py --modes experimental_8fsk_ldpc experimental_16fsk_ldpc experimental_8fsk_outer_rs experimental_qpsk_equalized --profiles awgn hf_mild hf_moderate --snr-db 0 --trials 2 --repeats 2 --card-pattern example --output work/onair-survivability-review/baseline
```
