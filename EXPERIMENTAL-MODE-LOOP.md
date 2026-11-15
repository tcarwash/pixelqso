# Experimental modem improvement loop

Goal: improve verified PixelQSO card recovery on weak and marginal signals while
keeping false verified cards at zero and keeping receive latency and CPU use
practical. The first synthetic pilot is recorded below; it is not RF evidence.

## 1. Freeze a baseline

Record the source commit, working-tree diff, Python/dependency versions, CPU,
mode key, card size/palette, audio placement, copy count, ID-marker setting,
sample rate, and decoder path. Preserve the current unrelated working-tree
changes; make each decoder candidate in an isolated Codex worktree. Keep the
transmitter waveform fixed when comparing receiver changes.

Start with an inexpensive smoke run of the existing harness:

```sh
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/benchmark_hf.py \
  --modes experimental_qpsk_5s --profiles awgn hf_moderate \
  --snr-db -6 0 --trials 1 --output work/modem-loop/smoke
```

`tools/benchmark_hf.py` already writes per-trial diagnostics, `results.json`,
and `summary.csv`. Its synthetic HF presets are illustrative, not certified
channel models or RF measurements. Do not use its broad default mode list for
the main campaign: it includes experimental receive-compatibility formats
whose transmit path is disabled. Select tested, transmit-capable modes
explicitly.

## 2. Make the benchmark match the product

The harness now supports `--id-markers on|off` (default `on`),
`--audio-placement near_carrier|centered`, `--save-wavs`, and
`--replay-manifest`; `--interlaced` rotates blocks across protected repeated
burst copies. A saved manifest records card, transmit waveform, and
capture hashes, expected card data, channel settings, marker state, audio
placement, source revision, working-diff and harness hashes, and airtime.
Replay verifies each capture hash before scoring. Each
trial records a coarse failure stage, wall and CPU time, and process peak RSS.
Use the replay command on the same manifest for every decoder candidate:

```sh
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/benchmark_hf.py \
  --replay-manifest work/modem-loop/development/manifest.json \
  --output work/modem-loop/replay
.venv/bin/python tools/compare_hf_replays.py \
  work/modem-loop/development/results.json work/modem-loop/replay/results.json
```

The comparison tool refuses capture sets with different digests by default. Use
`--paired-cases` to compare different transmitted waveforms for the same card
and channel seeds, such as plain repeated versus interlaced copies. It reports
paired gains and losses, false verification, and median decode time by stratum.

The following are still needed before a promotion-scale comparison:

1. Exercise the narrow modes at each supported placement and add a fixture for
   audio-chain high-pass filtering.
2. Score the production receive paths separately: selected mode, receive-all,
   saved WAV, and growing live snapshots through `LiveDecodeWorker` and
   `CopyCaptureCache`. Keep card assembly's checked completion gate.
3. Refine stage outcomes with decoder-specific evidence for missing blocks and
   timing or carrier misses. Preserve provisional coverage as a separate
   metric; it is not a successful decode.
4. Record time to first activity/preview and time to verified card. A decoder that
   recovers a card after the next beacon slot or stalls the live worker has not
   solved the operating problem.

Run a small known-good fixture through all paths before expanding the matrix.
Version the harness and manifest schema; do not change scoring mid-comparison.

## 3. Build two corpora

**Development set:** paired seeds and card patterns (random, flat, stripes,
real artwork) across clean audio, AWGN, mild/moderate/severe fading, echo,
combined fading and noise, carrier offset, frequency drift, and sample-clock
error. Include both marker settings, one and repeated copies, and narrow-mode
audio placements. Add input filtering/AGC, clipping, impulses, and nearby
signals as separate impairment families after the current basic channel is
stable. For 5/10 baud, measure the effect of sound-card high-pass filtering
near 25 Hz rather than assuming the audio path passes those tones.

**Held-out set:** different seeds, cards, impairment combinations and recorded
audio never shown to the Codex tuning run. Include empty noise, wrong-mode
signals, truncated captures, competing cards, and previously failed real
captures. Keep raw source files immutable and identify them by digest. Real
captures need labels for intended mode, station/card identity, capture route,
and whether an intact card is actually present; unknown labels stay unknown.

Use a short card and a few trials for diagnosis. Confirm any winning change on
full 32×32 cards, especially for 5 baud (about five minutes per copy). Do not
replace full-card evidence with a short-card result.

## 4. Measure the baseline and select one failure cluster

For each mode and channel family, sweep a few SNR points around the recovery
transition, then add seeds near that transition. Express SNR in the harness's
2500 Hz reference bandwidth. Keep transmit RMS fixed and report airtime as
well: a longer waveform carries more total energy, so a better recovery rate
is not automatically a coding gain. Report verified exact-card recovery with
confidence intervals, false verification, decode time, and latency. A 50%
recovery SNR estimate is useful only after enough trials bracket the transition.

