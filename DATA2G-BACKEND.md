# Previous direct Data2G PCM backend (removed)

This document described the old in-process adapter that imported Data2G's Python
modem and exchanged audio samples. That adapter, its package dependency, and its
PCM benchmark path have been removed. PixelQSO now connects to a separately
running Data2G host through its supported command and KISS interfaces.

See [the current migration plan](docs/DATA2G-INTEGRATION-PLAN.md) for mode policy,
frame format, host operation, and verification status, and
[the host protocol notes](DATA2G-BROADCAST-NOTES.md) for API semantics and open
interoperability checks.
