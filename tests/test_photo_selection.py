"""Regressionstests fuer PhotoSelection.

    python -m pytest tests          (oder: python -m unittest discover tests)

Die Oberflaechen-Tests starten das echte Fenster und werden uebersprungen,
wenn kein Bildschirm verfuegbar ist.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import photo_selection as ps  # noqa: E402


def noise_jpeg(path: Path, size=(64, 48), seed=0) -> Path:
    rng = np.random.default_rng(seed)
    Image.fromarray(rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)).save(path)
    return path


class TempDir(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="photosel_test_"))
        # addCleanup statt tearDown: laeuft auch, wenn setUp mit Skip endet
        self.addCleanup(self._remove_tmp)

    def _remove_tmp(self) -> None:
        # Unter Windows bleibt ein Ordner stehen, solange ein Vorlade-Thread
        # darin noch eine Datei offen hat - kurz nachfassen
        for _ in range(30):
            shutil.rmtree(self.tmp, ignore_errors=True)
            if not self.tmp.exists():
                return
            time.sleep(0.1)


# --------------------------------------------------------------------------
# Reine Funktionen
# --------------------------------------------------------------------------

class TestMarksValidation(unittest.TestCase):
    def test_slot_infinity_is_ignored(self):
        self.assertEqual(ps._clean_slot(float("inf")), 0)
        self.assertEqual(ps._clean_slot(json.loads("Infinity")), 0)

    def test_develop_infinity_is_ignored(self):
        dev = ps.Develop.from_dict({"orient": float("inf"), "exposure": float("nan"),
                                    "kelvin": 1e300})
        self.assertEqual(dev.orient, 0)
        self.assertEqual(dev.exposure, 0.0)
        self.assertEqual(dev.kelvin, ps.DEV_LIMITS["kelvin"][1])


class TestDevelop(unittest.TestCase):
    def test_jpg_exposure_applied_after_base(self):
        # Sonst bleibt der Regler im Bearbeitungsmodus bei JPGs wirkungslos
        dev = ps.Develop(exposure=1.0)
        self.assertEqual(ps.pending_exposure(Path("a.jpg"), dev), 1.0)
        self.assertEqual(dev.base_key(raw=False), ps.Develop().base_key(raw=False))

    def test_transfer_keeps_geometry_of_target(self):
        source = ps.Develop(exposure=1.0, contrast=20, orient=90, angle=3.0,
                            ratio="1:1", crop=(0.1, 0.1, 0.5, 0.5))
        target = ps.Develop(orient=180, crop=(0.2, 0.2, 0.9, 0.9))
        new = source.transferred_to(target)
        self.assertEqual((new.exposure, new.contrast), (1.0, 20))
        self.assertEqual((new.orient, new.angle, new.ratio, new.crop),
                         (180, 0.0, "frei", (0.2, 0.2, 0.9, 0.9)))

    def test_gain_table_matches_direct_formula(self):
        rng = np.random.default_rng(1)
        img = Image.fromarray(rng.integers(0, 255, (40, 60, 3), dtype=np.uint8))
        gains, ev = ps._kelvin_gains(3500, 20), 0.7
        arr = np.asarray(img, np.float32) / 255.0
        lin = np.power(arr, 2.2) * 2.0 ** ev * gains
        expected = np.power(np.clip(lin, 0, 1), 1 / 2.2) * 255 + 0.5
        got = np.asarray(ps._apply_gains(img, gains, ev), np.int16)
        self.assertLessEqual(np.abs(got - expected.astype(np.int16)).max(), 1)

    def test_highlights_shadows_match_direct_formula(self):
        rng = np.random.default_rng(3)
        img = Image.fromarray(rng.integers(0, 255, (40, 60, 3), dtype=np.uint8))
        for shadows, highlights in ((60, 0), (0, -80), (100, 100), (-100, 40)):
            dev = ps.Develop(shadows=shadows, highlights=highlights)
            arr = np.asarray(img, np.float64)
            lum = np.asarray(img.convert("L"), np.float64)[..., None] / 255.0
            gain = 1 + shadows / 100 * 0.9 * (1 - lum) ** 2 + highlights / 100 * 0.9 * lum ** 2
            expected = np.clip(arr * gain + 0.5, 0, 255).astype(np.int16)
            got = np.asarray(ps.apply_rgb(img, dev), np.int16)
            self.assertLessEqual(np.abs(got - expected).max(), 2, (shadows, highlights))

    def test_shutter_format(self):
        self.assertEqual(ps._fmt_shutter(1 / 250), "1/250 s")
        self.assertEqual(ps._fmt_shutter(1 / 3), "1/3 s")
        self.assertEqual(ps._fmt_shutter(0.8), "0.8 s")
        self.assertEqual(ps._fmt_shutter(0.4), "0.4 s")
        self.assertEqual(ps._fmt_shutter(2), "2 s")

    def test_sharpness_independent_of_preview_size(self):
        rng = np.random.default_rng(2)
        base = Image.fromarray(rng.integers(0, 255, (1707, 2560, 3), dtype=np.uint8))
        a = ps.analyse(base.resize((1799, 1199)))
        b = ps.analyse(base.resize((1800, 1200)))
        self.assertLess(abs(a - b) / max(a, b), 0.05)


class TestExif(TempDir):
    def test_private_data_removed_and_size_updated(self):
        img = Image.new("RGB", (400, 300))
        exif = img.getexif()
        exif[0x010F] = "Cam"
        sub = exif.get_ifd(0x8769)
        sub[0xA002], sub[0xA003] = 4000, 3000
        sub[0xA431] = "SERIAL123"
        exif.get_ifd(0x8825)[1] = "N"
        exif.get_ifd(0x8825)[2] = (52.0, 31.0, 0.0)
        img.save(self.tmp / "in.jpg", exif=exif)

        data = ps._exif_bytes(self.tmp / "in.jpg", (1200, 900), strip_private=True)
        Image.new("RGB", (8, 8)).save(self.tmp / "out.jpg", exif=data)
        result = Image.open(self.tmp / "out.jpg").getexif()
        self.assertEqual(dict(result.get_ifd(0x8825)), {})
        sub = result.get_ifd(0x8769)
        self.assertNotIn(0xA431, sub)
        self.assertEqual((sub.get(0xA002), sub.get(0xA003)), (1200, 900))
        self.assertEqual(result.get(0x010F), "Cam")

    def test_location_kept_when_wanted(self):
        img = Image.new("RGB", (40, 30))
        exif = img.getexif()
        exif.get_ifd(0x8825)[1] = "N"
        img.save(self.tmp / "in.jpg", exif=exif)
        data = ps._exif_bytes(self.tmp / "in.jpg", None, strip_private=False)
        Image.new("RGB", (8, 8)).save(self.tmp / "out.jpg", exif=data)
        self.assertEqual(Image.open(self.tmp / "out.jpg").getexif().get_ifd(0x8825).get(1), "N")


class TestExport(TempDir):
    def test_noisy_image_exports(self):
        # Pillows Puffer fuer optimize=True ist dafuer zu knapp - frueher
        # schlug die Entwicklung mit "broken data stream" fehl
        noise_jpeg(self.tmp / "noisy.jpg", size=(600, 400))
        ps.export_jpeg(self.tmp / "noisy.jpg", ps.Develop(contrast=10),
                       self.tmp / "out.jpg", 92, 0)
        self.assertEqual(Image.open(self.tmp / "out.jpg").size, (600, 400))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["noisy.jpg", "out.jpg"])


class TestFiles(TempDir):
    def test_write_atomic_replaces(self):
        target = self.tmp / "x.json"
        target.write_text("alt", encoding="utf-8")
        ps._write_atomic(target, "neu")
        self.assertEqual(target.read_text(encoding="utf-8"), "neu")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["x.json"])

    def test_same_size_different_content_is_not_skipped(self):
        # Unkomprimierte RAWs sind alle gleich gross - frueher wurde still uebersprungen
        src, dst = self.tmp / "src", self.tmp / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "DSC_1.NEF").write_bytes(b"A" * 1000)
        (src / "DSC_1.xmp").write_bytes(b"<x/>")
        (dst / "DSC_1.NEF").write_bytes(b"B" * 1000)
        plan = ps.App._plan_names([src / "DSC_1.NEF", src / "DSC_1.xmp"], dst, "DSC_1")
        # Die ganze Aufnahme weicht gemeinsam aus, damit RAW und XMP zusammenpassen
        self.assertEqual([(d.name, there) for _, d, there in plan],
                         [("DSC_1_1.NEF", False), ("DSC_1_1.xmp", False)])

    def test_identical_file_counts_as_done(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "a.jpg").write_bytes(b"same")
        (dst / "a.jpg").write_bytes(b"same")
        plan = ps.App._plan_names([src / "a.jpg"], dst, "a")
        self.assertEqual([(d.name, there) for _, d, there in plan], [("a.jpg", True)])

    def test_transfer_never_overwrites(self):
        (self.tmp / "a").write_bytes(b"neu")
        (self.tmp / "b").write_bytes(b"wertvoll")
        for move in (False, True):
            with self.assertRaises(FileExistsError):
                ps.App._transfer(self.tmp / "a", self.tmp / "b", move)
        self.assertEqual((self.tmp / "b").read_bytes(), b"wertvoll")

    def test_numbered_names(self):
        self.assertEqual(ps._numbered("IMG_1.CR2.xmp", "IMG_1", 2), "IMG_1_2.CR2.xmp")
        self.assertEqual(ps._numbered("img_1.jpg", "IMG_1", 1), "img_1_1.jpg")

    @unittest.skipUnless(sys.platform == "win32", "Junctions gibt es nur unter Windows")
    def test_scan_skips_junctions(self):
        outside = self.tmp / "outside"
        source = self.tmp / "source"
        outside.mkdir()
        (source / "sub").mkdir(parents=True)
        noise_jpeg(outside / "fremd.jpg")
        noise_jpeg(source / "sub" / "eigen.jpg")
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(source / "link"), str(outside)],
                              capture_output=True)
        if made.returncode:
            self.skipTest("Junction konnte nicht angelegt werden")
        fake = types.SimpleNamespace(recursive=types.SimpleNamespace(get=lambda: True))
        found = ps.App._scan_files(fake, source)
        self.assertEqual([p.name for p in found], ["eigen.jpg"])


class TestWorker(TempDir):
    """Der Arbeits-Thread, ohne Oberflaeche."""

    def run_plan(self, plan, move, cfg):
        fake = types.SimpleNamespace(worker_q=queue.Queue(), _cancel=threading.Event(),
                                     _ensure_dir=ps.App._ensure_dir,
                                     _plan_names=ps.App._plan_names,
                                     _transfer=ps.App._transfer)
        ps.App._run_worker(fake, plan, move, False, cfg)
        while True:
            kind, payload = fake.worker_q.get_nowait()
            if kind == "done":
                return payload

    def job(self, files, target, develop):
        return ps.Job(key=str(files[0]), slot=1, stem=files[0].stem, reldir=".",
                      target=target, master=files[0], files=list(files),
                      dev=ps.Develop(exposure=0.5), develop=develop)

    def test_move_and_develop_alongside(self):
        # Frueher wurde nach dem Verschieben aus dem alten Pfad entwickelt
        src, dst = self.tmp / "src", self.tmp / "dst"
        src.mkdir()
        dst.mkdir()
        noise_jpeg(src / "IMG_1.jpg")
        cfg = {"develop": True, "mode": "zusaetzlich", "quality": 90,
               "long_edge": 0, "strip_gps": True}
        ops, _, done_keys, _, errors, _, cancelled = self.run_plan(
            [self.job([src / "IMG_1.jpg"], dst, True)], True, cfg)
        self.assertEqual(errors, [])
        self.assertFalse(cancelled)
        self.assertEqual(sorted(k for _, _, k, _ in ops), ["develop", "move"])
        self.assertTrue((dst / "IMG_1.jpg").exists())
        self.assertTrue((dst / ps.DEV_SUBDIR / "IMG_1.jpg").exists())
        self.assertFalse((src / "IMG_1.jpg").exists())
        self.assertEqual(len(done_keys), 1)
        self.assertEqual([p.name for p in (dst / ps.DEV_SUBDIR).iterdir()], ["IMG_1.jpg"])

    def test_cancel_stops_before_next_job(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        src.mkdir()
        dst.mkdir()
        noise_jpeg(src / "a.jpg")
        fake_cancel = threading.Event()
        fake_cancel.set()
        fake = types.SimpleNamespace(worker_q=queue.Queue(), _cancel=fake_cancel,
                                     _ensure_dir=ps.App._ensure_dir,
                                     _plan_names=ps.App._plan_names,
                                     _transfer=ps.App._transfer)
        ps.App._run_worker(fake, [self.job([src / "a.jpg"], dst, False)], False, False,
                           {"develop": False, "mode": "zusaetzlich", "quality": 90,
                            "long_edge": 0, "strip_gps": True})
        kind, payload = fake.worker_q.get_nowait()
        self.assertEqual(kind, "done")
        self.assertTrue(payload[-1])            # abgebrochen
        self.assertEqual(payload[0], [])        # nichts angefasst


# --------------------------------------------------------------------------
# Oberflaeche
# --------------------------------------------------------------------------

class GuiTest(TempDir):
    def setUp(self) -> None:
        super().setUp()
        self.patches = [
            mock.patch.object(ps, "SLOTS_FILE", self.tmp / "slots.json"),
            mock.patch.object(ps, "LOG_FILE", self.tmp / "log.txt"),
            mock.patch.object(ps.messagebox, "askokcancel", return_value=True),
            mock.patch.object(ps.messagebox, "showinfo"),
            mock.patch.object(ps.messagebox, "showwarning"),
            mock.patch.object(ps.messagebox, "showerror"),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        # Tk scheitert unter Windows gelegentlich beim wiederholten Erzeugen in
        # einem Prozess ("tcl_findLibrary") - dann nochmal
        for attempt in range(3):
            try:
                self.app = ps.App()
                break
            except ps.tk.TclError as exc:
                if attempt == 2:
                    self.skipTest(f"kein Bildschirm: {exc}")
                time.sleep(0.2)
        self.app.withdraw()
        self.source = self.tmp / "src"
        self.source.mkdir()

    def tearDown(self) -> None:
        try:
            self.app._on_close()
        except Exception:
            pass

    def pump(self, seconds=0.2, until=None):
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            if until is not None and until():
                return
            time.sleep(0.01)

    def open_source(self):
        self.app.source = self.source
        self.app.rescan()
        self.pump()


class TestGui(GuiTest):
    def test_filter_open_does_not_skip(self):
        for i in range(6):
            noise_jpeg(self.source / f"IMG_{i}.jpg", seed=i)
        self.open_source()
        self.app.filter_mode.set("Nur offene")
        self.app.refresh_view()
        for _ in range(3):
            self.app.assign(1, toggle=True, advance=True)
        self.assertEqual([e.stem for e in self.app.entries if e.slot],
                         ["IMG_0", "IMG_1", "IMG_2"])

    def test_subfolder_marks_survive_non_recursive_save(self):
        noise_jpeg(self.source / "top.jpg")
        (self.source / "sub").mkdir()
        noise_jpeg(self.source / "sub" / "deep.jpg")
        self.app.recursive.set(True)
        self.open_source()
        next(e for e in self.app.entries if e.stem == "deep").slot = 2
        self.app._save_marks()
        self.app.recursive.set(False)
        self.app.rescan()
        self.app.goto(0)
        self.app.assign(3)
        self.app._save_marks()
        data = json.loads((self.source / ps.MARKS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(data["assignments"], {"top.jpg": 3, "sub/deep.jpg": 2})

    def test_hostile_marks_file_does_not_crash(self):
        noise_jpeg(self.source / "a.jpg")
        for content in ("[]", '{"assignments": {"a.jpg": Infinity}}',
                        '{"develop": {"a.jpg": {"orient": Infinity}}}'):
            (self.source / ps.MARKS_FILE).write_text(content, encoding="utf-8")
            self.open_source()
            self.assertEqual(len(self.app.entries), 1)

    def test_broken_raw_is_not_developed_endlessly(self):
        if ps.rawpy is None:
            self.skipTest("rawpy fehlt")
        (self.source / "BROKEN.dng").write_bytes(b"kein raw" * 100)
        self.open_source()
        calls = []
        real = self.app.developer.func
        self.app.developer.func = lambda *a: (calls.append(1), real(*a))[1]
        self.app.toggle_develop()
        self.pump(1.5)
        self.assertLessEqual(len(calls), 1)

    def test_undo_move_keeps_new_file_at_origin(self):
        noise_jpeg(self.source / "IMG_1.jpg", seed=1)
        dest = self.tmp / "dst"
        dest.mkdir()
        self.app.slots[1] = ps.Slot("Auswahl", dest)
        self.open_source()
        self.app.assign(1)
        self.app.move_files.set(True)
        self.app.run_assignments()
        self.pump(5, until=lambda: not self.app._busy)
        self.assertTrue((dest / "IMG_1.jpg").exists())

        noise_jpeg(self.source / "IMG_1.jpg", seed=99)   # neue Datei, gleicher Name
        newcomer = (self.source / "IMG_1.jpg").read_bytes()
        self.app.undo()
        self.assertEqual((self.source / "IMG_1.jpg").read_bytes(), newcomer)
        self.assertTrue((dest / "IMG_1.jpg").exists())
        self.assertIn("im Ziel gelassen", self.app.status.cget("text"))

    def test_sidecar_travels_with_image(self):
        noise_jpeg(self.source / "IMG_1.jpg")
        (self.source / "IMG_1.xmp").write_text("<x/>", encoding="utf-8")
        dest = self.tmp / "dst"
        dest.mkdir()
        self.app.slots[1] = ps.Slot("Auswahl", dest)
        self.open_source()
        self.assertEqual(len(self.app.entries), 1)
        self.app.assign(1)
        self.app.run_assignments()
        self.pump(5, until=lambda: not self.app._busy)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["IMG_1.jpg", "IMG_1.xmp"])


if __name__ == "__main__":
    unittest.main()
