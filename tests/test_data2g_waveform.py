"""Real modem/KISS/card assembly checks without audio devices or a radio."""
import unittest

import numpy as np
from data2g import modem
from data2g.arq import phy
from data2g.arq.engine import receive_accept
from data2g.config import FS
from data2g.kisslink import KissLink

import cardmodem
from backend_adapters import DATA2G_ADAPTER
from card_transfer import CardAssemblyService
from data2g_receive_host import enable_advertised_receive_lengths


class Data2GWaveformTests(unittest.TestCase):
    def test_fragmented_card_through_narrow_and_wide_modems(self):
        card = cardmodem.example_card()
        expected = cardmodem.unpack_indices(cardmodem.minimal_avatar_payload(card), 1024)
        for mode in ("n10-qpsk-r1/2", "n10-qpsk-r1/5", "qpsk-r1/5"):
            with self.subTest(mode=mode):
                tx, rx = KissLink(broadcast=mode), KissLink()
                tx_port, rx_port = tx.open("PIXELQSO"), rx.open("PIXELQSO")
                tx.set_mode(tx_port, mode)
                service = CardAssemblyService()
                frames = DATA2G_ADAPTER.frame_card(card, 256, "cq")
                self.assertGreater(len(frames), 1)
                original = modem.Accept.of
                try:
                    enable_advertised_receive_lengths()
                    accept = receive_accept()
                    for frame in frames:
                        tx.enqueue(frame, tx_port)
                        burst = tx.next_burst()
                        audio = phy.tx_audio(burst)
                        received = modem.receive(
                            np.r_[np.zeros(FS // 2), audio, np.zeros(FS // 2)],
                            bands=[modem.SUBMODES[mode].sync_band], accept=accept)
                        delivered = rx.on_burst(received)
                        self.assertEqual(delivered, [(rx_port, frame)])
                        _, metadata = service.feed_transport_frame("PIXELQSO", delivered[0][1])
                        pixels = metadata.pop("pixels")
                        metadata["palette"] = cardmodem.MINIMAL_AVATAR_PALETTE
                        report, _ = service.merge_preview({"card": metadata, "pixels": pixels})
                    self.assertTrue(report["assembly_event"]["verified_complete"])
                    self.assertEqual(report["pixels"], expected)
                    self.assertEqual(report["card"]["card_id"], card.card_id)
                finally:
                    modem.Accept.of = original
