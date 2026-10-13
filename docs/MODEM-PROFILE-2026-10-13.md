# Experimental modem profiling — 2026-10-13

Focused on `resilient_100` and `resilient_50`, using receiver source at
`dd76bbf220884989be9bb1d8b5bc896a50b1e97d`. No decoder changes or container deployment.

## Results

Three unprofiled repetitions per successful case, clearing the decoded-avatar
cache before each run. Every successful run passed whole-card verification and
matched the expected pixels exactly. Acquisition/template caches can remain warm.

| Mode | Input | Path | Input duration | Median wall | Median CPU |
| --- | --- | --- | ---: | ---: | ---: |
| Resilient 100 | Clean synthetic | Full capture | 18.08 s | 0.700 s | 0.697 s |
| Resilient 100 | Clean synthetic | Payload only | same transmission | 0.060 s | 0.058 s |
| Resilient 50 | Clean synthetic | Full capture | 32.36 s | 4.112 s | 4.026 s |
| Resilient 50 | Clean synthetic | Payload only | same transmission | 2.262 s | 2.262 s |
| Resilient 50 | Synthetic −6 dB AWGN | Full capture | 32.36 s | 2.533 s | 2.532 s |
| Resilient 50 | Saved HF, two copies | Payload only | 57.32 s combined | 14.539 s | 14.110 s |

Resilient 100 failed the same seeded −6 dB case with `no CRC-valid avatar identity
after soft recovery`; no successful timing is claimed for it. This is one card,
one seed, and one noise level, not a sensitivity or decode-rate measurement.
Noise can change the recovery search path, so the faster noisy Resilient 50 run
does not establish a general speed improvement over clean input.

## Where the time goes

- **Resilient 100, clean:** `find_copies` consumed 0.647 s of a 0.733 s profiled
  decode (88%). FFT work consumed 0.398 s. Payload decoding is already relatively
  cheap; acquisition is the first place to investigate.
- **Resilient 50, clean payload:** soft block decoding consumed 2.226 s of a
  3.177 s profiled decode (70%). There were 144 soft block calls, 13,354 RS decoder
  calls, and 12,835 CRC calls. Investigate repeated candidate decoding and byte/CRC
  work while preserving the checks that make recovered blocks trustworthy.
- **Resilient 50, saved HF:** local timing recovery consumed 12.194 s of a 21.295 s
  profiled decode (57%). `demodulate_metrics` ran 25,439 times and consumed 8.908 s;
  soft block decoding consumed 9.080 s. These are overlapping cumulative times,
  not additive shares. Reusing timing metrics and reducing repeated candidate
  checks are promising follow-up targets.

The profiler adds substantial overhead, especially to Python-heavy recovery.
Use the unprofiled medians for latency; use cProfile to locate work.

An identical-buffer cached replay of the saved HF payload took 0.007 s, versus
14.539 s with its decode cache cleared. Cached replay timings must not be used to
claim new-transmission decode speed.

## Memory and scope

RSS immediately before decoding was 110–144 MiB. Whole-process peak RSS was
451–596 MiB, including imports, waveform generation/acquisition, profiling, and
fixture preparation. These values do not isolate decoder allocations or prove a
memory leak; an allocation-specific profile is needed before optimizing memory.

Host environment: Python 3.13.6, NumPy 2.5.3, SciPy 1.18.1, x86_64, eight available
CPUs. OpenBLAS/OMP were limited to one thread; receiver FFT worker policy was
unchanged. Host load varied during the run. The final Resilient 50 clean cases
were rerun sequentially to remove contention between profiling jobs.

Full capture means the experimental adapter's acquisition and payload path,
not the entire application's receive-all loop or audio/UI scheduling. Payload
cases exclude header search. Synthetic input uses one copy, 300 Hz placement,
seed 1030, and the example card's canonical transmitted pixels. AWGN SNR is
referenced to 2500 Hz. Saved HF uses the previously admitted payload boundaries
from the October 12 capture and its known reference card; it does not measure
fresh acquisition on that recording. No new RF, hardware, or container-runtime
validation was performed.

## Reproduce

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python tools/profile_experimental_modems.py --mode resilient_100 --scenario clean --pipeline full --output work/modem-profile-20261013
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python tools/profile_experimental_modems.py --mode resilient_50 --scenario clean --pipeline full --output work/modem-profile-20261013
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python tools/profile_experimental_modems.py --mode resilient_50 --scenario awgn --snr-db -6 --pipeline full --output work/modem-profile-20261013
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python tools/profile_experimental_modems.py --mode resilient_50 --scenario saved-hf --pipeline payload --output work/modem-profile-20261013
```

Use `--pipeline payload` with either clean synthetic mode for payload-only
profiling. Saved HF requires local `work/rx-analysis-20261012` fixtures; synthetic
cases require no recording. The tool fails on incomplete or mismatched cards.

Tracked evidence: [timings, provenance, and hotspots](rx-evidence/2026-10-13/experimental-modem-profile.json).
Raw `.prof` and readable hotspot listings remain in the gitignored
`work/modem-profile-20261013/` directory.
