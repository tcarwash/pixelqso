"""Launch Data2G with TX-advertised burst lengths enabled on receive.

Data2G advertises up to 64 codewords per header, but its default live Engine
constructs an Accept table capped at 16 seconds. PixelQSO can transmit longer
frames using those same advertised modes. Apply the compatibility override in
this child process only, then hand control to Data2G's normal host CLI.
"""
from __future__ import annotations

import sys


def enable_advertised_receive_lengths() -> None:
    """Make Engine's receive table honor its protocol codeword maximum."""
    from data2g import modem

    original = modem.Accept.of.__func__
    if getattr(original, "_pixelqso_full_receive", False):
        return

    def of(cls, names=None, max_secs=None, min_score=0.0):
        # Keep explicit submode restrictions, and all score checks. Drop only
        # the extra seconds cap; Accept.of still bounds each mode by the count
        # its protected header can represent.
        if names is None:
            max_secs = None
        return original(cls, names, max_secs, min_score)

    of._pixelqso_full_receive = True
    modem.Accept.of = classmethod(of)


def main() -> int:
    enable_advertised_receive_lengths()
    from data2g.host import main as data2g_host_main

    sys.argv = ["data2g-host", *sys.argv[1:]]
    data2g_host_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
