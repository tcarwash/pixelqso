import unittest
from unittest.mock import patch
import numpy as np
import cardmodem as pixel
import modem_envelope as envelope
from copy_combining import SoftCopy, compatible, recovery_attempts, ADMITTED_IDENTITY
from card_backends import get_backend, MODE_REGISTRY

class CopyHeaderTests(unittest.TestCase):
    def test_strong_header_corrects_ten_gf64_errors_and_preserves_mode(self):
        for key in envelope.MODE_IDS:
            grouped = key in envelope.GROUP_MODES or key in envelope.COMPRESSED_MODES
            samples = envelope.GROUP_PAYLOAD_SAMPLES.get(key,48000)
            if key in envelope.COMPRESSED_MODES:
                from compressed_fsk_modem import payload_samples
                samples = payload_samples(key,192)
            symbols = envelope.header_symbols(key,samples,b'x'*16,
                **({'copies':3} if grouped else {}))
            _, permutation = envelope.constants()
            original = symbols[envelope.TRAINING:]
            tones = np.empty_like(original); tones[permutation] = original
            for i in range(10):
                tones[i*2] ^= 1
            received = tones[permutation]
            metrics = np.full((len(received),8), .001)
            metrics[np.arange(len(received)), received] = 1
            header = envelope.decode_header(metrics)
            self.assertEqual(header['mode'],key)
            self.assertEqual(header['identity'], (b'x'*16).hex())
            self.assertEqual(header['corrected_symbols'],10)

    def test_trailer_recovers_identity_when_prefix_is_lost(self):
        card = pixel.example_card()
        backend = get_backend('experimental_qpsk_5s')
        audio = backend.encode(card,1,'cq',-6)
        # Remove the leading common marker, keeping the whole payload/trailer.
        audio = audio[round(envelope.HEADER_SECONDS*48000):]
        copies = envelope.find_copies(audio,48000)
        self.assertEqual(len(copies),1)
        self.assertEqual(copies[0].mode, backend.key)
        self.assertTrue(copies[0].diagnostics['trailer'])
        report = backend.decode(audio,48000)
        self.assertTrue(report['card']['exact'])

    def test_same_card_id_with_different_pixels_is_not_combined(self):
        card = pixel.example_card()
        other = pixel.example_card();other.pixels = [7-i for i in card.pixels]
        key='experimental_qpsk_5s';backend=get_backend(key)
        first = backend.encode(card,1,'cq',None)
        second = backend.encode(other,1,'cq',None)
        audio=np.concatenate((first,second))
        copies=envelope.find_copies(audio,48000)
        self.assertEqual(len(envelope.groups(copies,key)),2)
        groups=[]
        def decode(x,rate):
            groups.append(ADMITTED_IDENTITY.get())
            return {'card':{},'pixels':[]}
        envelope.decode_capture(key,audio,48000,decode,copies=copies)
        self.assertEqual(len(groups),2)
        self.assertNotEqual(groups[0],groups[1])
        self.assertIsNone(ADMITTED_IDENTITY.get())

    def test_mode_routes_receive_all_without_trying_other_codecs(self):
        from app import decode_all_card_backends
        key='experimental_8fsk_ldpc'
        backend=get_backend(key); card=pixel.example_card()
        audio=backend.encode(card,1,'cq',None)
        with patch('card_backends.WeakSignalBackend.decode_payload',side_effect=AssertionError('wrong decoder')):
            result=decode_all_card_backends(audio,48000,include_experimental=True,
                                            preferred_mode='experimental_qpsk_5s')
        self.assertIsNone(result.failure)
        self.assertTrue(result.report['card']['exact'])
        self.assertEqual(result.report['copy_header']['announced_mode'],key)

    def test_unrelated_evidence_and_identities_are_not_pooled(self):
        rng=np.random.default_rng(5)
        a=SoftCopy(rng.normal(size=14080),{},('a',))
        b=SoftCopy(a.evidence.copy(),{},('b',))
        self.assertFalse(compatible(a,b))
        self.assertEqual(len(list(recovery_attempts([a,b]))),2)
        x=SoftCopy(rng.normal(size=14080),{})
        y=SoftCopy(rng.normal(size=14080),{})
        self.assertFalse(compatible(x,y))

    def test_all_registered_experimental_modes_have_wire_ids(self):
        self.assertEqual(set(envelope.MODE_IDS), {k for k,s in MODE_REGISTRY.items() if s.experimental})

    def test_noise_does_not_advertise_a_mode(self):
        for seed in range(3):
            audio=np.random.default_rng(seed).normal(0,.1,48000*8)
            self.assertEqual(envelope.find_copies(audio,48000),[])

