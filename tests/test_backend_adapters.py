import unittest

from backend_adapters import (AdapterKind, DATA2G_ADAPTER, EXPERIMENTAL_ADAPTER,
                              AdapterCapabilities, IntegrityScope,
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
        class CompatibleAdapter:
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


if __name__ == "__main__":
    unittest.main()
