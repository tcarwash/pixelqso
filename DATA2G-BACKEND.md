# Previous direct Data2G PCM backend (removed)

This document described the old in-process adapter that imported Data2G's Python
modem and exchanged audio samples. That adapter and its PCM benchmark path have
been removed. Data2G remains a pinned standard-install dependency because the
managed child host uses its supported command and KISS interfaces; PixelQSO
does not call Data2G modem functions in-process.

See [the current migration plan](docs/DATA2G-INTEGRATION-PLAN.md) for mode policy,
frame format, host operation, and verification status, and
[the host protocol notes](DATA2G-BROADCAST-NOTES.md) for API semantics and open
interoperability checks.
