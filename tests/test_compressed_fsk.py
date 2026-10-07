import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import cardmodem as pixel
import weak_signal_modem as weak
import compressed_fsk_modem as compressed
import modem_envelope as envelope
from card_backends import get_backend,available_modes

class CompressionTests(unittest.TestCase):
    def card(self,kind):
        card=pixel.example_card()
        if kind=='flat':card.pixels=[2]*1024
        if kind=='stripes':card.pixels=[(x//4)%8 for y in range(32) for x in range(32)]
        if kind=='random':
            card.pixels=np.random.default_rng(904).integers(0,8,1024).tolist()
            card.palette=list(pixel.MINIMAL_AVATAR_PALETTE)
        return card

    def test_lossless_selection_and_no_expansion(self):
        for kind in ('example','flat','stripes','random'):
            card=self.card(kind);native=weak.packet(card,'cq',-6)
            packet,capacity,codec=compressed.pack(card,'cq',-6)
            restored,info=compressed.unpack(packet.ljust(capacity,b'\0'))
            self.assertEqual(restored,native)
            self.assertLessEqual(len(packet),422)
            self.assertEqual(info['packet_bytes'],len(packet))
            if kind=='random':self.assertEqual((capacity,codec),(422,0))
            else:self.assertLess(capacity,422)

    def test_all_codecs_reconstruct_exact_bytes(self):
        native=weak.packet(self.card('example'))
        raster=native[weak.HEADER.size:-4]
        for codec,body in ((1,compressed._deflate(raster)),(2,compressed._runs(raster)),
                           (3,compressed._deflate(compressed._row_xor(raster)))):
            wire=b'PQC1'+bytes((codec,))+native[5:weak.HEADER.size]+struct.pack('>H',len(body))+body+native[-4:]
            capacity=next(c for c in compressed.CAPACITIES if len(wire)<=c)
            self.assertEqual(compressed.unpack(wire.ljust(capacity,b'\0'))[0],native)

    def test_bounded_decompression_corruption_and_padding(self):
        with self.assertRaises(ValueError):compressed._inflate(compressed._deflate(b'x'*1000000))
        raster=b'\0'*384
        with self.assertRaises(ValueError):compressed._inflate(compressed._deflate(raster)+b'extra')
        with self.assertRaises(ValueError):compressed._inflate(compressed._deflate(raster)[:-1])
        with self.assertRaises(ValueError):compressed._unruns(bytes([255])*33)
        with self.assertRaises(ValueError):compressed._unruns(b'')
        packet,capacity,_=compressed.pack(self.card('flat'))
        wire=bytearray(packet.ljust(capacity,b'\0'));wire[-1]=1
        with self.assertRaises(ValueError):compressed.unpack(bytes(wire))
        wire=bytearray(packet.ljust(capacity,b'\0'));wire[len(packet)-1]^=1
        with self.assertRaisesRegex(ValueError,'CRC'):compressed.unpack(bytes(wire))
        with self.assertRaises(ValueError):compressed.unpack(b'PQC9'+b'\0'*124)

    def test_same_pixels_with_different_encodings_have_different_copy_identity(self):
        import experimental_burst_modem as burst
        card=self.card('example');native=weak.packet(card)
        key='experimental_8fsk_compressed';identities=[]
        raster=native[weak.HEADER.size:-4]
        for codec,body in ((1,compressed._deflate(raster)),(2,compressed._runs(raster))):
            wire=b'PQC1'+bytes((codec,))+native[5:weak.HEADER.size]+struct.pack('>H',len(body))+body+native[-4:]
            capacity=next(c for c in compressed.CAPACITIES if len(wire)<=c)
            restored,info=compressed.unpack(wire.ljust(capacity,b'\0'))
            report=burst._whole_report(key,restored,info)
            identities.append(envelope.report_content_id(key,report))
        self.assertNotEqual(*identities)
        self.assertEqual(identities[0],envelope.content_id(key,card,'card',None))

    def test_all_shortened_codes_correct_channel_errors(self):
        for capacity in compressed.CAPACITIES:
            rng=np.random.default_rng(capacity)
            info=rng.integers(0,2,capacity*8,dtype=np.uint8)
            code=compressed.code(capacity);coded=code.encode(info)[0]
            llrs=12*(1-2*coded.astype(float))
            llrs[rng.choice(len(llrs),8,replace=False)] *= -.15
            decoded,_=weak.decode_ldpc(llrs,code_instance=code)
            np.testing.assert_array_equal(decoded,info)

    def test_header_profiles_validate_version_length_and_count(self):
        for key in compressed.MODES:
            for capacity in compressed.CAPACITIES:
                symbols=envelope.header_symbols(key,compressed.payload_samples(key,capacity),b'x'*16,copies=20)
                tones=symbols[envelope.TRAINING:];metrics=np.zeros((len(tones),8))
                metrics[np.arange(len(tones)),tones]=1
                header=envelope.decode_header(metrics)
                self.assertEqual(header['version'],3)
                self.assertEqual(header['group_copies'],20)
                self.assertEqual(compressed.capacity_for_samples(key,header['payload_samples']),capacity)
            with self.assertRaises(ValueError):envelope.header_symbols(key,48000,b'x'*16,copies=2)

class CompressedModemTests(unittest.TestCase):
    def test_real_waveforms_for_compressed_and_raw_cards(self):
        maker=CompressionTests()
        for key,native in compressed.MODES.items():
            grouped=native.replace('_ldpc','_grouped')
            for kind in ('example','random'):
                card=maker.card(kind);backend=get_backend(key)
                audio=backend.encode(card,2,'cq',None)
                self.assertAlmostEqual(len(audio)/48000,backend.estimate_seconds(card,2),places=6)
                old=get_backend(grouped).estimate_seconds(card,2)
                self.assertLessEqual(len(audio)/48000,old+1e-8)
                if kind=='example':self.assertLess(len(audio)/48000,old*.65)
                report=backend.decode(audio,48000)
                self.assertTrue(report['card']['exact'])
                self.assertEqual(report['pixels'],pixel.unpack_indices(pixel.minimal_avatar_payload(card),1024))
                self.assertEqual(report['copy_header']['copy_header_version'],3)
                self.assertEqual(report['receive_profile'],key)
                self.assertEqual(report['card']['avatar_mode'],key)
                self.assertNotIn(key,dict(available_modes()))
                self.assertIn(key,dict(available_modes(include_experimental=True)))

    def test_every_profile_over_actual_audio(self):
        rng=np.random.default_rng(56);random=rng.integers(0,8,1024).tolist();cards={}
        for count in range(0,1025,16):
            card=pixel.example_card();card.palette=list(pixel.MINIMAL_AVATAR_PALETTE)
            card.pixels=random[:count]+[0]*(1024-count)
            _,capacity,_=compressed.pack(card)
            cards.setdefault(capacity,card)
        self.assertEqual(set(cards),set(compressed.CAPACITIES))
        for key in compressed.MODES:
            for capacity,card in cards.items():
                with self.subTest(key=key,capacity=capacity):
                    backend=get_backend(key);audio=backend.encode(card,1,'card',None)
                    self.assertEqual(len(audio),76800+compressed.payload_samples(key,capacity))
                    report=backend.decode(audio,48000)
                    self.assertTrue(report['card']['exact'])
                    self.assertEqual(report['pixels'],card.pixels)
                    self.assertEqual(report['card']['diagnostics']['coding_capacity_bytes'],capacity)

    def test_saved_wav_selected_and_receive_all(self):
        from app import DecodeWorker
        from PySide6.QtWidgets import QApplication
        from card_transfer import CardAssemblyService
        qt=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            for key in compressed.MODES:
                card=pixel.example_card();audio=get_backend(key).encode(card,1,'cq',None)
                path=Path(folder)/(key+'.wav');pixel.write_wav(path,audio)
                for mode in (key,'auto'):
                    done=[];worker=DecodeWorker(path,Path(folder)/'state',mode=mode,include_experimental=True)
                    worker.signals.finished.connect(done.append);worker.run()
                    self.assertIsNone(done[0][2]);report,_=CardAssemblyService().merge_preview(done[0][1])
                    self.assertTrue(report['assembly_event']['verified_complete'])
        qt.processEvents()

    def test_growing_cache_and_identity_mismatch(self):
        key='experimental_8fsk_compressed';backend=get_backend(key);card=pixel.example_card()
        audio=backend.encode(card,3,'cq',None);cache=envelope.CopyCaptureCache()
        for stop in (round(2*48000),round(5*48000),len(audio)):
            part=audio[:stop];copies=envelope.find_copies(part,48000);cache.update(part,48000,copies)
        data,copies=cache.materialize();self.assertEqual(len(copies),3)
        report=envelope.decode_capture(key,data,8000,backend.decode_payload,copies=copies,
                                       announced_decoder=backend.decode_announced_copies)
        self.assertTrue(report['card']['exact'])
        for copy in copies:copy.identity='00'*16
        with self.assertRaisesRegex(ValueError,'protected copy identity'):
            envelope.decode_capture(key,data,8000,backend.decode_payload,copies=copies,
                                    announced_decoder=backend.decode_announced_copies)

    def test_noise_missing_header_and_bad_repeat_counts(self):
        card=pixel.example_card();backend=get_backend('experimental_8fsk_compressed')
        audio=backend.encode(card,1,'cq',None)
        with self.assertRaises(ValueError):backend.decode(audio[76800:],48000)
        with self.assertRaises(ValueError):backend.decode(np.random.default_rng(17).normal(0,.1,len(audio)),48000)
        for count in (0,21,1.5):
            with self.assertRaises(ValueError):backend.encode(card,count,'cq',None)
            with self.assertRaises(ValueError):backend.estimate_seconds(card,count)

if __name__=='__main__':unittest.main()
