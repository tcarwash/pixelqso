import unittest
from unittest.mock import patch

from data2g import modem
from data2g import codes
from data2g.arq.engine import receive_accept
from data2g_receive_host import (enable_advertised_receive_lengths,
                                 waterfall_audio_datagram)
from data2g_runtime import (data2g_audio_device_selector, local_host_arguments,
                            local_host_command)


class Data2GRuntimeTests(unittest.TestCase):
    def test_waterfall_tap_mirrors_the_host_left_channel_and_rate(self):
        import numpy as np
        audio = np.asarray([0.25, -0.25, 0.5, -0.5], dtype=np.float32).tobytes()
        packet = waterfall_audio_datagram(audio, channels=2, sample_rate=48000)
        self.assertEqual(int.from_bytes(packet[:4], "big"), 48000)
        np.testing.assert_allclose(np.frombuffer(packet[4:], dtype="<f4"), [0.25, 0.5])

    def test_merged_pr65_receive_policy_is_left_unchanged(self):
        from data2g.arq import engine
        original = modem.Accept.of.__func__
        with patch.object(engine, "receive_accept", create=True, return_value=None):
            enable_advertised_receive_lengths()
        self.assertIs(modem.Accept.of.__func__, original)

    def test_long_256_byte_broadcast_roundtrips_with_upstream_receive_policy(self):
        import numpy as np
        from data2g.arq import phy
        from data2g.config import FS
        from data2g.kisslink import KissLink

        tx, rx = KissLink(broadcast="n10-qpsk-r1/5"), KissLink()
        payload = bytes(range(256))
        tx.enqueue(payload)
        burst = tx.next_burst()
        audio = phy.tx_audio(burst)
        self.assertGreater(len(audio) / FS, 16)
        self.assertGreater(len(burst.slots), dict(modem.Accept.of(None, 16).max_cw)[burst.submode])
        original = modem.Accept.of
        try:
            enable_advertised_receive_lengths()
            accept = receive_accept()
            received = modem.receive(np.r_[np.zeros(FS // 2), audio, np.zeros(FS // 2)],
                                     bands=["n10"], accept=accept)
            self.assertEqual(rx.on_burst(received), [(0, payload)])
        finally:
            modem.Accept.of = original

    def test_narrow_header_matches_locked_upstream_revision(self):
        # Upstream changed n10 (also used by n4) from 4|6 to 5|5 without
        # changing PROTOCOL_VERSION. A dependency update must not silently
        # split the desktop and container into incompatible radio protocols.
        import numpy as np
        expected = {
            "n10-qpsk-r1/5": "68936bee1cdab4acaf534cc024fe0bea7b3ffef4e3b8d17815",
            "n4-qpsk-r1/5": "68082178e72a0dd0aebfd2852d6f58e11c46d5f0bf347ea69f",
        }
        for name, header in expected.items():
            spec = modem.SUBMODES[name]
            with self.subTest(mode=name):
                bits = modem.header_bits(spec.index, 12, spec.sync_band)
                self.assertEqual(np.packbits(bits).tobytes().hex(), header)

    def test_source_install_launches_receive_compatibility_shim(self):
        command = local_host_command(["--help"], frozen=False, executable="python")
        self.assertEqual(command[0], "python")
        self.assertTrue(command[1].endswith("/data2g_receive_host.py"))
        self.assertEqual(command[2:], ["--help"])

    def test_frozen_app_reenters_only_at_server_cli_dispatch(self):
        self.assertEqual(local_host_command(["--help"], frozen=True,
                                            executable="PixelQSO"),
                         ["PixelQSO", "--run-data2g-host", "--help"])

    def test_upstream_receiver_accepts_256_byte_kiss_frame(self):
        before = dict(modem.Accept.of(None, 16.0).max_cw)
        after = dict(receive_accept(0.35).max_cw)
        spec = modem.SUBMODES["n4-qpsk-r1/5"]
        needed = 4 + (258 + codes.payload_bytes(spec) - 1) // codes.payload_bytes(spec)
        self.assertLess(before[spec.name], needed)
        self.assertGreaterEqual(after[spec.name], needed)
        self.assertEqual(receive_accept(0.35).min_score, 0.35)

    def test_legacy_host_fallback_preserves_long_frame_admission(self):
        from data2g.arq import engine
        from data2g.config import max_codewords
        original = modem.Accept.of
        try:
            with patch.object(engine, "receive_accept", None):
                enable_advertised_receive_lengths()
            accepted = dict(modem.Accept.of(None, 16).max_cw)
            spec = modem.SUBMODES["n4-qpsk-r1/5"]
            self.assertEqual(accepted[spec.name], max_codewords(spec.sync_band))
        finally:
            modem.Accept.of = original

    def test_host_configuration_maps_station_devices_and_radio_server(self):
        args = local_host_arguments(
            command_port=8300, kiss_port=8100, callsign="ag7su",
            input_device="WebSDR loopback", output_device="USB Audio",
            rig_host="127.0.0.1", rig_port=4532,
            bandwidth_hz=500, record_dir="captures/data2g")
        pairs = dict(zip(args[::2], args[1::2]))
        self.assertEqual(pairs["--host"], "127.0.0.1")
        self.assertEqual(pairs["--command-port"], "8300")
        self.assertEqual(pairs["--kiss-port"], "8100")
        self.assertEqual(pairs["--mycall"], "AG7SU")
        self.assertEqual(pairs["--input-device"], "WebSDR loopback")
        self.assertEqual(pairs["--output-device"], "USB Audio")
        self.assertEqual(pairs["--rigctld-host"], "127.0.0.1")
        self.assertEqual(pairs["--rigctld-port"], "4532")
        self.assertEqual(pairs["--kiss-bw"], "500")

    def test_audio_device_selector_uses_stable_backend_id(self):
        self.assertEqual(data2g_audio_device_selector(
            b"alsa_input.usb-device.iec958", "Friendly audio input"),
            "alsa_input.usb-device.iec958")
        self.assertEqual(data2g_audio_device_selector(
            b"\xff", "Friendly audio input"), "Friendly audio input")

    def test_host_bandwidth_only_accepts_supported_caps(self):
        with self.assertRaisesRegex(ValueError, "500 or 2400"):
            local_host_arguments(command_port=8300, kiss_port=8100,
                callsign="AG7SU", input_device="", output_device="",
                rig_host="localhost", rig_port=4532, bandwidth_hz=1200,
                record_dir="")


if __name__ == "__main__":
    unittest.main()