class IdentityRecoveryTests(unittest.TestCase):
    def test_native_header_chase_recovers_two_uncertain_gf64_errors(self):
        card=pixel.example_card()
        tones=pixel._encode_avatar_metadata(card,resilient=True,wire_version=4)
        metrics=np.full((len(tones),8), .001)
        metrics[np.arange(len(tones)),tones]=1
        for at in (7,17):
            actual=tones[at*2+1]
            metrics[at*2+1,actual]=.45
            metrics[at*2+1,actual^1]=.5
        result=pixel._decode_avatar_metadata_soft(metrics)
        self.assertEqual(result['callsign'],card.callsign)
        self.assertEqual(result['image_id'],pixel._decode_avatar_metadata(tones)['image_id'])

    def test_options_exposes_shared_copy_count(self):
        import os,tempfile
        from PySide6.QtWidgets import QApplication
        import app
        qt=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'PIXELQSO_DATA_DIR':folder}), patch('app.QTimer.singleShot'):
            window=app.MainWindow()
            try:
                window.show_experimental_modes.setChecked(True)
                window.backend_combo.setCurrentIndex(window.backend_combo.findData('experimental'))
                with patch('app.QDialog.exec'):
                    window.open_station_settings()
                self.assertFalse(window.repeat_count.isHidden())
                for key in MODE_REGISTRY:
                    at=window.mode_combo.findData(key)
                    window.mode_combo.setCurrentIndex(at)
                    self.assertEqual(window.repeat_count.maximum(),20)
                window.station_dialog.adjustSize()
                window.station_dialog.grab().save('work/copy-hardening-live/options.png')
            finally:
                window.rx_timer.stop();window.close();qt.processEvents()

class LiveCopyCacheTests(unittest.TestCase):
    def test_snapshots_are_replaced_and_survive_window_trim(self):
        cache=envelope.CopyCaptureCache()
        diagnostic={'carrier_offset_hz':0}
        copy=envelope.AnnouncedCopy('standard','a',10,410,diagnostic)
        cache.update(np.ones(410),8000,[copy],100)
        later=envelope.AnnouncedCopy('standard','a',0,600,diagnostic)
        cache.update(np.ones(600),8000,[later],110)
        audio,copies=cache.materialize()
        self.assertEqual(len(copies),1)
        self.assertEqual(len(audio),600)
        new=envelope.AnnouncedCopy('standard','a',0,500,diagnostic)
        cache.update(np.ones(500),8000,[new],8110)
        audio,copies=cache.materialize()
        self.assertEqual(len(copies),2)
        self.assertEqual(len(audio),1100)

    def test_memory_budget_and_card_isolation(self):
        cache=envelope.CopyCaptureCache(max_bytes=4000,max_groups=2)
        for i in range(3):
            copy=envelope.AnnouncedCopy('standard',str(i),0,500,{'carrier_offset_hz':0})
            cache.update(np.ones(500),8000,[copy],i*8000)
        audio,copies=cache.materialize()
        self.assertLessEqual(audio.nbytes,4000)
        self.assertEqual(set(c.identity for c in copies),{'1','2'})
        cache.clear()
        self.assertEqual(cache.materialize()[1],[])

class CompletedIdentityTests(unittest.TestCase):
    def test_verified_native_card_must_match_common_content_id(self):
        import weak_signal_modem as weak
        card=pixel.example_card(); other=pixel.example_card()
        other.pixels=[7-v for v in other.pixels]
        key='experimental_qpsk_5s'
        payload=weak.encode(other,1,'cq',None)
        wrong=envelope.transmit(key,card,payload,1,'cq',None)
        with self.assertRaisesRegex(ValueError,'protected copy identity'):
            get_backend(key).decode(wrong,48000)

class RawCopyTests(unittest.TestCase):
    def test_raw_mode_pools_copies_but_remains_unverified(self):
        card=pixel.example_card();backend=get_backend('fast_avatar')
        report=backend.decode(backend.encode(card,2,'cq',None),48000)
        self.assertEqual(report['card']['received_copies'],2)
        self.assertFalse(report['card']['exact'])
        self.assertEqual(report['pixels'],pixel.unpack_indices(pixel.minimal_avatar_payload(card),1024))
