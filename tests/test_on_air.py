import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import wave

import numpy as np

import cardmodem as modem
from on_air import SessionCollector, SessionRecorder, card_reference, decoded_reference, signal_data
from tools.on_air_test import session_report, analyze


class OnAirTests(unittest.TestCase):
    def test_legacy_preview_cannot_break_telemetry(self):
        self.assertIsNone(decoded_reference({"pixels": [[0, 0, 0]], "card": {"exact": False}}))

    def test_authenticated_two_station_session_and_exact_pcm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credential = root / "credential"
            credential.write_text("test-session-credential-" * 2)
            collector = SessionCollector(root / "collector", credential.read_text())
            url = f"http://127.0.0.1:{collector.port}"
            try:
                with self.assertRaises(HTTPError) as error:
                    urlopen(url + "/sessions/shared/events")
                self.assertEqual(error.exception.code, 401)
                recorders = []
                for station in ("tx", "rx"):
                    recorders.append(SessionRecorder({"session_id": "shared", "station_id": station,
                        "output_dir": str(root / "local"), "collector_url": url,
                        "token_file": str(credential), "source": "synthetic-test"}))
                card = modem.example_card()
                reference = card_reference(card)
                pcm = np.asarray([0, -32768, 32767, 4, -4] * 1000, dtype="<i2").tobytes()
                recorders[0].emit("tx_start", {"reference": reference}, pcm)
                recorders[1].emit("rx_decode", {"reference": {**reference, "exact": True}})
                for recorder in recorders:
                    self.assertTrue(recorder.close())
                    self.assertEqual((recorder.errors, recorder.network_errors, recorder.dropped), (0, 0, 0))
                wav = next((root / "local").rglob("*.wav"))
                with wave.open(str(wav)) as file:
                    self.assertEqual(file.readframes(file.getnframes()), pcm)
                request = Request(url + "/sessions/shared/events", headers={"Authorization": "Bearer " + credential.read_text()})
                with urlopen(request) as response:
                    events = json.load(response)["events"]
                self.assertEqual({e["station_id"] for e in events}, {"tx", "rx"})
                self.assertTrue(all("collector_utc_ns" in e for e in events))
                rows, matches = session_report(root / "local", root / "review")
                self.assertEqual(len(rows), 2)
                self.assertEqual(len(matches), 1)
                # Expected references never turn a partial/unverified decode
                # into a matched result.
                recorder = SessionRecorder({"session_id": "shared", "station_id": "rx2", "output_dir": str(root / "local")})
                recorder.emit("rx_decode", {"reference": {**reference, "exact": False}})
                recorder.close()
                self.assertEqual(len(session_report(root / "local", root / "review")[1]), 1)
                bad = {"schema": 1, "event_id": "event", "session_id": "../escape", "station_id": "rx", "kind": "rx_decode"}
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(url + "/events", data=json.dumps(bad).encode(),
                                    headers={"Authorization": "Bearer " + credential.read_text()}))
                self.assertEqual(error.exception.code, 400)
            finally:
                collector.close()

    def test_failed_collector_retains_local_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credential = root / "credential"
            credential.write_text("test-session-credential-" * 2)
            recorder = SessionRecorder({"session_id": "offline", "station_id": "rx", "output_dir": directory,
                                       "collector_url": "http://127.0.0.1:1", "token_file": str(credential)})
            recorder.emit("rx_decode", {"error": "no frame"})
            self.assertTrue(recorder.close())
            self.assertEqual(recorder.network_errors, 1)
            self.assertTrue((recorder.folder / "events.jsonl").exists())

    def test_host_transfer_report_matches_only_verified_raster_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder = SessionRecorder({"session_id": "host-test", "station_id": "tx",
                                        "output_dir": str(root)})
            card = modem.example_card()
            reference = card_reference(card)
            recorder.emit("host_tx_start", {"reference": reference, "mode": "qpsk-r1/2"})
            recorder.emit("host_rx_card", {"reference": {**reference, "exact": True}})
            other = {**reference, "pixel_sha256": "0" * 64, "exact": True}
            recorder.emit("host_rx_card", {"reference": other})
            self.assertTrue(recorder.close())
            _rows, matches = session_report(root, root / "review")
            self.assertEqual(len(matches), 1)
            self.assertTrue(matches[0]["verified_reference_match"])

    def test_levels_and_spectrum_are_digital_not_rf(self):
        rate = 8000
        audio = .5 * np.sin(2 * np.pi * 500 * np.arange(rate) / rate)
        levels, spectra = signal_data(audio, rate)
        self.assertAlmostEqual(levels["rms"], .5 / np.sqrt(2), places=5)
        self.assertEqual(len(levels["frames"]), 4)
        self.assertAlmostEqual(spectra["frequency_hz"][np.argmax(spectra["power"][0])], 500)
        self.assertIn("uncalibrated", levels["level_units"])

    def test_offline_wav_replay_with_acquisition_and_symbol_evidence(self):
        from card_backends import get_backend
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = get_backend("resilient_100", "centered")
            modem.write_wav(root / "received.wav", backend.encode(modem.example_card(), 1, "cq", None))
            result = analyze(root / "received.wav", root / "analysis", backend.key, "centered")
            self.assertTrue(result["reference"]["exact"])
            self.assertIn("frequency_offset_hz", result["report"]["card"]["acquisition"])
            self.assertTrue((root / "analysis" / "symbol_metrics.npz").exists())
            modem.write_wav(root / "silence.wav", np.zeros(48000, dtype=np.float32))
            failed = analyze(root / "silence.wav", root / "failed", backend.key, "centered")
            self.assertIsNone(failed["report"])
            self.assertTrue((root / "failed" / "analysis.json").exists())


if __name__ == "__main__":
    unittest.main()
