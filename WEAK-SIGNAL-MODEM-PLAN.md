# Pixel QSO: five-second weak-signal modem plan

Date: 2026-10-01  
Status: experimental QPSK implementation available; validation results are in [WEAK-SIGNAL-MODEM.md](WEAK-SIGNAL-MODEM.md). Production promotion remains future work.  
Source inspected: `3ae3140e27712176387522735ba8d7ddcc93f0d7`, plus the existing uncommitted benchmark tools and documentation.

## 1. Objective and decisions

Deliver an **exact 32×32 card in one transmission lasting at most five seconds**, at the lowest practical received SNR. Improve the existing production modem first. Introduce a separately selectable experimental backend if receiver improvements cannot deliver the required sensitivity.

The accepted bandwidth budget is **2.5 kHz**. The user has explicitly accepted this as suitable for the IC-7300; measuring the radio's passband to establish that suitability is not a prerequisite. The original 2 kHz limit is superseded.

Use these working requirements:

| Item | Requirement |
| --- | --- |
| Image | Exact 32×32 raster; no resizing, inferred pixels, or lossy reconstruction |
| Initial palette | Eight colors, matching the existing benchmark palette; 16/32 colors are later profiles |
| Information | Pixels, full callsign/grid, card identity, message type, format identification, and integrity checks |
| Transmission | At most 5.0 seconds for the entire attempt, including acquisition, metadata, parity, pilots, ramps, and configured PTT guards |
| Bandwidth | Designed waveform at or below 2.5 kHz; include preamble and transitions in the waveform spectrum check |
| Direction | One-way; no ACK, remote cache, shared card database, or earlier reception required |
| Repetition | Any copies or additional parity must fit inside that same five-second attempt |
| Receiver | Unknown arrival time, phase, and tuning error; ordinary CPU operation through the real application |
| Integrity | Only verified complete cards count toward the primary sensitivity metric |
| Compatibility | Receiver-only improvements preserve production wire version 3; new wire formats get explicit new identifiers |

Eight colors is a design assumption, not permission to silently reduce a user's palette. A card outside an experimental profile's capacity must select another supported profile or report that limitation explicitly.

**Recommended direction:** repair acquisition and make use of soft evidence in the current receiver, then evaluate a 2.5 kHz, strongly coded QPSK burst. Prefer the existing modem if it meets the target with acceptable complexity. Keep the new modem experimental until measured comparisons and application integration justify promotion.

## 2. What the existing results establish

The completed bracketed sweep is in [the benchmark report](work/first-comparison/bracketed-snr-repeats/report.html) and [its machine-readable results](work/first-comparison/bracketed-snr-repeats/results.json).

It used a **16×16, eight-color patterned card**, white Gaussian noise, 20 trials per setting, and one through three copies. It is not yet a 32×32 baseline. Some useful full-card recovery counts were:

| Original benchmark SNR | Fast, 1 / 2 / 3 copies | Resilient, 1 / 2 / 3 copies |
| --- | --- | --- |
| −2 dB | 18 / 19 / 19 of 20 | 20 / 20 / 20 of 20 |
| −3 dB | 13 / 13 / 12 of 20 | 19 / 20 / 19 of 20 |
| −4 dB | 6 / 8 / 7 of 20 | 10 / 11 / 15 of 20 |
| −5 dB | 1 / 0 / 0 of 20 | 5 / 5 / 7 of 20 |
| −6 dB | 0 / 0 / 0 of 20 | 0 / 1 / 0 of 20 |

Twenty trials give coarse estimates. The nonmonotonic results with more copies warrant diagnosis; they do not establish that repetition is intrinsically harmful. Fast's pixel match is also distinct from receiver-verified recovery: that mode has no image integrity check.

### Correct the SNR reference before claiming sensitivity

`tools/modem_benchmark.py` normalizes transmit RMS to 0.1 and adds white noise across real audio sampled at 48 kHz. Its noise reference therefore spans a one-sided **24 kHz Nyquist band**. The report's negative numbers are not negative SNR in a 2.5 kHz receiver reference.

