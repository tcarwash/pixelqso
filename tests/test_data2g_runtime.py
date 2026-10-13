import unittest

from data2g import modem
from data2g.arq.engine import KISS_FRAME_BYTES, receive_accept
from data2g import codes
from data2g_runtime import (data2g_audio_device_selector, local_host_arguments,
                            local_host_command)


class Data2GRuntimeTests(unittest.TestCase):
    def test_source_install_launches_snr_host_wrapper(self):
        command = local_host_command(["--help"], frozen=False, executable="python")
        self.assertEqual(command[0], "python")
        self.assertTrue(command[1].endswith("/data2g_receive_host.py"))
        self.assertEqual(command[2:], ["--help"])

    def test_frozen_app_reenters_only_at_server_cli_dispatch(self):
        self.assertEqual(local_host_command(["--help"], frozen=True,
                                            executable="PixelQSO"),
                         ["PixelQSO", "--run-data2g-host", "--help"])

    def test_upstream_receiver_accepts_256_byte_kiss_frame(self):
        self.assertEqual(KISS_FRAME_BYTES, 256)
        before = dict(modem.Accept.of(None, 16.0).max_cw)
        after = dict(receive_accept(0.35).max_cw)
        mode = modem.SUBMODES["n4-qpsk-r1/5"]
        need = 4 + (2 + 256 + codes.payload_bytes(mode) - 1) // codes.payload_bytes(mode)
        self.assertLess(before[mode.name], need)
        self.assertGreaterEqual(after[mode.name], need)
        self.assertEqual(receive_accept(0.35).min_score, 0.35)

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
