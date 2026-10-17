"""Prueba de punta a punta con cámara y micrófono SINTÉTICOS (ffmpeg lavfi).

No toca dispositivos reales. Tarda ~45 s; se activa con VIGD_E2E=1."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from vigd import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO = ("aevalsrc=0.003*(random(0)*2-1)+if(between(mod(t\\,20)\\,12\\,14)\\,"
         "0.3*(random(1)*2-1)\\,0):s=16000")
VIDEO = ("color=c=gray:s=640x480:r=30[bg];color=c=white:s=80x80:r=30[box];"
         "[bg][box]overlay=x='if(lt(mod(t\\,20)\\,4)\\,100+mod(t\\,20)*60\\,-200)':y=200")


@unittest.skipUnless(os.environ.get("VIGD_E2E") == "1" and shutil.which("ffmpeg"), "VIGD_E2E=1 y ffmpeg requeridos")
class EndToEndTest(unittest.TestCase):
    def test_capture_produces_playable_events(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "c.toml")
            with open(cfg, "w") as f:
                f.write(f'[general]\ndata_dir = {json.dumps(os.path.join(d, "data"))}\nmin_free_gb = 0\n'
                        f'[audio]\ncommand = {json.dumps(["ffmpeg", "-loglevel", "error", "-re", "-f", "lavfi", "-i", AUDIO, "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1"])}\n'
                        f'[video]\ninput_args = {json.dumps(["-re", "-f", "lavfi", "-i", VIDEO])}\n'
                        '[events]\npre_roll_s = 3\npost_roll_s = 3\nmerge_gap_s = 4\n'
                        '[transcription]\nenabled = false\n')
            p = subprocess.Popen([sys.executable, "-m", "vigd", "-c", cfg, "capture"], cwd=ROOT,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(40)
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(90), 0)
            conn = db.connect(os.path.join(d, "data", "db", "vigilancia.db"))
            evs = db.list_events(conn, limit=50)
            ready = [e for e in evs if e["status"] == "ready"]
            self.assertGreaterEqual(len(ready), 2)
            self.assertTrue(any(e["has_motion"] for e in ready))
            self.assertTrue(any(e["has_sound"] for e in ready))
            for e in ready:
                self.assertIn("audio", e["media"])
                for m in e["media"].values():
                    rel = conn.execute("SELECT relpath FROM media WHERE id=?", (m["id"],)).fetchone()[0]
                    path = os.path.join(d, "data", rel)
                    self.assertTrue(os.path.getsize(path) > 0)
            video = next(e for e in ready if e["has_motion"])["media"]["video"]
            rel = conn.execute("SELECT relpath FROM media WHERE id=?", (video["id"],)).fetchone()[0]
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                                    "-of", "csv=p=0", os.path.join(d, "data", rel)], capture_output=True, text=True)
            self.assertEqual(sorted(probe.stdout.split()), ["audio", "video"])
            conn.close()


if __name__ == "__main__":
    unittest.main()