Rank failures by missed verified cards *and* likelihood of appearing on actual
captures. Pick one hypothesis per iteration. Likely first targets are native
acquisition without ID markers, clock/CFO drift over long narrow cards,
header-to-payload boundaries, weak FSK tone evidence under fading, and
noise-weighted copy combining. Inspect the failed WAVs and diagnostics before
changing code.

## 5. Codex managed validation and code-tuning run

Start one bounded **Codex worktree task** for the chosen failure cluster. Give
it the baseline report, development fixtures, failure WAVs, fixed scoring
contract, affected decoder files, and the prompt below. The task may edit
receiver code and focused tests, run development fixtures, and return a diff
plus a compact before/after table. It must not inspect or alter the held-out
set, change the transmitter format or mode IDs, relax CRC/content checks, or
claim RF success from synthetic audio. Limit each run to one causal change;
discard an unsuccessful candidate and start the next from the same baseline.

This can be a local Codex app worktree task. For a programmatic managed Codex
harness, the [official Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview)
supports a session with an execution environment and follow-up turns; the
repository and fixtures must be available in that environment. A local
`codex exec` job is another bounded option, but it is distinct from an
OpenAI-managed Agents API session. Do not make the desktop app or the modem
call an LLM during reception.

Suggested task prompt:

> Improve verified decode for **[mode + impairment + marker setting]**. Read
> `EXPERIMENTAL-MODE-LOOP.md`, the frozen manifest, baseline `results.json`,
> and the listed failure captures. Reproduce at least two failures. State one
> decoder-side hypothesis and a measurable prediction before editing. Change
> only **[allowed files]**. Preserve all wire IDs, payload bytes, integrity
> gates, and unrelated working-tree changes. Run the same development captures
> before and after, plus focused unit tests. Report verified exact cards,
> false verified cards, median/p95 decode time, first activity and verified
> latency, and any regressions by mode/channel. If the result does not improve
> the target without a material regression, revert your candidate change and
> report the evidence. Leave the held-out corpus untouched.

Codex's review of waveforms and code is a hypothesis generator. Deterministic
replay and integrity checks decide whether the patch works.

## 6. Independent gate and promotion

After the Codex task ends, run the untouched held-out corpus from outside its
worktree on both baseline and candidate revisions, using identical capture
bytes. Predeclare the minimum worthwhile gain, sample count, and latency
budget before seeing these results. For a first full-card campaign, use at
least 50 paired marginal captures per target mode/channel stratum, 50 clean
captures, and 100 negative controls; report a paired confidence interval for
the recovery difference. Increase sample size if the interval remains too
wide to decide. Require all of the following before promotion:

- Zero false verified cards in controls and no bypass of CRC32, content tag,
  protected identity, or checked card assembly.
- A repeatable verified-card gain in the target marginal region, with no
  meaningful regression on clean audio or other supported modes. Report
  counts and uncertainty rather than a pass/fail percentage alone.
- Decode latency and memory within the live receive budget; no growing-snapshot
  backlog. Run `tools/profile_live_receive.py` on a matching WAV for expensive
  receive changes.
- Focused unit tests, saved-WAV replay, and two-window/virtual-audio checks.
  Then test two actual stations and retain both raw captures before calling
  the change on-air validated.

If the candidate fails, retain its manifest, diff, and failure classification
in the findings log, then choose the next hypothesis. If it passes, promote
the patch and add the recovered capture to future regression fixtures. Repeat
the loop with a new baseline.

## Findings log

