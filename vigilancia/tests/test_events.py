import os
import tempfile
import unittest

from vigd import db
from vigd.config import DEFAULTS, Paths
from vigd.events import EventManager, OpenEvent, classify


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class FakeAudio:
    def start_recording(self, start):
        return object()

    def stop_recording(self, rec, end):
        return None, b""


class FakeFinalizer:
    def __init__(self):
        self.jobs = []

    def submit(self, job):
        self.jobs.append(job)


class EventManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = Paths(self.tmp.name)
        self.paths.ensure()
        self.conn = db.init(self.paths.db)
        self.clock = Clock()
        self.fin = FakeFinalizer()
        cfg = {"events": dict(DEFAULTS["events"], pre_roll_s=5, post_roll_s=6, merge_gap_s=8, max_event_s=60)}
        self.em = EventManager(cfg, self.conn, FakeAudio(), None, self.fin, clock=self.clock)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def advance(self, until, step=0.5):
        while self.clock.t < until:
            self.clock.t += step
            self.em.tick()

    def test_close_triggers_merge(self):
        self.em.on_audio(1000, {"kind": "sonido"})
        self.advance(1005)
        self.em.on_motion(1005, 0.05)
        self.advance(1030)
        self.assertEqual(len(self.fin.jobs), 1)
        job = self.fin.jobs[0]
        self.assertAlmostEqual(job.ev.start, 995)
        self.assertAlmostEqual(job.end, 1011)
        self.assertEqual(job.ev.motion_hits, 1)

    def test_separate_events_do_not_overlap(self):
        self.em.on_audio(1000, {"kind": "sonido"})
        self.advance(1009)
        self.em.on_audio(1009, {"kind": "sonido"})
        self.advance(1040)
        self.assertEqual(len(self.fin.jobs), 2)
        a, b = self.fin.jobs
        self.assertGreaterEqual(b.ev.start, a.end)
        self.assertIsNone(b.ev.continuation_of)

    def test_long_event_is_split_with_continuation(self):
        t = 1000.0
        while t < 1130:
            self.em.on_audio(t, {"kind": "voz"})
            t += 1
            self.advance(t)
        self.advance(1160)
        self.assertGreaterEqual(len(self.fin.jobs), 3)
        self.assertIsNone(self.fin.jobs[0].ev.continuation_of)
        self.assertEqual(self.fin.jobs[1].ev.continuation_of, self.fin.jobs[0].ev.id)
        rows = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(rows, len(self.fin.jobs))

    def test_classify(self):
        ev = OpenEvent(id=1, uid="x", start=0, first_trigger=0, last_trigger=0,
                       audio_frames=100, voice_frames=60, sound_frames=40, motion_hits=3, motion_max=0.1)
        f = classify(ev, DEFAULTS["events"], 5, 10)
        self.assertEqual(f["category"], "voz")
        self.assertEqual(f["has_motion"], 1)
        ev2 = OpenEvent(id=2, uid="y", start=0, first_trigger=0, last_trigger=0, motion_hits=4, motion_max=0.02)
        self.assertEqual(classify(ev2, DEFAULTS["events"], 5, 10)["category"], "movimiento")


class DbTest(unittest.TestCase):
    def test_list_filters_and_delete(self):
        with tempfile.TemporaryDirectory() as d:
            paths = Paths(d)
            paths.ensure()
            conn = db.init(paths.db)
            for i in range(5):
                eid = db.insert_event(conn, f"u{i}", 1000 + i)
                db.update_event(conn, eid, status="ready", has_motion=i % 2, transcript=f"hola {i}%")
            rel = "audio/2020-01-01/u0.m4a"
            os.makedirs(os.path.dirname(paths.abs(rel)))
            with open(paths.abs(rel), "wb") as f:
                f.write(b"x")
            db.add_media(conn, 1, "audio", rel, "audio/mp4", 1)
            self.assertEqual([e["id"] for e in db.list_events(conn, limit=2)], [5, 4])
            self.assertEqual([e["id"] for e in db.list_events(conn, before_id=4, limit=2)], [3, 2])
            self.assertEqual([e["id"] for e in db.list_events(conn, etype="movimiento")], [4, 2])
            self.assertEqual([e["id"] for e in db.list_events(conn, q="3%")], [4])
            self.assertEqual([e["id"] for e in db.list_events(conn, q="%")], [5, 4, 3, 2, 1])
            with self.assertRaises(ValueError):
                db.list_events(conn, etype="x; DROP TABLE events")
            db.delete_event(conn, paths, 1)
            self.assertFalse(os.path.exists(paths.abs(rel)))
            self.assertIsNone(db.get_event(conn, 1))
            conn.close()

    def test_ai_toggle(self):
        with tempfile.TemporaryDirectory() as d:
            paths = Paths(d)
            paths.ensure()
            conn = db.init(paths.db)
            self.assertTrue(db.ai_enabled(conn))
            db.set_ai_enabled(conn, False)
            self.assertFalse(db.ai_enabled(db.connect(paths.db)))  # visible desde otro proceso
            db.set_ai_enabled(conn, True)
            self.assertTrue(db.ai_enabled(conn))
            conn.close()

    def test_path_validation(self):
        with tempfile.TemporaryDirectory() as d:
            paths = Paths(d)
            for bad in ("../x", "/etc/passwd", "audio/../../x"):
                with self.assertRaises(ValueError):
                    paths.abs(bad)


if __name__ == "__main__":
    unittest.main()
