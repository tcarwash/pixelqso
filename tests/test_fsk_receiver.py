import unittest
import numpy as np
import cardmodem as pixel
import modem_envelope as envelope
import experimental_burst_modem as burst
import compressed_fsk_modem as compressed
from copy_combining import SoftCopy,hypothesis_attempts
from tools.hf_channel import simulate


class FSKReceiverTests(unittest.TestCase):
    def test_header_acquires_when_preamble_is_completely_faded(self):
        key='experimental_16fsk_compressed'
        audio=envelope.header_audio(key,compressed.payload_samples(key,192),b'z'*16,copies=2)
        first=round(envelope.GUARD*48000)
        last=round((envelope.GUARD+envelope.TRAINING/envelope.BAUD)*48000)
        audio[first:last]=0
        capture,_=simulate(audio,48000,profile='awgn',snr_db=0,seed=31)
        copies=envelope.find_copies(capture,48000)
        self.assertEqual(len(copies),1)  # available prefix of the first payload
        self.assertEqual(copies[0].mode,key)
        self.assertEqual(copies[0].identity,(b'z'*16).hex())
        self.assertEqual(copies[0].diagnostics['group_copies'],2)

    def test_compressed_prefix_recovery_beyond_hard_rs_capacity(self):
        for key in compressed.MODES:
            symbols = envelope.header_symbols(key,compressed.payload_samples(key,192),b'z'*16,copies=6)
            _,permutation = envelope.constants()
            tones = np.empty_like(symbols[envelope.TRAINING:])
            tones[permutation] = symbols[envelope.TRAINING:]
            metrics = np.full((len(tones),8),.001)
            metrics[np.arange(len(tones)),tones] = 1
            # Eight errors in the known magic/version/mode, five in the
            # unknown identity. The prefix aid must not guess identity bytes.
            for at in list(range(8))+list(range(17,22)):
                actual = tones[2*at]
                metrics[2*at,actual] = .01
                metrics[2*at,actual^1] = 2
            received = metrics[permutation]
            with self.assertRaises(ValueError):
                envelope.decode_header(received)
            result = envelope.decode_header(received,soft=True)
            self.assertEqual(result['mode'],key)
            self.assertEqual(result['identity'],(b'z'*16).hex())
            self.assertEqual(result['group_copies'],6)

    def test_alternates_never_double_count_a_physical_copy(self):
        candidates = [[SoftCopy(np.full(16,i+rank+1.),{'physical_copy':i},('card',))
                       for rank in range(3)] for i in range(3)]
        attempts = list(hypothesis_attempts(candidates,max_extra=4))
        self.assertLessEqual(len(attempts),3+2+4)
        self.assertTrue(any(any(c.diagnostics['physical_copy']==0 and c is candidates[0][1]
                                for c in used) for _,used in attempts))
        for evidence,used in attempts:
            physical = [c.diagnostics['physical_copy'] for c in used]
            self.assertEqual(len(physical),len(set(physical)))
            np.testing.assert_array_equal(evidence,np.sum([c.evidence for c in used],axis=0))

    def test_alternate_receiver_lanes_preserve_identity_isolation(self):
        candidates = [[SoftCopy(np.ones(16),{},(identity,)) for _ in range(3)]
                      for identity in ('a','b')]
        for _,used in hypothesis_attempts(candidates):
            self.assertEqual(len({c.identity for c in used}),1)

    def test_native_16fsk_tracks_both_drift_directions_with_exact_pixels(self):
        for slope in (-40.,40.):
            card = pixel.example_card();card.card_id = 0x5700
            card.pixels = np.random.default_rng(1000).integers(0,8,1024).tolist()
            card.palette = list(pixel.MINIMAL_AVATAR_PALETTE)
            audio = burst.encode('experimental_16fsk_ldpc',card,'cq',-9)
            capture,_ = simulate(audio,48000,snr_db=6,seed=1000,profile='awgn',
                                 drift_hz_per_second=slope)
            report = burst.decode('experimental_16fsk_ldpc',capture,48000)
            self.assertTrue(report['card']['exact'])
            self.assertEqual(report['pixels'],card.pixels)
            diagnostics = report['card']['diagnostics']
            self.assertEqual(diagnostics['frequency_tracking'],'training + pilot linear drift')
            self.assertLessEqual(abs(diagnostics['frequency_drift_hz_per_second']-slope),10)
            self.assertEqual(diagnostics['combined_copies'],1)


if __name__ == '__main__':
    unittest.main()
