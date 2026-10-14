# Data2G recovery checkpoint

Recovery branch: `fix/data2g-recovery`, based on `dd76bbf` (the reset main).
Reviewed `origin/main` (`5e0fcb1`) and `broken` (`8986790`). Neither reference
was rewritten or merged wholesale.

## What the comparison establishes

The operator tested both stations at each source checkpoint, using the dropdown's
**Narrow HF** choice. That maps to `n10-qpsk-r1/2` (500 Hz). The 16-second
fragment target introduced in `5e0fcb1` still allows 256-byte application frames
for this mode; it does not explain this mode's reported regression by itself.
No attributable failed RF recording from that test sequence has been identified,
so the historical cause remains unproven.

The local environment initially still contained Data2G `27236ec` after the source
reset. `uv sync --locked` restored the checked-in `20bdb05` pin. Direct generated
audio comparisons show that the two revisions use different narrow-band headers
(the newer n10/n4 header is 5|5 instead of 4|6, with protocol version still 12).
Matching old/old and new/new pairs recover the exact frame; mixed revisions fail
in narrow mode. Wide QPSK is a successful cross-revision control. This proves a
compatibility hazard, not the cause of the operator's earlier matched testing.

Read-only inspection also found that the current container has `broken` app and
card-assembly files, the newer shim without the receive-length override, but
Data2G `20bdb05`. That dependency lacks merged PR #65. Consequently its receiver
can again reject legal broadcasts longer than 16 seconds. For example, a
256-byte `n10-qpsk-r1/5` burst takes 19.4 seconds. This example is slower than the
operator's selected r1/2 mode and must not be presented as proof of that failure.

PR #65 is merged in upstream `27236ec` (including the MODES reporting fix in its
ancestry). The recovery wrapper uses upstream `receive_accept` unchanged when
available; older hosts retain the isolated receive-length workaround. The old
pin was used only for the rollback baseline. The final project dependency follows
upstream's default branch, with `uv.lock` resolving `27236ec`; neither the manifest
nor the managed host is pinned to the old PR. Both the local environment and the
container must install that lock, not only copy app source. The stream Dockerfile
now installs the overlay's pyproject/lock instead of the older fetched checkout's
dependencies. 256-byte framing and receive-SNR metadata remain intact.

## Recovered fixes

| Source | Fix retained |
| --- | --- |
| `broken`, `card_transfer.py` | v4 verified-block spans subtract the eight GF64 overhead symbols: 106 pixels per 3-bit block, rather than 122. Later checked blocks can correct tentative pixels instead of producing false overlap conflicts. |
| `origin/main`, card identity | Native v4 and preview state include numeric card ID even for identical artwork. Distinct cards cannot share partial preview evidence. |
| `origin/main`, native receiver | The newest decoded native identity wins over a more complete older card; absolute native acquisition information is retained. |
| `broken`, preview | Repeated same-identity activity preserves a completed image instead of clearing and hiding it. |
| `origin/main`, outer RS | Independent admitted copies retain complementary checked shards. Conflicting shard values are excluded; whole-card integrity remains mandatory. |
| Recovery wrapper | Detect merged PR #65 capability; retain legacy long-frame reception without overriding the newer upstream policy. |

## Work retained on the original branches

The new orthogonal 8FSK wire format, unidentified-pixel preview feature, broader
native fallback/discovery changes, profiling tools/reports, and parallel header/
payload worker experiments remain available on `origin/main` and `broken`.
They are not prerequisites for restoring the Data2G baseline. In particular,
`broken` adds concurrent access to the mutable copy cache and permits overlapping
payload work; those changes need their own resource and concurrency review before
reintroduction. Keeping sequential baseline scheduling avoids importing that
experiment into this recovery.

## Verification and reproduction

Artifacts and logs are in gitignored `work/recovery-20261013/`. The initial full
suite ran 209 tests with five failures and 26 errors. Most errors are stale tests
trying to transmit deliberately disabled compressed/grid modes or invoke removed
UI functions. Relevant preview expectations were updated for the already shipped
single duplicate tile and unified starter gallery; production behavior was not
changed to satisfy obsolete UI expectations. Do not report the full legacy suite
as green.

The recovery checks include a golden narrow-header compatibility vector, actual
256-byte modem waveform recovery beyond the old duration limit, real fragmented
card recovery through narrow and wide modem/KISS/assembly paths, shortened v4
block refinement, identity separation, native recency, and complementary outer
shards. Exact-mode two-host virtual-audio checks use no radio or rig control:

```
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/verify_data2g_hosts.py --mode n10-qpsk-r1/2
```

The tool creates uniquely named temporary audio devices and isolated host ports,
checks exact recovered pixels/identity/CRC, and unloads its own devices on exit.
The managed wrapper and upstream host paths are checked separately. Source,
virtual audio, and recorded/generated waveform evidence do not establish fresh
RF recovery. The final container image includes a source hash manifest and the same upstream
Data2G lock as the local environment. Deployment verification is recorded below.

## Final result

- Final manifest follows upstream's default Git branch; lock and installed
  desktop/container dependency both resolve `27236ec1c11ab5adc584d2d7a0d903d0fe1ea594`.
- 154 relevant recovery tests pass on that final dependency (79.039 seconds).
- Rebuilt image: 10 runtime tests and the three-mode waveform/card test pass.
- Two isolated managed hosts pass Narrow HF `n10-qpsk-r1/2`: 2/2 fragments,
  exact raster and station identity, whole-card CRC verified. The older baseline
  also passed the same virtual-audio check.
- Container recreated from `radio-stream:pixelqso-recovery-20261013`, also tagged
  `radio-stream:pixelqso`. Source-manifest hashes and installed dependency match.
  The original three persistent mounts and runtime environment were retained.
- Previous image retained as `radio-stream:pixelqso-before-recovery-20261013`;
  overwritten sibling source-overlay files/Dockerfile backed up under
  `work/recovery-20261013/stream-overlay-before/`. No unrelated sibling changes
  were reset.
- Original RF regression cause remains unproven. Retest Narrow HF on air with
  both stations on this final revision and capture exact UTC/mode plus raw audio.