| Run | Revision / diff | Mode, markers, failure cluster | Development result | Held-out result | Latency / CPU | Decision |
|---|---|---|---|---|---|---|
| Smoke, 2026-11-11 | `a6caa0e` plus dirty checkout | `experimental_qpsk_5s`, marked; AWGN and moderate HF, −6/0 dB, seed 1000 | 0/2 verified at −6 dB; 2/2 at 0 dB; noise 0/1 false verified | Not run | Decode 0.20–1.08 s | Harness smoke passed; too small for reliability claims |
| Frozen development, 2026-11-11 | `a6caa0e`; decoder SHA256 `a46d90b2…` | `experimental_qpsk_5s`, marked; seeds 1200–1203; AWGN/moderate HF at −6/−3/0 dB | AWGN 0/4, 4/4, 4/4; moderate HF 0/4, 0/4, 2/4; noise 0/4 false verified | Held out seeds 4200–4203 | Manifest has 28 captures | Baseline frozen; LDPC parity failure selected |
| Codex iteration 1 | Isolated checkout; decoder reverted | Shorten QPSK pilot gain average from 9 to 3 blocks | No changed exact-card outcomes across all 28 captures; 0 false verified | Not opened to Codex | Moderate HF median decode about 4.2 s at −3 dB in replay | Reject: no target gain; four focused tests passed |
| Codex iteration 2 | Same isolated checkout; decoder reverted | Smoothed per-block pilot residual variance for LDPC evidence | No changed exact-card outcomes across all 28 captures; 0 false verified | Not opened to Codex | Moderate HF median decode about 4.4 s at −3 dB in replay | Reject: no target gain; five focused tests passed |
| Held-out baseline | Same decoder SHA256 | Seeds 4200–4203; same channel grid | Not used for tuning | AWGN 0/4, 4/4, 4/4; moderate HF 0/4, 0/4, 1/4; noise 0/4 false verified | Synthetic offline only | Baseline characterization; no candidate passed development gate |
| Product-path and marker checks | Same decoder SHA256 | Clean QPSK capture; marker-off QPSK and centered `resilient_100` | QPSK and `resilient_100` each verified exact in one marker-off clean trial; noise controls stayed unverified | 28 held-out WAV outcomes reproduced byte-for-byte | Live selected and receive-all each verified at the final 8.422 s snapshot; three snapshots, median decode under 0.25 s | Path smoke only; no live RF or audio-device proof |
| FSK pilot-derived per-tone gain/noise estimate | Isolated Codex candidate; reverted | `experimental_16fsk_ldpc`, AWGN and moderate HF, −3/0 dB, 15 immutable development WAVs | No exact-card changes: AWGN −3 0/3, AWGN 0 3/3, moderate HF −3/0 0/3 each; noise controls 0 false verified | Not run | Median 5.35 s baseline vs 5.32 s candidate | Reject: no recovery gain; keep baseline decoder |
| FSK interlaced repeated-copy TX | Benchmark-only waveform option; no decoder patch retained | Two paired seeds, `experimental_16fsk_ldpc`, two copies | Plain repeats: AWGN −3 2/2 and 0 dB 2/2; interlaced: −3 0/2 and 0 dB 2/2; both 0/4 moderate-HF recoveries and 0 false verifies | Not run | Median across 10 captures: plain 4.95 s; interlaced 6.05 s | Reject interlacing for now: loses the two marginal AWGN cases and does not improve fading; plain repeats cost twice the airtime but helped these two AWGN cases |
| Interlace-index Codex investigation | Isolated Codex candidate and focused test; reverted | Same immutable interlaced WAVs; investigate copy-index block restoration | Candidate produced no new exact-card recoveries: 0/2 AWGN −3, 2/2 AWGN 0, 0/4 moderate HF; plain replay reproduced 2/2 AWGN −3 | Not run | Candidate median 6.95 s vs plain 4.95 s | Reject patch. Code review showed existing path restores with `copy_index`, then `_ldpc_evidence` defaults to `copy_index=None` (identity); candidate passed the index a second time, so its claimed duplicate rotation was incorrect |
| Trained 16-FSK soft demapper v1 | Offline PyTorch MLP; receiver hook reverted; trainer retained | Seven-symbol context window, 16 tone-energy features; 500 training bursts, 120 disjoint-seed synthetic test bursts across AWGN and three fading presets | Synthetic heldout symbol accuracy 28.97% vs 32.32% for center-symbol energy argmax. On the 15 frozen card captures: AWGN 0 dB fell from 3/3 to 0/3 exact cards; AWGN −3 dB remained 0/3; moderate HF −3/0 dB remained 0/6; noise controls 0 false verifies | Not run | Candidate decode median 3.00 s for AWGN vs baseline 1.13 s at 0 dB; moderate HF was similar | Reject: worsens clean-ish packet recovery and adds no fading gain. Removed model inference hook; retained `tools/train_fsk_demapper.py` and `work/modem-loop/fsk-demapper-v1.npz` for reproducibility only |
| Pilot-conditioned ridge equalizer v1 | Alternate per-burst receiver lane; reverted | 16-FSK tone-energy mixing fit from known pilots; blocked leave-one-pilot-group-out validation gates use | No changes on 15 frozen cards: AWGN −3 dB 0/3, AWGN 0 dB 3/3, moderate HF −3/0 dB 0/6; noise controls 0 false verifies. Adapter passed its pilot-CV gate on only 3/110 timing hypotheses, at 12.5–16.7% accuracy vs 4.2–12.5% raw pilot tone accuracy; it recovered no additional card | Not run | Median decode 3.26 s AWGN −3, 1.11 s AWGN 0, 8.26 s moderate HF −3, 5.86 s moderate HF 0 | Reject: the small pilot set is too weak to estimate a useful tone-mixing correction; removed the adapter lane |

The Codex runs used `codex exec` in an isolated Git worktree with only the
development manifest. FSK per-tone equalization and the interlace-index
investigation were separate managed runs using immutable saved WAV replays;
no held-out FSK corpus was used. The interlace investigation’s proposed
second restoration was not a fix: the existing `_ldpc_evidence` default uses
identity ordering, while passing the copy index again would rotate twice. The second run resumed the first Codex session. Neither
candidate changed transmitter bytes or the checked completion gate, and both
decoder edits were reverted. The small pilot does not satisfy the 50-pair and
100-control promotion sample targets. The next receiver hypothesis should
target frequency-selective distortion or intersymbol interference directly,
using a new development stratum and the same held-out discipline.
