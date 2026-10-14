# 7066 kHz receive investigation, 2026-10-14

The source-stage RF recording is `pixelqso_stream/recordings/skew_followup_20261014/AG7SU`.
The WebSDR bridge was tuned to 7066 kHz USB. Earlier app event metadata says 14074 kHz;
that metadata did not represent the bridge tuning. WAV chunks cover all extracted payloads.

## Findings and changes

* E-major truncated all hypotheses to the shortest evidence length, losing the final
  four raster pixels even in clean audio. Each hypothesis now retains its own extent.
* E-major selected unchecked previews partly by combined-copy count. A protected
  content digest now selects valid candidates before preview ranking. Unverified
  combinations cannot replace a single-copy preview merely by adding copies.
* The initial two E-major RF copies now combine into a digest-verified 1024-pixel card.
  Later passes of that card and all three passes of card 32285 remain unverified.
  A later captured copy shows relative alignment changes of several raster symbols.
  Constant-color regions make some lag estimates ambiguous; the origin of discontinuities
  remains unproven. A reported nominal 0 ppm is not an independent clock measurement.
* Offline scanning found a protected QPSK header missed by the live receiver.
  Its payload passes native integrity and the protected content identity.
* Three outer-RS copies reconstruct a whole verified card offline. The live receive
  restarted between copies, clearing its cache; its later result had ten checked shards,
  fewer than the twelve required for reconstruction. The earlier zero-packet status
  was incomplete and should not be read as the final capture result.
* Live jobs took 85–328 seconds. Without a protected header, live native fallback now
  probes the selected mode on a bounded recent window. Protected headers continue to
  route all modes. The same 19.528-second capture's fallback took 4.954 seconds in replay,
  versus 85.137 seconds in its original live job; these are different runs/paths.
* Discard preview cleared PCM but retained the previous sample-count watermark, delaying
  new decode scheduling. It also cleared a cache still owned by stale workers. Discard
  now resets the schedule and replaces the cache; listener operation continues.
* Existing overlay frame-sync protection and duplicate-hypothesis suppression were
  preserved and reconciled into source.

## Validation

71 musical/copy-header/live-preview tests passed, including stale-cache isolation after
Discard preview, partial-copy preservation, nonzero final pixels, and preview preservation
when an unverified copy is added. Nine narrow-mode tests passed. The broader experimental
suite passed its substantive codec tests but exposed four stale assertions: two orthogonal
modes exceed the old ten-second limit and sync-only activity on noise is allowed. Those
assertions now preserve zero verified packets/pixels, and the two affected tests passed.

`summary.json` and the named report JSON files contain final replay outcomes. Offline
replay is evidence from the recorded RF audio; updated live behavior needs a new on-air
pass. No transmitter wire-format changes were made.

Reproduce the selected protected acquisitions:

```sh
.venv/bin/python docs/rx-evidence/2026-10-14/replay.py   /home/tyler/dev/pixelqso_stream/recordings/skew_followup_20261014/AG7SU   /tmp/pixelqso-rf-replay
```

The replay groups by protected content identity and orders captures chronologically.
It can pool the same identity across receive restarts for diagnosis; the live cache is
intentionally scoped to its current receive window.
