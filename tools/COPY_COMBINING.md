# Experimental copy framing and HF receiver hardening

All 17 local experimental mode IDs support 1–20 copies. Set **Station settings → Options → Copies per card**. The saved copy count applies to each local modem, and the displayed airtime includes framing. Data2G remains a host-owned transport; this framing is not injected into its KISS frames.

The two compressed FSK modes use one v3 opening header and content-dependent payload sizes; see [COMPRESSED-FSK.md](../docs/COMPRESSED-FSK.md).

The two grouped FSK modes use one v2 opening header for the entire group; see [GROUPED-FSK.md](../docs/GROUPED-FSK.md). The other modes retain v1 per-copy framing.

A v1 transmission is `[common prefix][native modem payload][common trailer]` for each copy. The fixed common waveform is 100-baud 8-FSK (800–1500 Hz), independent of the selected payload format. Each marker takes 1.6 seconds, adding **3.2 seconds per copy**. Payload formats retain their own synchronization and error-control codes. Historical unannounced payloads still use native acquisition.

## Common header version 1

| Field | Encoding |
|---|---|
| Magic | `PQCH`, 4 bytes |
| Version | 1 byte, currently 1 |
| Mode/role | Stable mode ID, with bit 7 marking the trailer |
| Payload length | Big-endian uint32, samples at 48 kHz |
| Content identity | 16-byte BLAKE2s digest |
| Checksum | CRC32 over preceding fields |

The 30-byte header becomes 40 GF(64) data symbols plus 20 RS parity symbols, correcting up to ten damaged GF(64) symbols. Its 120 8-FSK tones are interleaved. A seeded 32-symbol common training sequence precedes them. Supported mode IDs are explicit in `modem_envelope.MODE_IDS`; never derive wire IDs from menu order. Unsupported versions/mode IDs are not accepted as announcements.

The digest binds mode, callsign, grid, 16-bit card ID, dimensions, and on-air colors. For burst formats it also binds message type and SNR report. Standard progressive packets do not carry those latter fields, so their digest excludes them. A completed native decode must reproduce the digest. This is an accidental-mixing/integrity check, not cryptographic sender authentication.

RX validates RS, CRC32, version and mode before choosing the payload decoder. Either marker can delimit a payload. Copies with different identities are kept separate, even when they reuse a card ID. Each copy is independently aligned and frequency-corrected before pooling soft evidence. Checked modes retain their existing whole-card or block integrity gates; raw mode remains an unverified preview.

## Receivers

- QPSK: noise-weighted bit LLR combining before LDPC. The equalized receiver combines adaptive-equalizer evidence across copies and can fall back to the pilot receiver.
- Experimental LDPC FSK: known-tone noncoherent acquisition, local timing/CFO refinement, calibrated noncoherent `log I0` tone likelihoods, then bit LLR combining before LDPC. Noise is estimated from distant pilot tones using the exponential-energy median correction.
- Outer-RS FSK: combine tone likelihoods before inner RS/CRC and outer erasure recovery.
- Avatar/narrow FSK: independently align admitted copies, combine header and image metrics before RS/CRC, and use bounded soft Chase recovery for the native identity. Narrow TX now uses v4 image-bound blocks; v3 recordings remain readable.
- Progressive standard: combine packet likelihoods inside each common content identity. Recorded evidence state keeps separate content namespaces, preserving earlier packet groups instead of mixing reused card IDs.

Live RX retains admitted payload copies in a bounded cache (8 kHz float32, up to 20 copies per identity, four identities, 256 MiB stored payload budget). Growing snapshots replace their earlier snapshot rather than creating a new copy. This lets copies survive the 360-second rolling audio window. A new receive window gets a new cache; stale workers cannot populate it. Exceeding the budget evicts the least recently updated identity.

## Validation and benchmarking

```sh
OPENBLAS_NUM_THREADS=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests
OPENBLAS_NUM_THREADS=1 QT_QPA_PLATFORM=offscreen .venv/bin/python tools/verify_experimental_burst_link.py --copies 2 --output work/copy-hardening-live/live-link.json
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/benchmark_hf.py --repeats 8 --snr-db -6 --trials 5 --output work/hf-copies8
```

The HF simulator uses illustrative Gaussian-scatter profiles; these measurements are synthetic, not RF or sound-device validation. Compare airtime and total energy as well as copy counts: different payload modes have different durations, and added markers alter the fading realization for a given seed. Large copy counts may require substantial decode CPU time.
