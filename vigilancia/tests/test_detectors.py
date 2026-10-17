import unittest

import numpy as np

from vigd.audio import FRAME_BYTES, AudioDetector, pcm_stats, rms_db
from vigd.config import DEFAULTS
from vigd.video import AH, AW, MotionDetector

RNG = np.random.default_rng(0)


def noise_frame(amp):
    return (RNG.normal(0, amp, FRAME_BYTES // 2)).clip(-32767, 32767).astype("<i2").tobytes()


def gray(level, box=None):
    f = np.full((AH, AW), level, np.float32) + RNG.normal(0, 2, (AH, AW))
    if box:
        x, y = box
        f[y:y + 20, x:x + 20] = 250
    return f.clip(0, 255).astype(np.uint8).tobytes()


class AudioDetectorTest(unittest.TestCase):
    def setUp(self):
        self.det = AudioDetector(dict(DEFAULTS["audio"]), vad=None)

    def feed(self, amp, n):
        return [self.det.process(noise_frame(amp)) for _ in range(n)]

    def test_rms(self):
        self.assertLess(rms_db(b"\x00\x00" * 480), -100)
        self.assertAlmostEqual(rms_db(np.full(480, 32767, "<i2").tobytes()), 0, delta=0.1)

    def test_silence_does_not_trigger(self):
        out = self.feed(30, 400)
        self.assertFalse(any(out))
        self.assertIsNotNone(self.det.floor)

    def test_loud_sound_triggers_after_floor(self):
        self.feed(30, 400)
        out = self.feed(3000, 40)
        trig = [t for t in out if t]
        self.assertTrue(trig)
        self.assertEqual(trig[-1]["kind"], "sonido")
        self.assertTrue(any(t["impulsive"] for t in trig))

    def test_floor_adapts_to_constant_noise(self):
        self.feed(30, 400)
        self.feed(3000, 600)  # ruido fuerte constante durante 18 s
        out = self.feed(3000, 50)
        self.assertFalse(any(out), "un ruido constante debe volverse ambiente")

    def test_pcm_stats(self):
        peak, mean = pcm_stats(noise_frame(30) * 10 + noise_frame(3000))
        self.assertGreater(peak, mean)


class MotionDetectorTest(unittest.TestCase):
    def setUp(self):
        self.det = MotionDetector(dict(DEFAULTS["video"]))
        for _ in range(15):
            self.det.process(gray(100))

    def test_static_scene(self):
        self.assertFalse(any(self.det.process(gray(100)) for _ in range(20)))

    def test_moving_object(self):
        res = [self.det.process(gray(100, (10 + 5 * i, 50))) for i in range(10)]
        self.assertIn("motion", res)

    def test_light_change_is_not_motion(self):
        res = [self.det.process(gray(170)) for _ in range(20)]
        self.assertIn("light", res)
        self.assertNotIn("motion", res)

    def test_ignore_zone(self):
        cfg = dict(DEFAULTS["video"], ignore_zones=[[0.0, 0.0, 0.5, 1.0]])
        det = MotionDetector(cfg)
        for _ in range(15):
            det.process(gray(100))
        res = [det.process(gray(100, (10 + (i % 3), 50))) for i in range(10)]
        self.assertNotIn("motion", res)


if __name__ == "__main__":
    unittest.main()