For this specific unfiltered AWGN model:

```text
SNR_2500 = SNR_sampled + 10 log10(24000 / 2500)
         = SNR_sampled + 9.823 dB

SNR_2000 = SNR_sampled + 10.792 dB
```

The old −6 dB setting is approximately **+3.82 dB in 2.5 kHz**, or +4.79 dB in 2 kHz. The earlier runs establish reception below total sampled-band noise power, but not below noise power in the intended channel. This conversion is not a general correction for colored noise, fading, interference, or the receiver's own SNR estimator.

Make **`snr_db_2500` the common comparison reference** regardless of a candidate's occupied bandwidth. Also record signal power, noise PSD, actual sample rate, and occupied bandwidth. This prevents a narrower or wider modem from gaining an apparent advantage merely by changing the reporting reference.

## 3. Payload, time, and realistic sensitivity targets

For an eight-color 32×32 raster:

```text
image bits                         = 32 × 32 × 3 = 3072
metadata + whole-message CRC budget =                384
total information budget           =               3456 bits
average information rate over 5 s  =              691.2 bit/s
```

The 48-byte overhead allowance includes the whole-message CRC and leaves room for full identity fields. It is a proposed experimental payload budget; the current production metadata format remains unchanged. Coding parity, sync, and pilots consume additional channel capacity.

