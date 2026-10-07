import unittest
from unittest.mock import patch

from backend_adapters import (AdapterKind, DATA2G_ADAPTER, EXPERIMENTAL_ADAPTER,
                              AdapterCapabilities, ExperimentalLocalAdapter,
                              DecodeFailureKind, IntegrityScope, ReceiveStartError,
                              adapter_for_backend_selection, register_adapter)


class _HostSession:
    def __init__(self):
        self.calls = []

    def start(self, timeout=None):
        self.calls.append(("discover", timeout))
        return [type("Mode", (), {"name": "n4-qpsk-r1/2"})()]

    def check_broadcast_modes(self, names):
        self.calls.append(("validate", tuple(names)))
        return {"n4-qpsk-r1/2": None}

    def set_mode(self, name):
        self.calls.append(("mode", name))

    def send_frame(self, tag, frame):
        self.calls.append(("frame", tag, frame))

    def close(self, timeout=5.0):
        self.calls.append(("close", timeout))
        return True


class BackendAdapterTests(unittest.TestCase):
    def test_product_selection_resolves_backend_independently_of_mode(self):
        self.assertIs(adapter_for_backend_selection("data2g"), DATA2G_ADAPTER)
        self.assertIs(adapter_for_backend_selection("experimental"), EXPERIMENTAL_ADAPTER)

    def test_internal_registry_can_add_a_compatible_experimental_adapter(self):
        class CompatibleAdapter(ExperimentalLocalAdapter):
            key = "test-compatible"
            capabilities = AdapterCapabilities(
                AdapterKind.LOCAL_IMAGE_MODEM, "pixelqso", "pixelqso_cat_or_test_audio",
                "local_waveform_decode", "local_audio_completion",
                IntegrityScope.CHECKED_REGION, True, True, True)

        adapter = CompatibleAdapter()
        register_adapter(adapter)
        try:
                self.assertIs(adapter_for_backend_selection("experimental", adapter.key), adapter)
        finally:
            # Keep the process-wide registry isolated for the remaining tests.
            from backend_adapters import ADAPTERS
            ADAPTERS.pop(adapter.key, None)

    def test_internal_registry_rejects_adapters_missing_kind_specific_operations(self):
        class IncompleteAdapter:
            key = "test-incomplete"
            capabilities = AdapterCapabilities(
                AdapterKind.LOCAL_IMAGE_MODEM, "pixelqso", "pixelqso_cat_or_test_audio",
                "local_waveform_decode", "local_audio_completion",
                IntegrityScope.CHECKED_REGION, True, True, True)

        with self.assertRaisesRegex(ValueError, "decode_capture, start_receive, stop_receive"):
            register_adapter(IncompleteAdapter())

    def test_capabilities_report_distinct_ownership_and_integrity(self):
        host = DATA2G_ADAPTER.capabilities
        local = EXPERIMENTAL_ADAPTER.capabilities
        self.assertEqual(host.kind, AdapterKind.FRAME_TRANSPORT)
        self.assertEqual(host.radio_owner, "data2g_host")
        self.assertEqual(host.integrity, IntegrityScope.CHECKED_FRAME)
        self.assertFalse(host.spectrum)
        self.assertEqual(local.kind, AdapterKind.LOCAL_IMAGE_MODEM)
        self.assertEqual(local.audio_owner, "pixelqso")
        self.assertTrue(local.provisional_pixels)
        self.assertTrue(local.receive_all_modes)

    def test_data2g_adapter_routes_lifecycle_and_frames_to_supported_session_api(self):
        session = _HostSession()
        modes, supported = DATA2G_ADAPTER.discover(session, timeout=3)
        self.assertEqual(modes[0].name, "n4-qpsk-r1/2")
        self.assertIsNone(supported[modes[0].name])
        DATA2G_ADAPTER.select_mode(session, modes[0].name)
        DATA2G_ADAPTER.submit_frame(session, b"\x00\x01", b"frame")
        DATA2G_ADAPTER.disconnect(session, timeout=2)
        self.assertEqual(session.calls, [
            ("discover", 3), ("validate", ("n4-qpsk-r1/2",)),
            ("mode", "n4-qpsk-r1/2"), ("frame", b"\x00\x01", b"frame"),
            ("close", 2),
        ])

    def test_data2g_stop_cancels_queued_work_but_reports_inflight_uncertainty(self):
        queued = [b"later-1", b"later-2"]
        stopped = DATA2G_ADAPTER.request_stop(queued, b"in-flight-tag")
        self.assertEqual(stopped.queued_cancelled, 2)
        self.assertTrue(stopped.in_flight_may_complete)
        self.assertEqual(queued, [])
        stopped = DATA2G_ADAPTER.request_stop(queued, None)
        self.assertEqual(stopped.queued_cancelled, 0)
        self.assertFalse(stopped.in_flight_may_complete)

    def test_local_adapter_delegates_waveform_operations(self):
        class LocalBackend:
            def encode(self, *args):
                return args

            def decode(self, *args):
                return args

        backend = LocalBackend()
        self.assertEqual(EXPERIMENTAL_ADAPTER.encode(
            backend, "card", 2, "cq", None), ("card", 2, "cq", None))
        self.assertEqual(EXPERIMENTAL_ADAPTER.decode(
            backend, b"audio", 48000), (b"audio", 48000))

    def test_local_adapter_normalizes_decode_miss_and_backend_failure(self):
        class NoMatchBackend:
            def decode(self, *_args):
                raise ValueError("no valid avatar identity header received yet")

        no_match = EXPERIMENTAL_ADAPTER.decode_capture(NoMatchBackend(), b"noise", 48000)
        self.assertIsNone(no_match.report)
        self.assertEqual(no_match.failure.kind, DecodeFailureKind.NO_MATCH)
        self.assertTrue(no_match.failure.retryable)
        self.assertEqual(no_match.failure.adapter_key, EXPERIMENTAL_ADAPTER.key)

        class BrokenBackend:
            def decode(self, *_args):
                raise RuntimeError("decoder worker failed")

        failed = EXPERIMENTAL_ADAPTER.decode_capture(BrokenBackend(), b"audio", 48000)
        self.assertIsNone(failed.report)
        self.assertEqual(failed.failure.kind, DecodeFailureKind.BACKEND_ERROR)
        self.assertFalse(failed.failure.retryable)

    def test_legacy_wav_decode_is_normalized_and_preserves_completion_evidence(self):
        packet = type("Packet", (), {"packet_type": 1, "card_id": 9, "seq": 2,
                                     "corrected_bits": 0, "payload": b"payload"})()
        card = {"card_id": 9, "exact": True}
        with patch("backend_adapters.modem.decode_wav_with_state",
                   return_value=([packet], [], 48000, {"profile": "wide"})), \
                patch("backend_adapters.modem.reconstruct", return_value=(card, [])), \
                patch("backend_adapters.modem.is_complete_card", return_value=True):
            outcome = EXPERIMENTAL_ADAPTER.decode_legacy_wav("capture.wav", "state.json")

        self.assertIsNone(outcome.failure)
        self.assertEqual(outcome.report["valid_packets"][0]["payload_hex"], b"payload".hex())
        self.assertTrue(outcome.report["complete"])
        self.assertEqual(outcome.report["card"], card)

    def test_legacy_audio_without_card_identity_is_a_retryable_miss(self):
        with patch("backend_adapters.modem.select_capture_evidence",
                   return_value=("profile",([], 0, 0, 1))) as select, \
                patch("backend_adapters.modem.receive_capture_evidence", return_value=([], 0, 0, 1)), \
                patch("backend_adapters.modem.decode_packet_evidence", return_value=([], [])), \
                patch("backend_adapters.modem.reconstruct", return_value=({"card_id": None}, [])):
            outcome = EXPERIMENTAL_ADAPTER.decode_legacy_audio([0.0] * 44100, 44100)

        self.assertIsNone(outcome.report)
        self.assertEqual(outcome.failure.kind, DecodeFailureKind.NO_MATCH)
        self.assertTrue(outcome.failure.retryable)
        self.assertEqual(select.call_args.args[1],8000)
        self.assertEqual(len(select.call_args.args[0]),8000)

    def test_local_adapter_decode_outcome_contains_one_result(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            from backend_adapters import DecodeOutcome
            DecodeOutcome()

    def test_local_receive_adapter_owns_audio_and_udp_resource_lifecycle(self):
        class Device:
            def isFormatSupported(self, audio_format):
                return True

        class Source:
            def __init__(self, device, audio_format, parent):
                self.stopped = 0

            def start(self):
                return "audio-stream"

            def stop(self):
                self.stopped += 1

        sources = []

        def source_factory(*args):
            source = Source(*args)
            sources.append(source)
            return source

        audio = EXPERIMENTAL_ADAPTER.start_receive(
            device=Device(), audio_format="48k-mono", parent=None,
            audio_source_factory=source_factory)
        self.assertIs(audio.audio_source, sources[0])
        self.assertEqual(audio.audio_stream, "audio-stream")
        self.assertEqual(EXPERIMENTAL_ADAPTER.stop_receive(audio), ())
        self.assertEqual(sources[0].stopped, 1)
        self.assertEqual(EXPERIMENTAL_ADAPTER.stop_receive(audio), ())
        self.assertEqual(sources[0].stopped, 1)

        class Socket:
            def __init__(self):
                self.closed = 0
                self.deleted = 0

            def close(self):
                self.closed += 1

            def deleteLater(self):
                self.deleted += 1

        sockets = []
        def socket_factory(parent):
            sock = Socket()
            sockets.append(sock)
            return sock

        udp = EXPERIMENTAL_ADAPTER.start_receive(
            device=None, audio_format=None, parent=None,
            audio_source_factory=source_factory, test_port=40001,
            test_socket_factory=socket_factory,
            test_bind=lambda sock, port: port == 40001)
        self.assertTrue(udp.test_mode)
        self.assertIs(udp.test_socket, sockets[0])
        self.assertEqual(EXPERIMENTAL_ADAPTER.stop_receive(udp), ())
        self.assertEqual((sockets[0].closed, sockets[0].deleted), (1, 1))

    def test_local_receive_adapter_reports_failed_acquisition_and_cleans_up(self):
        class UnsupportedDevice:
            def isFormatSupported(self, audio_format):
                return False

        with self.assertRaisesRegex(ReceiveStartError, "does not support"):
            EXPERIMENTAL_ADAPTER.start_receive(
                device=UnsupportedDevice(), audio_format=None, parent=None,
                audio_source_factory=lambda *_args: self.fail("unsupported format must not open"))

        class FailedSource:
            def __init__(self):
                self.stopped = 0

            def start(self):
                return None

            def stop(self):
                self.stopped += 1

        failed = FailedSource()
        with self.assertRaisesRegex(ReceiveStartError, "could not open"):
            EXPERIMENTAL_ADAPTER.start_receive(
                device=type("Device", (), {"isFormatSupported": lambda *_: True})(),
                audio_format=None, parent=None,
                audio_source_factory=lambda *_args: failed)
        self.assertEqual(failed.stopped, 1)


if __name__ == "__main__":
    unittest.main()
