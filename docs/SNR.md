# Receive SNR and station reports

New protected-header local-mode receptions populate `card.measured_snr_db`,
`card.measured_snr_bandwidth_hz` (2500), and `card.measured_snr_method`.
The estimate uses the common header's known training tones and off-tone noise
bins between 300 and 2800 Hz. It measures the header, rather than claiming an
average across the entire image payload. Estimates assume representative noise
in that passband; interference, filtering, fading, and clipping can bias them.
Synthetic AWGN checks establish approximate calibration and gain independence,
not physical receiver or RF calibration. Existing unannounced recordings may
have native estimates with no reference bandwidth recorded.

Exchange and RR73 replies use the receive estimate as the existing `snr_db`
station report. `snr_db` remains distinct from `measured_snr_db`: it describes
how the sending station received its peer. Ordinary cards, CQ, and final 73
do not automatically send an SNR report. Old saved cards without measurements
cannot acquire a trustworthy estimate without their original audio.

The managed Python Data2G host forwards `PHY.measure`'s existing `snr_est`
and reference bandwidth. For a fragmented card, the measurement describes the
latest measured burst supplying its fragments. Remote or native Data2G hosts
without the metadata extension still deliver cards, but do not supply this
receive estimate through the ordinary KISS interface.

## Managed-host KISS metadata extension

The child-process shim emits private KISS command `0x0D` on the receive port
immediately before the corresponding ordinary KISS DATA frame, in one locked
socket write. Metadata is `>4s32sfH`: magic `PQS1`, SHA-256 of the exact DATA
payload, float32 SNR, and uint16 reference bandwidth in Hz. It changes the local
host/client interface only; the RF format and ordinary DATA payload are unchanged.

The transport delivers metadata and its following DATA callback in order on
the same socket. PixelQSO matches by session, port, and frame hash, and consumes
the measurement once. Queues are bounded. Missing, malformed, or nonfinite
measurements do not prevent reception. Unknown KISS commands remain ignorable
for clients that only consume ordinary DATA frames.

When a broadcast group's control header is decoded but its application frames
are lost, the managed shim emits `BCAST <port> RXMETADATA <snr> <bandwidth>
<call-or-dash> <mode> <codewords>` on the command connection. It updates incoming
burst details without claiming a card identity or attaching that measurement to
an unrelated DATA frame. Partial application metadata is displayed independently
of artwork; repeated protected headers preserve known details for the current
incomplete image, while a new protected identity clears the previous station.