For context, the ideal AWGN capacity relationship is `C = B log2(1 + S/N)`. At 2.5 kHz and 691.2 bit/s, it gives an optimistic threshold of approximately **−6.75 dB SNR_2500**. This is an asymptotic benchmark, not an achievable five-second guarantee. Finite blocks, training, synchronization, fading, and implementation losses require margin. [Capacity reference](https://www.nokia.com/bell-labs/publications-and-media/publications/the-capacity-of-the-band-limited-gaussian-channel/); [finite-blocklength reference](https://people.lids.mit.edu/yp/homepage/data/finite_block.pdf).

Set provisional AWGN goals for a full, independently decoded 32×32 card:

| Milestone | Target at ≥90% verified full-card recovery |
| --- | --- |
| First useful weak-signal result | 0 dB SNR_2500 |
| Main design target | −3 dB SNR_2500 |
| Stretch research target | −4.5 dB SNR_2500 |

Use enough held-out trials that the **lower 95% confidence bound** on success is at least 90% before declaring a target achieved. Publish the measured 50% and 90% recovery thresholds, confidence intervals, and time/energy costs even if these goals are missed. Fading performance gets separate curves; do not reuse the AWGN thresholds as field predictions.

The wider bandwidth supplies more coded symbols, but is not a large fundamental sensitivity gain by itself. For the same 3456 bits and five seconds, the ideal 2 kHz capacity threshold expressed in the same 2.5 kHz reference is about −6.64 dB, only approximately 0.11 dB above the 2.5 kHz result. The practical opportunity is better coding, synchronization, and receiver design.

Lossless raster compression can later turn easy images into additional parity budget. Every profile must still handle an incompressible raster within its declared capacity. Compressed images must reconstruct exactly, and the receiver must know the codec and length from protected fields.

## 4. Stage A — establish a trustworthy 32×32 baseline

**Deliverable:** calibrated benchmark outputs and an explicit failure breakdown, before changing the production receiver.

1. Add an explicit SNR-reference argument and output fields to the benchmark. Keep old results labelled with their original definition. Generate noise from a specified PSD: for real 48 kHz white samples, `sigma² = P_signal × (Fs/2) / (2500 × 10^(SNR_2500/10))`. Numerically check signal and noise separately through a reference filter with measured equivalent noise bandwidth.
2. Separate nominal pre-fade SNR from measured post-channel SNR. Record both signal and noise powers for synthetic channels; interference gets a separate signal-to-interference measure.
3. Use a fixed corpus of exact 32×32 cards: real artwork, text, sparse images, dense color transitions, and seeded random indices. Reserve cards and random seeds for final evaluation. Never give expected pixels or identity to the decoder.
4. Measure actual generated sample counts. At the current profile, eight-color 32×32 Fast is approximately 2.865 s and Resilient is 3.090 s per copy. Two full copies exceed five seconds. A one-copy production baseline fits; a two/three-copy comparison is a diagnostic and must be labelled outside the time budget.
5. Start at +12 through −6 dB SNR_2500 in 2 dB steps, then bracket the transition in 1 and 0.5 dB steps. Use roughly 30–50 trials per cell to screen, 200–500 near candidate thresholds, and a fresh final seed/card set for acceptance. Expand the range only if a threshold is not bracketed.
6. Record acquisition, validated header, individual block checks, full-card verification, partial coverage, decode latency, false acceptance, and exception reasons separately. Include time/frequency estimates and which copy was selected.
7. Add random leading delay, noise-only input, truncated bursts, unrelated signals, wrong identities, and back-to-back cards. Fixed one-second padding and zero frequency offset must not become prerequisites for success.
8. Save representative float receive audio or uniformly scaled PCM with scaling metadata. The existing WAV writer clips at ±1, so its very noisy exports are not faithful replays of the float benchmark inputs.

For power comparisons, publish both equal average transmitted power and equal transmitter peak-power constraints. For unequal burst durations, report total energy as well as airtime; a longer burst at the same average power has more energy. Do not describe that advantage as coding gain alone.

**Gate A:** the SNR calibration, payload sizes, and duration accounting are reproducible, and stage-level counters explain where the production modem loses cards.

## 5. Stage B — improve the production receiver without changing its waveform

The source suggests several specific losses. Their size is unmeasured; implement and evaluate each separately.

### B1. Filter before acquisition decimation

`acquire()` currently uses a sliced 48 kHz waveform to search at about 8 kHz, without an antialias filter. Wideband noise aliases into the acquisition band. Add proper filtering/resampling with compensated delay and preserve enough passband for the tone set plus the tuning-search range. Compare filtered and original acquisition on exactly the same noisy captures.

This is especially relevant to the current full-band AWGN harness. Its benefit on real receiver audio may be smaller because that audio is already filtered; benchmark both input models.

### B2. Keep multiple synchronization candidates and all useful copies

The receiver fits a correlation peak and subsequently demands an exact 24-tone `FRAME_SYNC` in hard decisions. At low SNR, a useful matched-filter detection can be discarded by a single hard sync error. A selected later copy can also leave earlier evidence outside the demodulated span.

Introduce bounded candidate search with timing/frequency scores and protected-header validation. Use correlation-derived frame boundaries rather than an all-or-nothing exact tone match. Preserve samples before and after candidate peaks so all copies can contribute. Keep CRC and format checks mandatory; relaxing synchronization must not mean accepting arbitrary noise.

The current acquisition search is roughly ±20 Hz, while the existing `tuning` and `mixed` profiles inject +80 and +40 Hz. Expand the declared capture range, initially to ±100 Hz, using coarse-to-fine search rather than an expensive fine grid over the entire capture. Exercise sample-clock error and timing tracking across the full card.

### B3. Preserve soft tone evidence

`demodulate_metrics()` already returns all tone energies, but the avatar path immediately reduces them with `argmax`. Carry calibrated likelihoods and reliability estimates through header and block decoding.

For the existing shortened RS(63,61) code, evaluate bounded reliability-guided candidate decoding and a proper error/erasure implementation. There are only two parity symbols: its algebraic limit is `2 × errors + erasures ≤ 2`. A list search can propose alternate inputs checked by CRC; it does not turn this into a strong low-rate code. Bound candidate counts and CPU, and measure false acceptance after any search expansion.

The tones are spaced 200 Hz at 400 symbols/s. Do not assume independent orthogonal noncoherent tone observations when deriving likelihoods. Compare the current energy detector with a waveform-aware detector using the actual tone correlations and continuous phase; retain the simpler detector unless the measured gain justifies complexity.

### B4. Combine before hard decisions where repetition is available

Fast currently votes on decoded pixel indices, and Resilient accumulates already verified blocks. Add alignment and noise-weighted soft-evidence accumulation for copies belonging to the same validated identity. Protect against stale card IDs, inconsistent metadata, and incorrect copy alignment. Track all evidence already consumed so live snapshots are not counted twice.

This helps existing repeated operation and explains the earlier sweep. It cannot make two full 32×32 copies fit into five seconds. Additional parity or partial-copy layouts for that budget require a new wire format and belong in Stage C.

### B5. Attribute the remaining loss

For diagnostics only, compare the real receiver with a receiver given true timing/frequency, and then with one given true frame boundaries. Label these as assisted diagnostic results. The gap estimates acquisition loss; remaining failures expose payload-code or demodulator limits. Only the unaided receiver counts toward targets.

**Gate B:** publish each change's gain and cost at the same signal, power, and duration. Preserve decoding of existing version-3 captures and the production app's live behavior. Prefer production improvements if they reach the main target. If acquisition is working but the weak production code leaves the target out of reach, proceed with the experimental design rather than extending weak parity by repeated full headers.

## 6. Stage C — experimental five-second coded modem

### Primary candidate: shaped QPSK with strong soft-decision FEC

Create a versioned backend such as `experimental_qpsk_5s`, disabled as the default. Use a fixed initial profile so the receiver knows the basic waveform before it can decode metadata.

| Parameter | Initial engineering choice |
| --- | --- |
| Modulation | Coherent QPSK with a defined Gray mapping |
| Symbol rate | 2000 symbols/s |
| Pulse shaping | Root-raised-cosine transmit/receive filters, roll-off 0.25 |
| Nominal shaped bandwidth | `(1 + 0.25) × 2000 = 2500 Hz` |
| Audio placement | Center about 1500 Hz, nominal spectrum about 250–2750 Hz |
| Information budget | 3456 bits, including 32×32 eight-color pixels, metadata, and CRC |
| FEC goal | Effective rate near 1/4 with calibrated soft likelihoods |
| Integrity | Whole-message CRC-32 covering identity, format, and raster; protected bootstrap fields |
| Interleaving | Fixed published mapping over the data interval, with bounded memory |
| Synchronization | Known training sequence, robust mode identifier, and distributed pilots |
| Decoder | Bounded CPU work, explicit failure results, no expected-card lookup |

The bandwidth calculation is for ideal shaping. Choose a finite filter and ramps, then measure the generated burst spectrum and adjust symbol rate/roll-off if needed. This is a software waveform check, not a requested measurement of the IC-7300 passband. [RRC modulation reference](https://wiki.gnuradio.org/index.php/Constellation_Modulator).

A starting time allocation is:

| Segment | Budget |
| --- | ---: |
| PTT/key-up allowance and leading ramp | 0.20 s |
| Timing/frequency/channel training | 0.40 s |
| Strongly protected fixed bootstrap/mode information | 0.20 s |
| Coded metadata and image, including distributed pilots | 4.00 s |
| Filter tail, ramp-down, and release allowance | 0.20 s |
| Total | **5.00 s** |

The four-second data interval holds 8000 QPSK symbols. Reserving 10% for pilots leaves 7200 data symbols, or **14,400 coded bits**. At rate 1/4 this supports 3600 information bits, above the 3456-bit budget. Thus the first-order payload/time budget closes without relying on image compression. Guard requirements that grow must reduce another segment; five seconds is not silently extended.

These counts are allocation targets, not a completed code specification. Select an established, documented LDPC family with a suitable block size/rate, or compare a CRC-aided polar code if an appropriate LDPC implementation is unavailable. Include shortening, padding, rate matching, and every CRC bit in the exact ledger. Freeze code matrices, bit order, interleaver, and reference vectors before interoperability work. Do not select a code by nominal rate alone.

Use the existing soft Viterbi implementation only as a development comparator after validating its metric convention and termination. Its current rate-1/2 convolutional code is not the desired final redundancy level. Compare stronger coding against repeated coded bits at the same total energy to establish the coding benefit.

Receiver work for this candidate includes:

1. Matched filtering, coarse timing/CFO search, and channel estimation from known training.
2. Explicit resolution of QPSK phase ambiguity using training and checks.
3. Pilot-assisted phase, timing, and channel tracking, including frequency drift and sample-clock error.
4. Equalization for multipath; noise-aware soft bit likelihoods that include channel-estimation uncertainty.
5. Deinterleaving, bounded FEC decoding, whole-message CRC, and structural validation.
6. A cap on acquisition hypotheses, iterations, buffered time, and decoder queue depth.

No phase-coherent combining between separate transmissions is assumed. Within the burst, coherent demodulation must estimate its reference from the received waveform.

### Comparators and alternatives

| Candidate | Why evaluate it | Decision criterion |
| --- | --- | --- |
| Improved production 8-FSK Resilient | Lowest migration cost; existing app behavior | Keep if it meets sensitivity and duration goals |
| 2000-baud shaped QPSK, near-rate-1/4 code | Fits the full card plus substantial redundancy in five seconds | Primary new-waveform candidate |
| 1500-baud shaped QPSK, near-rate-1/3 code | About 1.875 kHz nominal bandwidth; useful complexity and bandwidth control | Compare at the same 2.5 kHz SNR reference and total energy |
| Existing Data2G backend | Already integrated; offers a coded OFDM reference | Measure exact 32×32 time, sensitivity, CPU cost, and peak-power penalty |
| Coded FSK/CPM candidate | Potential advantage when linear-transmitter backoff dominates | Pursue only if measured radio/PA behavior or QPSK complexity warrants it and the full payload ledger fits |

The local Data2G documentation gives approximately 6.392 s for its robust 32×32 eight-color profile and 2.936 s for the fast one. Recalculate from the pinned dependency before comparing; the robust example exceeds this project's time budget. Changing submode, packing, or waveform parameters is a distinct experiment. Upstream describes an OFDM design and soft coding; its results cannot substitute for this app's measured card performance. [Data2G project](https://github.com/arodland/Data2G); [local adapter notes](DATA2G-BACKEND.md).

Slow orthogonal FSK can be attractive for small messages, but it is not automatically a solution for this payload: 8-FSK at 200 symbols/s carries only 600 raw bit/s, or 2400 coded bits in four seconds, fewer than the uncoded card requires. FT8-style tiny-message thresholds are likewise not transferable to a full image; FT4/FT8 carry 77 information bits with a different time and bandwidth budget. [Protocol paper](https://wsjt.sourceforge.io/FT4_FT8_QEX.pdf).

**Gate C:** an unaided candidate must fit the exact bit/time budget, produce the intended software spectrum, and improve verified full-card sensitivity on held-out captures. Continue only candidates within approximately 1 dB of the best result unless they offer a demonstrated CPU, multipath, or peak-power advantage.

## 7. Stage D — realistic channel and application evaluation

Evaluate the winning candidates in successive layers so each loss remains explainable:

1. **AWGN:** measure complete recovery curves and false accepts, first with known timing as a diagnostic, then with full acquisition.
2. **Oscillator errors:** random arrival phase, ±100 Hz CFO, modest frequency drift, and ±100 ppm clock error. Refine bounds using recorded stations if available.
3. **Propagation:** controlled two-path channels at 0.5, 1, and 2 ms delays, multiple relative path strengths, and slowly/rapidly varying fades. Include static notches and burst noise. Report each channel separately instead of averaging all conditions into a single SNR claim.
4. **Audio/transmitter impairments:** sample-rate conversion, gain variation, clipping, and a realistic peak-power/backoff comparison. QPSK pulse shaping and OFDM can have higher peaks than the current FSK waveform; equal audio RMS alone is insufficient to judge radio efficiency.
5. **Actual app:** two independent clients, normal receive buffers and workers, partial arrivals, repeated snapshots, consecutive cards, cancellation, CQ/exchange/73 metadata, and duplicate suppression. Confirm that complete checked cards reach the log through the real UI path.
6. **Recorded receive audio:** replay every attempt, including failures, with known transmitted cards and independently recorded setup details. Later live comparisons can establish practical performance. The accepted 2.5 kHz IC-7300 passband does not need its own validation campaign.

Keep a conventional uncompressed exact-card mode for acceptance. Compression, repeated prior cards, and progressive previews may be evaluated separately but must not improve the advertised cold-start sensitivity by changing the task.

For false acceptance, run an initial 10,000-window screen, then at least 100,000 independent negative windows for a promotion candidate, including wrong-mode traffic and corrupted headers. Report the count and statistical bound: zero events in 100,000 trials only gives an approximate 95% upper bound of 3×10⁻⁵ per tested window. Also report continuous-stream false detections per hour; expanded hypothesis searches change the number of opportunities for an error.

Measure decode latency on the user's CPU with the UI active. Initial target: 95th-percentile completion within two seconds after the burst and no sustained worker backlog. Latency is reported separately from the five-second transmission limit.

## 8. Stage E — integration and promotion

Register the experimental backend through `card_backends.py`, preserving the common card report. Wire it into `app.py` mode selection, duration estimates, live receive workers, and exact-card handling. Audit hard-coded Fast/Resilient/Data2G checks in automatic exchange handling rather than assuming registration alone integrates a new mode.

Use an explicit experimental label and a versioned waveform identifier. Existing receivers continue to recognize production bursts; experimental receivers must never misclassify a new format as a valid old card. Keep the first implementation to one fixed 32×32 eight-color profile and one bounded receive search. Add adaptive profiles only after the basic link is proven.

For the experimental mode, show the total attempt duration. A copy control must not concatenate multiple five-second bursts while claiming the attempt meets the limit. If later repeated attempts are offered, display and measure their total airtime separately.

Promote only when all of the following are documented:

- Exact payload and ≤5.0 s total duration hold for every supported card in the profile.
- The waveform stays within the accepted 2.5 kHz design budget, including finite filter tails and transitions.
- The measured sensitivity target is supported by held-out cards/seeds and confidence intervals.
- It provides a material advantage over the improved production receiver; aim for ≥3 dB at the same recovery target, with comparable power accounting, to justify a new backend's maintenance cost.
- Negative-input results, decode latency, CPU/memory limits, and real application operation are satisfactory.
- A public wire specification, fixed code tables, implementation version, and reproducible reference vectors exist.

If the main sensitivity target remains out of reach, publish the best measured result and the diagnosed loss. Choose among improving acquisition, a stronger code, or a better equalizer before increasing complexity elsewhere. Changes to the five-second limit or exact 32×32 requirement are explicit future product decisions.

## 9. US HF operating scope

Plan for operation in HF segments that authorize the selected emission for the control operator. The general data limitation in §97.307(f)(3) is 2.8 kHz on the bands to which it applies; band-specific exceptions and permissions are set by §97.305. The accepted 2.5 kHz budget leaves nominal margin. Publicly document the coding technique and review its treatment under §97.309 before over-the-air experimental use. Image content alone does not establish the allowed operating segment. This plan does not select a frequency or assume the user's license privileges. [Official Part 97 text, §§97.305–97.309](https://www.govinfo.gov/content/pkg/CFR-2025-title47-vol5/pdf/CFR-2025-title47-vol5-part97.pdf).

For RF evaluation, retain the distinction between a software occupied-power metric and the bandwidth definition in §97.3(a)(8), which uses 26 dB attenuation. A software B99 result alone is not a legal bandwidth determination. No additional IC-7300 passband suitability test is required by this plan. [Part 97 definition](https://www.govinfo.gov/content/pkg/CFR-2025-title47-vol5/pdf/CFR-2025-title47-vol5-part97.pdf).

Icom documents adjustable SSB/SSB-D transmit filtering and an SSB-D default of 300–2700 Hz. Treat the user's accepted 2.5 kHz capability as the hardware assumption, with suitable existing settings, rather than inferring suitability from that default. [IC-7300 full manual](https://icomuk.co.uk/files/icom/PDF/advancedManuals/IC-7300_Full_English%20v6.pdf).

## 10. Implementation sequence and artifacts

| Order | Work | Main files/artifacts | Exit condition |
| --- | --- | --- | --- |
| 1 | Calibrate SNR, add the 32×32 corpus, duration and failure accounting | `tools/modem_benchmark.py`, `BENCHMARKING.md`, versioned corpus/manifest | Gate A |
| 2 | Antialias filtering and bounded multi-candidate acquisition | `cardmodem.py`, capture-based comparison report | Measured improvement with old-format compatibility |
| 3 | Soft header/block decisions, copy alignment, assisted diagnostics | `cardmodem.py`, stage-level result tables | Gate B and a measured decision on new coding |
| 4 | Freeze experimental payload, integer code ledger, and sync design | New experimental modem specification and reference vectors | All overhead fits five seconds |
| 5 | Build shaped QPSK and compare code/width choices against production and Data2G | Separate experimental DSP module, `card_backends.py`, offline curves | Gate C |
| 6 | Add fading/error campaigns and exercise actual application reception | Benchmark profiles, `app.py`, two-client runtime results | Stage D criteria |
| 7 | Package the optional mode and make a promotion decision | Mode documentation, reproducible results, compatibility artifacts | Stage E criteria |

Each comparison report should contain the exact code revision and uncommitted source snapshot, environment versions, cards, seeds, SNR reference, code parameters, duration, power normalization, decoder limits, and channel parameters. Preserve baseline outputs and create new directories for each change.

## 11. Calibration and baseline update — 2026-10-01

The benchmark now implements an explicit 2500 Hz SNR reference and a shared receive FIR for desired signal, interference, and noise. Its default nominal audio passband is 100–3100 Hz, with measured equivalent noise bandwidth 2951.81 Hz. Both the benchmark and two-client loopback use the same calibration helper. Seven numerical regression checks passed, and both directions of a 32×32 Resilient loopback at +12 dB passed. The benchmark now defaults to 32×32.

The [calibrated baseline report](work/first-comparison/calibrated-snr-32/report.html) contains 1680 trials: 32×32 eight-color cards, both native backends, 1/2/3 copies, and 20 trials per setting. Negative levels 0 through −6 dB produced no complete-card recovery in 840 attempts. Positive levels bracketed the transition. Resilient recovered 18/20 at +7 dB and 9/20 at +6 dB with one copy; at +6 dB, two and three copies each recovered 17/20. Those repeated attempts last 6.18 and 9.27 seconds, outside the five-second target. Requested and measured reference SNR differed by at most 0.091 dB.

This is a single patterned-card baseline, not completion of the planned diverse-corpus and held-out evaluation. It also does not establish field performance. Source snapshots, per-case logs, exact SNR/filter parameters, and [recovery curves](work/first-comparison/calibrated-snr-32/recovery-curves.svg) are preserved with the run.

With the new 3 kHz input filter, wideband noise no longer folds into the approximately 8 kHz acquisition search as it did in the original benchmark. Consequently, adding antialias filtering inside the production receiver remains a robustness improvement but should not be presumed to deliver the same large gain on this filtered baseline. The next implementation task is to instrument acquisition and header failures, compare unaided reception with true-timing diagnostics, and then address the measured bottleneck. The full-card FEC and experimental-QPSK decisions remain pending.
