"""Grouped framing must save airtime without weakening identity/integrity gates."""
import unittest
from unittest.mock import patch
import numpy as np
import cardmodem as pixel
import modem_envelope as envelope
from card_backends import GROUPED_MODES, get_backend, available_modes

class GroupedFSKTests(unittest.TestCase):
    def test_modes_roundtrip_and_actual_airtime(self):
        card = pixel.example_card()
        for key, native in GROUPED_MODES.items():
            with self.subTest(key=key):
                backend = get_backend(key)
                audio = backend.encode(card, 3, 'cq', -6)
                self.assertAlmostEqual(len(audio)/48000, backend.estimate_seconds(card,3), places=6)
                self.assertLess(len(audio),len(get_backend(native).encode(card,3,'cq',-6)))
                copies = envelope.find_copies(audio,48000)
                self.assertEqual(len(copies),3)
                self.assertEqual([c.diagnostics['copy_index'] for c in copies],[0,1,2])
                self.assertTrue(all(c.diagnostics['version']==2 for c in copies))
                report = backend.decode(audio,48000)
                self.assertTrue(report['card']['exact'])
                self.assertEqual(report['pixels'],pixel.unpack_indices(pixel.minimal_avatar_payload(card),1024))
                self.assertEqual(report['card']['avatar_mode'],key)
                self.assertEqual(report['copy_header']['copy_header_version'],2)
                self.assertNotIn(key,dict(available_modes()))
                self.assertIn(key,dict(available_modes(include_experimental=True)))
                for count in (0,21,1.5):
                    with self.assertRaises(ValueError):
                        backend.estimate_seconds(card,count)
                    with self.assertRaises(ValueError):
                        backend.encode(card,count,'cq',None)

    def test_header_count_is_protected_and_bounded(self):
        key='experimental_8fsk_grouped'
        symbols=envelope.header_symbols(key,240240,b'x'*16,copies=20)
        tones=symbols[envelope.TRAINING:]
        metrics=np.zeros((len(tones),8));metrics[np.arange(len(tones)),tones]=1
        header=envelope.decode_header(metrics)
        self.assertEqual(header['group_copies'],20)
        self.assertEqual(header['payload_samples'],240240)
        with self.assertRaises(ValueError):
            envelope.header_symbols(key,240240,b'x'*16,copies=21)

    def test_soft_opening_header_recovers_beyond_hard_rs_limit(self):
        symbols=envelope.header_symbols('experimental_8fsk_grouped',240240,b'x'*16,copies=4)
        _,permutation=envelope.constants()
        tones=np.empty_like(symbols[envelope.TRAINING:]);tones[permutation]=symbols[envelope.TRAINING:]
        metrics=np.full((len(tones),8),.001)
        metrics[np.arange(len(tones)),tones]=1
        for i in range(11):
            correct=tones[2*i];metrics[2*i,correct]=.4 if i==0 else .01
            metrics[2*i,correct^1]=.41 if i==0 else 1
        transmitted=metrics[permutation]
        with self.assertRaises(ValueError):
            envelope.decode_header(transmitted)
        result=envelope.decode_header(transmitted,soft=True)
        self.assertEqual(result['identity'],(b'x'*16).hex())
        self.assertEqual(result['group_copies'],4)

    def test_known_group_prefix_aids_header_but_crc_remains_required(self):
        symbols=envelope.header_symbols('experimental_16fsk_grouped',270720,b'z'*16,copies=8)
        _,permutation=envelope.constants()
        tones=np.empty_like(symbols[envelope.TRAINING:]);tones[permutation]=symbols[envelope.TRAINING:]
        metrics=np.full((len(tones),8),.001)
        metrics[np.arange(len(tones)),tones]=1
        for i in range(13):
            correct=tones[2*i];metrics[2*i,correct]=.001;metrics[2*i,correct^1]=2
        received=metrics[permutation]
        with self.assertRaises(ValueError):
            envelope.decode_header(received)
        result=envelope.decode_header(received,soft=True)
        self.assertEqual(result['identity'],(b'z'*16).hex())
        self.assertEqual(result['group_copies'],8)
        # A coherent-looking codeword with an invalid CRC cannot be admitted.
        body=envelope.BODY.pack(b'PQCH',2,15,270720|(7<<27),b'z'*16)
        from experimental_fec import rs_encode
        bad=rs_encode(pixel._bytes_to_gf64(body+b'\0'*4),envelope.PARITY)
        badtones=np.asarray(pixel._gf64_to_tones(bad))[permutation]
        badmetrics=np.zeros((len(badtones),8));badmetrics[np.arange(len(badtones)),badtones]=1
        with self.assertRaisesRegex(ValueError,'checksum'):
            envelope.decode_header(badmetrics,soft=True)

    def test_partial_snapshots_do_not_duplicate_copies(self):
        card=pixel.example_card();key='experimental_8fsk_grouped';backend=get_backend(key)
        audio=backend.encode(card,3,'cq',None)
        copies=envelope.find_copies(audio,48000)
        cache=envelope.CopyCaptureCache()
        for end in (copies[0].end,copies[1].start+48000,len(audio)):
            snapshot=audio[:end];found=envelope.find_copies(snapshot,48000)
            cache.update(snapshot,48000,found)
        pooled,found=cache.materialize()
        self.assertEqual(len(found),3)
        report=envelope.decode_capture(key,pooled,8000,backend.decode_payload,
            copies=found,copy_decoder=backend.decode_payload_copies)
        self.assertTrue(report['card']['exact'])

    def test_missing_opening_header_and_noise_reject(self):
        card=pixel.example_card();key='experimental_8fsk_grouped';backend=get_backend(key)
        audio=backend.encode(card,2,'cq',None)
        with self.assertRaises(ValueError):
            backend.decode(audio[round(envelope.HEADER_SECONDS*48000):],48000)
        noise=np.random.default_rng(234).normal(0,.1,len(audio))
        with self.assertRaises(ValueError):
            backend.decode(noise,48000)

    def test_saved_wav_uses_selected_and_auto_application_routes(self):
        from app import DecodeWorker
        from PySide6.QtWidgets import QApplication
        from pathlib import Path
        import tempfile
        from card_transfer import CardAssemblyService
        qt=QApplication.instance() or QApplication([])
        card=pixel.example_card()
        with tempfile.TemporaryDirectory() as folder:
            for key in GROUPED_MODES:
                path=Path(folder)/(key+'.wav')
                pixel.write_wav(path,get_backend(key).encode(card,1,'cq',None))
                for mode in (key,'auto'):
                    received=[]
                    worker=DecodeWorker(path,Path(folder)/'state.json',mode=mode,include_experimental=True)
                    worker.signals.finished.connect(received.append)
                    worker.run()
                    self.assertEqual(len(received),1)
                    _,report,error=received[0]
                    self.assertIsNone(error)
                    merged,_=CardAssemblyService().merge_preview(report)
                    self.assertTrue(merged['assembly_event']['verified_complete'])
                    self.assertEqual(merged['pixels'],pixel.unpack_indices(pixel.minimal_avatar_payload(card),1024))
        qt.processEvents()

    def test_unannounced_receive_all_skips_grouped_decoders(self):
        from app import decode_all_card_backends
        import experimental_burst_modem as burst
        card=pixel.example_card()
        audio=burst.encode('experimental_8fsk_ldpc',card,'cq',None)
        with patch('card_backends.GroupedFSKBackend.decode',side_effect=AssertionError('blind grouped decode')):
            result=decode_all_card_backends(audio,48000,include_experimental=True)
        self.assertIsNone(result.failure)
        self.assertTrue(result.report['card']['exact'])

    def test_receive_all_routes_group_and_content_mismatch_rejects(self):
        from app import decode_all_card_backends
        card=pixel.example_card();key='experimental_16fsk_grouped';backend=get_backend(key)
        audio=backend.encode(card,2,'cq',None)
        with patch('card_backends.WeakSignalBackend.decode_payload',side_effect=AssertionError('wrong mode')):
            outcome=decode_all_card_backends(audio,48000,include_experimental=True)
        self.assertIsNone(outcome.failure)
        self.assertTrue(outcome.report['card']['exact'])
        copies=envelope.find_copies(audio,48000)
        for copy in copies:
            copy.identity='00'*16
        with self.assertRaisesRegex(ValueError,'protected copy identity'):
            envelope.decode_capture(key,audio,48000,backend.decode_payload,
                copies=copies,copy_decoder=backend.decode_payload_copies)

if __name__=='__main__':
    unittest.main()
