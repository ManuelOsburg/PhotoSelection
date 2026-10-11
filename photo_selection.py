#!/usr/bin/env python3
"""PhotoSelection - schlanke Foto-Voransicht fuer JPG und RAW.

Bilder durchblaettern, per Taste 1-9 einem von neun Zielordnern zuordnen und die
Zuordnungen am Ende in einem Rutsch ausfuehren (kopieren oder verschieben).
Beim Sichten werden RAW-Dateien ueber das eingebettete Vorschau-JPEG angezeigt -
das ist um ein Vielfaches schneller als eine vollstaendige Entwicklung.

Beurteilungs-Hilfen: EXIF-Einblendung, Histogramm, Clipping-Warnung und ein
Schaerfe-Score (Laplace-Varianz der Bildmitte).

Bearbeitung (Taste E): Belichtung, Weissabgleich, Kontrast, Saettigung, Lichter,
Schatten, Schaerfen, Rauschminderung sowie Zuschnitt und Geraderichten. Die
Einstellungen sind nicht-destruktiv, gelten pro Aufnahme und werden beim
Ausfuehren der Zuordnungen als JPG entwickelt.

Alles laesst sich mit Strg+Z rueckgaengig machen - auch abgeschlossene Kopier-,
Verschiebe- und Entwicklungs-Vorgaenge.
"""

from __future__ import annotations

import functools
import io
import json
import math
import os
import queue
import re
import shutil
import stat
import sys
import threading
import time
import tkinter as tk
import traceback
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, ttk


def _missing_dependency(exc: ImportError) -> None:
    """Fehlendes Pflichtpaket melden - unter pythonw gaebe es sonst keinerlei Rueckmeldung."""
    text = (f"Es fehlt ein benötigtes Paket: {exc.name or exc}\n\n"
            "Installieren mit:\n    pip install -r requirements.txt")
    print(text, file=sys.stderr)
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("PhotoSelection", text)
        root.destroy()
    except tk.TclError:
        pass
    raise SystemExit(1)


try:
    import numpy as np
    from PIL import (Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps,
                     ImageStat, ImageTk)
except ImportError as _exc:
    _missing_dependency(_exc)

try:
    from PIL import ImageCms
except ImportError:  # Pillow ohne LittleCMS - Farbprofile werden dann ignoriert
    ImageCms = None

try:
    import rawpy
except ImportError:  # RAW-Unterstuetzung ist optional
    rawpy = None

try:
    import exifread
except ImportError:  # EXIF-Einblendung faellt dann auf das Vorschaubild zurueck
    exifread = None

try:
    from pillow_heif import register_heif_opener
except ImportError:  # HEIF/HIF ist optional
    register_heif_opener = None

JPG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}
if register_heif_opener is not None:
    register_heif_opener()
    JPG_EXT |= {".heic", ".heif", ".hif"}
RAW_EXT = {
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf",
    ".orf", ".rw2", ".pef", ".dng", ".raw", ".rwl", ".srw", ".3fr", ".iiq",
    ".mos", ".mrw", ".x3f",
}
ALL_EXT = JPG_EXT | RAW_EXT
# Begleitdateien anderer Programme - wandern mit der Aufnahme mit, damit z. B.
# Lightroom (IMG.xmp), darktable (IMG.CR2.xmp), DxO (.dop) oder RawTherapee
# (.pp3) ihre Bearbeitung wiederfinden
SIDECAR_EXT = {".xmp", ".thm", ".dop", ".pp3"}

MARKS_FILE = ".photoselection.json"                        # pro Quellordner
SLOTS_FILE = Path.home() / ".photoselection_slots.json"    # global
LOG_FILE = Path.home() / ".photoselection.log"
DEV_SUBDIR = "entwickelt"      # Unterordner fuer "zusaetzlich zur RAW-Datei"

CACHE_SIZE = 8                 # schnelle Vorschauen im Speicher
DEV_CACHE_SIZE = 3             # entwickelte Bilder im Speicher (gross!)
FULL_CACHE_SIZE = 2            # volle Aufloesung fuer die 100-%-Ansicht
MAX_DECODE_PX = 3000    # harte Obergrenze der gecachten Vorschau
DRAFT_PX = 2560         # was von libjpeg angefordert wird, siehe _draft_size()
ANALYSIS_PX = 1200      # feste Kantenlaenge fuer den Schaerfe-Score
UNDO_LIMIT = 200
SORT_MODES = ("Name", "Dateidatum")

SLOT_COLORS = {
    1: "#3ddc84", 2: "#4ea1ff", 3: "#ffb454", 4: "#ff6b8b", 5: "#b48ead",
    6: "#f2e14c", 7: "#4dd0e1", 8: "#e06c75", 9: "#9aa0a6",
}
# Fuer Text auf der weissen Liste abgedunkelt - Gelb und Grau waeren sonst kaum lesbar
LIST_COLORS = {
    i: "#%02x%02x%02x" % tuple(int(int(c[k:k + 2], 16) * 0.6) for k in (1, 3, 5))
    for i, c in SLOT_COLORS.items()
}
HI_CLIP, LO_CLIP = 250, 6

RATIOS = {"frei": None, "3:2": 3 / 2, "2:3": 2 / 3, "4:3": 4 / 3, "3:4": 3 / 4,
          "1:1": 1.0, "16:9": 16 / 9}
DENOISE = {"aus": 0, "leicht": 1, "stark": 2}   # rawpy.FBDDNoiseReductionMode
LONG_EDGES = OrderedDict([("Original", 0), ("4096 px", 4096), ("3000 px", 3000),
                          ("2048 px", 2048), ("1600 px", 1600), ("1200 px", 1200)])

# Was beim Export mit "Standort und Seriennummern entfernen" wegfaellt: das
# GPS-IFD sowie Besitzer, Seriennummern und die MakerNote (in der Hersteller
# oft weitere Seriennummern ablegen) aus dem Exif-IFD
PRIVATE_EXIF_TAGS = (0xA430, 0xA431, 0xA435, 0x927C)
_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


# --------------------------------------------------------------------------
# Dateien
# --------------------------------------------------------------------------

def _write_atomic(path: Path, text: str) -> None:
    """Erst vollstaendig daneben schreiben, dann austauschen.

    Ein Abbruch mittendrin (Absturz, Karte gezogen, Netzwerk weg) hinterlaesst
    so immer die alte oder die neue Fassung - nie eine halbe, die beim
    naechsten Laden verworfen wuerde.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _publish(part: Path, dest: Path) -> None:
    """Fertig geschriebene Datei an ihren Platz bringen - ohne zu ueberschreiben."""
    try:
        if dest.exists():   # unter Windows verweigert rename das ohnehin
            raise FileExistsError(f"existiert bereits: {dest}")
        os.rename(part, dest)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def _signature(path: Path) -> tuple[int, int, int]:
    """Woran sich eine Datei wiedererkennen laesst - die Groesse allein reicht nicht."""
    st = path.stat()
    return st.st_size, st.st_mtime_ns, st.st_ino


def _same_content(a: Path, b: Path, chunk: int = 1 << 20) -> bool:
    """Gleicher Inhalt? Unkomprimierte RAWs einer Kamera sind alle gleich gross."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                block_a, block_b = fa.read(chunk), fb.read(chunk)
                if block_a != block_b:
                    return False
                if not block_a:
                    return True
    except OSError:
        return False


def _numbered(name: str, stem: str, n: int) -> str:
    """IMG_1.CR2 -> IMG_1_2.CR2, auch fuer Begleitdateien wie IMG_1.CR2.xmp."""
    if name.lower().startswith(stem.lower()):
        return f"{name[:len(stem)]}_{n}{name[len(stem):]}"
    path = Path(name)
    return f"{path.stem}_{n}{path.suffix}"


def _free_path(dest: Path) -> Path:
    """dest selbst, wenn frei - sonst dest_1, dest_2 ..."""
    n = 0
    candidate = dest
    while candidate.exists():
        n += 1
        candidate = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
    return candidate


def _is_link(entry: os.DirEntry) -> bool:
    """Symlink oder Windows-Junction - beides fuehrt womoeglich aus dem Ordner heraus."""
    if entry.is_symlink():
        return True
    try:
        return entry.stat(follow_symlinks=False).st_reparse_tag == _MOUNT_POINT
    except (AttributeError, OSError):   # st_reparse_tag gibt es nur unter Windows
        return False


def _natural_key(text: str) -> list:
    """IMG_2 vor IMG_10, unabhaengig von Gross- und Kleinschreibung."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", text)]


# --------------------------------------------------------------------------
# Einstellungen pro Aufnahme
# --------------------------------------------------------------------------

# Zulaessige Bereiche - gelten fuer die Regler und fuer alles, was aus einer
# Datei eingelesen wird
DEV_LIMITS = {
    "exposure": (-2.0, 3.0), "kelvin": (2000, 12000), "tint": (-100, 100),
    "contrast": (-100, 100), "saturation": (-100, 100), "highlights": (-100, 100),
    "shadows": (-100, 100), "sharpen": (0, 100), "angle": (-45.0, 45.0),
}


def _clean_slot(value) -> int:
    """Zuordnung aus einer Datei - nur 1 bis 9 sind gueltig, sonst 0."""
    try:
        slot = int(value)
    except (TypeError, ValueError, OverflowError):   # OverflowError: Infinity
        return 0
    return slot if 1 <= slot <= 9 else 0


def _clean_crop(value) -> tuple | None:
    """Zuschnitt aus einer Datei pruefen - unsinnige Werte werden verworfen.

    Ohne diese Pruefung wuerde ein Zuschnitt wie [-1e9, -1e9, 1e9, 1e9] beim
    Zeichnen ein Bild mit 2*10^22 Pixeln anfordern.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        rect = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in rect):
        return None
    x0, y0, x1, y1 = (min(max(v, 0.0), 1.0) for v in rect)
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        return None
    return (x0, y0, x1, y1)


@dataclass
class Develop:
    """Nicht-destruktive Bearbeitung einer Aufnahme."""

    exposure: float = 0.0        # EV, -2 bis +3
    wb: str = "Kamera"           # Kamera | Auto | Kelvin
    kelvin: int = 5500
    tint: int = 0                # -100 gruen ... +100 magenta
    auto_bright: bool = True
    denoise: str = "aus"
    contrast: int = 0
    saturation: int = 0
    highlights: int = 0
    shadows: int = 0
    sharpen: int = 0
    orient: int = 0              # 0/90/180/270, grobe Drehung
    angle: float = 0.0           # -45 bis +45, Geraderichten
    ratio: str = "frei"
    crop: tuple | None = None    # (x0, y0, x1, y1), relativ zum geraden Bild

    def base_key(self, raw: bool = True) -> tuple:
        """Parameter, die eine erneute Entwicklung des Basisbilds erfordern.

        Bei eingeschalteter Auto-Helligkeit wird die Belichtung erst nach der
        Entwicklung angewendet - sie taucht deshalb hier nicht auf und der
        Regler wirkt ohne Wartezeit. Bei JPG & Co. steckt nur der Weissabgleich
        im Basisbild, die Belichtung kommt immer danach (pending_exposure).
        """
        if not raw:
            return (self.wb, self.kelvin, self.tint)
        return (0.0 if self.auto_bright else round(self.exposure, 2),
                self.wb, self.kelvin, self.tint, self.auto_bright, self.denoise)

    def is_default(self) -> bool:
        return self == Develop()

    def copy(self) -> "Develop":
        return Develop(**asdict(self))

    def transferred_to(self, target: "Develop") -> "Develop":
        """Diese Einstellungen fuer eine andere Aufnahme.

        Zuschnitt, Seitenverhaeltnis, Drehung und Geraderichten gehoeren zum
        Motiv - sie bleiben, wie sie bei der Zielaufnahme waren.
        """
        new = self.copy()
        for name in GEOMETRY_FIELDS:
            setattr(new, name, getattr(target, name))
        return new

    def to_dict(self) -> dict:
        data = asdict(self)
        data["crop"] = list(self.crop) if self.crop else None
        return data

    @staticmethod
    def from_dict(data: dict) -> "Develop":
        """Einstellungen aus der Datei uebernehmen - streng geprueft.

        Die Datei liegt im Quellordner und reist mit ihm mit, kann also
        beschaedigt oder absichtlich praepariert sein. Alles, was nicht passt,
        faellt auf den Standardwert zurueck.
        """
        dev = Develop()
        if not isinstance(data, dict):
            return dev
        for field_info in fields(Develop):
            name = field_info.name
            if name not in data:
                continue
            value = data[name]
            try:
                if name == "crop":
                    dev.crop = _clean_crop(value)
                elif name == "wb":
                    dev.wb = value if value in ("Kamera", "Auto", "Kelvin") else "Kamera"
                elif name == "denoise":
                    dev.denoise = value if value in DENOISE else "aus"
                elif name == "ratio":
                    dev.ratio = value if value in RATIOS else "frei"
                elif name == "orient":
                    dev.orient = int(value) % 360 // 90 * 90
                elif name == "auto_bright":
                    dev.auto_bright = bool(value)
                else:
                    lo, hi = DEV_LIMITS[name]
                    number = float(value)
                    if not math.isfinite(number):
                        continue
                    number = min(max(number, lo), hi)
                    setattr(dev, name, number if isinstance(lo, float) else int(number))
            except (TypeError, ValueError, KeyError, OverflowError):
                continue   # Standardwert behalten
        return dev


GEOMETRY_FIELDS = ("crop", "ratio", "orient", "angle")


@dataclass
class Preview:
    """Ergebnis des schnellen Hintergrund-Ladens."""

    image: Image.Image | None = None
    exif: dict = field(default_factory=dict)
    sharpness: float = 0.0
    full_size: tuple[int, int] | None = None   # Pixelmasse des Originals, fuer 100 %
    error: str = ""


# --------------------------------------------------------------------------
# Schnelle Vorschau (eingebettetes JPEG)
# --------------------------------------------------------------------------

def load_preview(path: Path) -> Preview:
    try:
        if path.suffix.lower() in RAW_EXT and rawpy is not None:
            img, thumb_exif, full = _load_raw_thumb(path)
        else:
            img = Image.open(path)
            full = img.size
            target = _draft_size(img.size, DRAFT_PX)
            if target:
                img.draft("RGB", target)
            img.load()
            img = _to_srgb(ImageOps.exif_transpose(img))
            thumb_exif = _exif_from_pil(img)

        if img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > MAX_DECODE_PX:
            img.thumbnail((MAX_DECODE_PX, MAX_DECODE_PX), Image.Resampling.LANCZOS)

        return Preview(image=img, exif=read_exif(path) or thumb_exif,
                       sharpness=analyse(img), full_size=_oriented(full, img.size))
    except Exception as exc:
        return Preview(error=f"{type(exc).__name__}: {exc}")


def load_full(path: Path) -> Image.Image:
    """Volle Aufloesung fuer die 100-%-Ansicht - im Hintergrund, kann dauern."""
    if is_raw(path):
        with rawpy.imread(str(path)) as raw:
            flip = getattr(raw.sizes, "flip", 0)
            sensor = max(raw.sizes.width, raw.sizes.height)
            try:
                thumb = raw.extract_thumb()
            except Exception:
                thumb = None
            if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
                img = Image.open(io.BytesIO(thumb.data))
                if max(img.size) >= 0.9 * sensor:   # Vorschaubild in voller Groesse
                    return _open_thumb(img, flip, draft=False)[0].convert("RGB")
            # Sonst bleibt nur die volle Entwicklung mit Kamera-Weissabgleich
            return Image.fromarray(raw.postprocess(use_camera_wb=True, output_bps=8))
    img = Image.open(path)
    img.load()
    return _to_srgb(ImageOps.exif_transpose(img))


def _load_raw_thumb(path: Path) -> tuple[Image.Image, dict, tuple[int, int] | None]:
    with rawpy.imread(str(path)) as raw:
        flip = getattr(raw.sizes, "flip", 0)
        full = (raw.sizes.width, raw.sizes.height)
        try:
            thumb = raw.extract_thumb()
        except Exception:
            thumb = None

        if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
            img, exif = _open_thumb(Image.open(io.BytesIO(thumb.data)), flip, draft=True)
            return img, exif, full

        if thumb is not None and thumb.format == rawpy.ThumbFormat.BITMAP:
            return _apply_flip(Image.fromarray(thumb.data), flip), {}, full

        img = Image.fromarray(raw.postprocess(half_size=True, use_camera_wb=True))
        return img, {}, full


def _open_thumb(img: Image.Image, flip: int, draft: bool) -> tuple[Image.Image, dict]:
    """Eingebettetes Vorschau-JPEG dekodieren und richtig herum drehen."""
    if draft:
        # Viele Kameras legen ein Vorschaubild in voller Groesse ab - draft()
        # laesst libjpeg direkt verkleinert dekodieren, das spart ein Vielfaches
        target = _draft_size(img.size, DRAFT_PX)
        if target:
            img.draft("RGB", target)
    img.load()
    exif = _exif_from_pil(img)
    had_orientation = bool(img.getexif().get(0x0112))
    img = ImageOps.exif_transpose(img)
    if not had_orientation:
        img = _apply_flip(img, flip)
    return img, exif


def _oriented(full: tuple[int, int] | None, shown: tuple[int, int]):
    """Originalmasse in der Ausrichtung des angezeigten Bildes."""
    if not full or not all(full):
        return None
    if (full[0] >= full[1]) != (shown[0] >= shown[1]):
        return full[1], full[0]
    return tuple(full)


_SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")) if ImageCms else None


def _to_srgb(img: Image.Image) -> Image.Image:
    """Eingebettetes Farbprofil (z. B. Adobe RGB) nach sRGB umrechnen.

    Ohne das wirken solche Bilder in der Vorschau flau, und ein Export ohne
    Profil zeigt sie anderswo mit falschen Farben.
    """
    icc = img.info.get("icc_profile")
    if icc and _SRGB is not None:
        try:
            profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            if "srgb" not in ImageCms.getProfileDescription(profile).lower():
                return ImageCms.profileToProfile(img, profile, _SRGB, outputMode="RGB")
        except Exception:
            pass   # defektes Profil - dann eben ohne Umrechnung
    return img if img.mode == "RGB" else img.convert("RGB")


def _apply_flip(img: Image.Image, flip: int) -> Image.Image:
    """LibRaw-Orientierung nachziehen, wenn der Thumb sie nicht schon hat."""
    if flip == 3:
        return img.transpose(Image.Transpose.ROTATE_180)
    if flip in (5, 6) and img.width >= img.height:
        rot = Image.Transpose.ROTATE_90 if flip == 5 else Image.Transpose.ROTATE_270
        return img.transpose(rot)
    return img


def _draft_size(size: tuple[int, int], limit: int) -> tuple[int, int] | None:
    """Zielgroesse fuer Image.draft() - seitenverhaeltnistreu.

    draft() verkleinert nur in Zweierpotenzen und nur, wenn das Ergebnis die
    angegebene Box noch vollstaendig ausfuellt. Eine quadratische Box bewirkt
    bei einem Querformat deshalb gar nichts. Und weil die naechste Stufe knapp
    unter der Obergrenze liegen kann, wird hier etwas weniger verlangt als
    MAX_DECODE_PX - sonst faellt die Verkleinerung ganz aus.
    """
    scale = limit / max(size)
    if scale >= 1:
        return None
    return max(1, int(size[0] * scale)), max(1, int(size[1] * scale))


def analyse(img: Image.Image) -> float:
    """Schaerfe-Score: Laplace-Varianz der Bildmitte.

    Gemessen wird immer bei derselben Kantenlaenge - die Laplace-Varianz
    haengt stark vom Massstab ab, und die Vorschaubilder sind je nach Kamera
    und Format unterschiedlich gross.
    """
    small = img
    if max(img.size) > ANALYSIS_PX:
        scale = ANALYSIS_PX / max(img.size)
        small = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                           Image.Resampling.BOX)
    grey = np.asarray(small.convert("L"), dtype=np.float32)
    h, w = grey.shape
    # Nur die mittleren 60 % bewerten - Bokeh am Rand ist ja gewollt
    grey = grey[int(h * 0.2):int(h * 0.8), int(w * 0.2):int(w * 0.8)]
    if grey.size < 64:
        return 0.0
    lap = (4 * grey[1:-1, 1:-1] - grey[:-2, 1:-1] - grey[2:, 1:-1]
           - grey[1:-1, :-2] - grey[1:-1, 2:])
    return float(lap.var())


# --------------------------------------------------------------------------
# Entwicklung
# --------------------------------------------------------------------------

def _blackbody_rgb(kelvin: int) -> np.ndarray:
    """Naeherung der Farbe eines Planckschen Strahlers (Tanner Helland)."""
    t = max(1000, min(kelvin, 40000)) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
    if t >= 66:
        b = 255.0
    elif t <= 19:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(t - 10) - 305.0447927307
    return np.clip(np.float32([r, g, b]), 1.0, 255.0)


def _kelvin_gains(kelvin: int, tint: int) -> np.ndarray:
    """Korrektur-Faktoren relativ zu Tageslicht (5500 K)."""
    gains = _blackbody_rgb(5500) / _blackbody_rgb(kelvin)
    gains = gains / gains[1]
    gains[1] *= 1.0 - tint / 250.0
    return gains


def is_raw(path: Path) -> bool:
    return path.suffix.lower() in RAW_EXT and rawpy is not None


def pending_exposure(path: Path, dev: Develop) -> float:
    """EV, das nach der Entwicklung noch anzuwenden ist.

    LibRaws Auto-Helligkeit zieht das Histogramm zurecht und macht eine
    Belichtungskorrektur im RAW damit weitgehend wirkungslos - gemessen war
    -1 EV bei den meisten Dateien exakt deckungsgleich mit 0 EV. Bei
    eingeschalteter Auto-Helligkeit wird die Belichtung deshalb erst danach
    angewendet; ausgeschaltet greift sie linear im RAW (bessere Lichter).
    JPG & Co. haben kein RAW - dort kommt die Belichtung immer danach.
    """
    if not is_raw(path):
        return dev.exposure
    return dev.exposure if dev.auto_bright else 0.0


def develop_base(path: Path, dev: Develop, half: bool = True) -> Image.Image:
    """Basis-Bild: RAW entwickeln bzw. JPG laden, inklusive Weissabgleich.

    half: fuer die Vorschau genuegt das RAW in halber Aufloesung bzw. ein JPG
    in Vorschaugroesse - ein 40-MP-JPG voll zu laden kostet sonst mit den
    Rechenkopien ueber ein Gigabyte.
    """
    if is_raw(path):
        return _develop_raw(path, dev, half)

    img = Image.open(path)
    if half:
        target = _draft_size(img.size, DRAFT_PX)
        if target:
            img.draft("RGB", target)
    img.load()
    img = _to_srgb(ImageOps.exif_transpose(img))
    if half and max(img.size) > MAX_DECODE_PX:
        img.thumbnail((MAX_DECODE_PX, MAX_DECODE_PX), Image.Resampling.LANCZOS)
    gains = _kelvin_gains(dev.kelvin, dev.tint) if dev.wb == "Kelvin" else None
    if dev.wb == "Auto":
        mean = np.float32(ImageStat.Stat(img).mean[:3])
        gains = np.float32(mean.mean() / np.maximum(mean, 1e-3))
    if gains is not None:
        img = _apply_gains(img, gains, 0.0)
    return img


def _raw_wb_reference(raw) -> np.ndarray | None:
    """Bezugs-Weissabgleich als R,G,B - None, wenn unbrauchbar.

    Tageslicht zuerst: dann bedeutet 5500 K auch Tageslicht und nicht
    "wie die Kamera es eingestellt hatte".
    """
    for attr in ("daylight_whitebalance", "camera_whitebalance"):
        values = list(getattr(raw, attr, []) or [])[:3]
        if len(values) == 3 and min(values) > 0:
            values = np.float32(values)
            return values / values[1]
    return None


def _develop_raw(path: Path, dev: Develop, half: bool) -> Image.Image:
    post_gains = None
    with rawpy.imread(str(path)) as raw:
        params = dict(
            half_size=half,
            output_bps=8,
            output_color=rawpy.ColorSpace.sRGB,
            no_auto_bright=not dev.auto_bright,
            use_camera_wb=False,
            use_auto_wb=False,
            fbdd_noise_reduction=rawpy.FBDDNoiseReductionMode(DENOISE[dev.denoise]),
            highlight_mode=rawpy.HighlightMode.Blend,
        )
        if dev.wb == "Auto":
            params["use_auto_wb"] = True
        elif dev.wb == "Kelvin":
            reference = _raw_wb_reference(raw)
            # Bereits demosaikte DNGs (z. B. gestitchte Drohnen-Panoramen) haben
            # keinen brauchbaren Kamera-Weissabgleich und LibRaw ignoriert dort
            # user_wb - in dem Fall wird die Korrektur hinterher angewendet
            if reference is None or getattr(raw, "raw_type", None) != rawpy.RawType.Flat:
                params["use_camera_wb"] = True
                post_gains = _kelvin_gains(dev.kelvin, dev.tint)
            else:
                wb = reference * _kelvin_gains(dev.kelvin, dev.tint)
                params["user_wb"] = [float(wb[0]), float(wb[1]),
                                     float(wb[2]), float(wb[1])]
        else:
            params["use_camera_wb"] = True

        if dev.exposure and not dev.auto_bright:
            # exp_shift arbeitet linear vor der Gradation - das ist die Stelle,
            # an der sich Lichter noch retten lassen. LibRaw laesst nur den
            # Bereich 0,25 bis 8, also -2 bis +3 EV. Mit Auto-Helligkeit waere
            # das wirkungslos, dort greift pending_exposure() danach
            params["exp_shift"] = float(min(max(2 ** dev.exposure, 0.25), 8.0))
            params["exp_preserve_highlights"] = 1.0

        img = Image.fromarray(raw.postprocess(**params))

    return _apply_gains(img, post_gains, 0.0) if post_gains is not None else img


_LEVELS = np.arange(256, dtype=np.float64) / 255.0


def _apply_gains(img: Image.Image, gains, exposure: float) -> Image.Image:
    """Belichtung und Farbfaktoren in linearem Licht anwenden.

    Jeder Kanal haengt nur von sich selbst ab - eine Tabelle mit 3 x 256
    Eintraegen ist deshalb exakt und um ein Vielfaches schneller als die
    Rechnung je Pixel.
    """
    factors = np.ones(3) if gains is None else np.asarray(gains, np.float64)
    factors = factors * 2.0 ** exposure
    lin = np.power(_LEVELS, 2.2)
    lut = np.concatenate([np.power(np.clip(lin * f, 0.0, 1.0), 1 / 2.2) for f in factors])
    return img.point((lut * 255 + 0.5).astype(np.uint8).tolist())


def _inscribed(w: float, h: float, angle: float) -> tuple[float, float]:
    """Groesstes achsenparalleles Rechteck im gedrehten Bild."""
    a = abs(math.radians(angle))
    if a < 1e-6:
        return w, h
    sin_a, cos_a = math.sin(a), math.cos(a)
    short, long_ = (w, h) if w <= h else (h, w)
    if short <= 2 * sin_a * cos_a * long_ or abs(sin_a - cos_a) < 1e-10:
        x = 0.5 * short
        return (x / sin_a, x / cos_a) if w <= h else (x / cos_a, x / sin_a)
    cos_2a = cos_a * cos_a - sin_a * sin_a
    return (w * cos_a - h * sin_a) / cos_2a, (h * cos_a - w * sin_a) / cos_2a


def straighten(img: Image.Image, dev: Develop) -> Image.Image:
    """Grobe Drehung und Geraderichten - ohne den Zuschnitt des Nutzers."""
    if dev.orient:
        img = img.rotate(-dev.orient, expand=True)
    if abs(dev.angle) > 0.05:
        img = img.rotate(dev.angle, resample=Image.Resampling.BICUBIC, expand=False)
        wr, hr = _inscribed(img.width, img.height, dev.angle)
        cx, cy = img.width / 2, img.height / 2
        img = img.crop((round(cx - wr / 2), round(cy - hr / 2),
                        round(cx + wr / 2), round(cy + hr / 2)))
    return img


def crop_to(img: Image.Image, crop: tuple | None) -> Image.Image:
    """Zuschnitt des Nutzers, relativ zum geraden Bild."""
    if crop:
        x0, y0, x1, y1 = crop
        box = (round(x0 * img.width), round(y0 * img.height),
               round(x1 * img.width), round(y1 * img.height))
        if box[2] - box[0] >= 16 and box[3] - box[1] >= 16:
            img = img.crop(box)
    return img


def apply_geometry(img: Image.Image, dev: Develop) -> Image.Image:
    return crop_to(straighten(img, dev), dev.crop)


def apply_rgb(img: Image.Image, dev: Develop, extra_ev: float = 0.0) -> Image.Image:
    """Belichtungsrest, Tonwerte, Kontrast, Saettigung, Schaerfen.

    Was nur vom Tonwert abhaengt, laeuft ueber Tabellen bzw. in C - die
    Vorschau rechnet das bei jedem Neuzeichnen.
    """
    if extra_ev:
        img = _apply_gains(img, None, extra_ev)
    if dev.highlights or dev.shadows:
        # Die Verstaerkung haengt nur von der Helligkeit ab: Tabelle ueber L
        gain_lut = np.ones(256, np.float32)
        if dev.shadows:
            gain_lut += np.float32((dev.shadows / 100.0) * 0.9 * (1.0 - _LEVELS) ** 2)
        if dev.highlights:
            gain_lut += np.float32((dev.highlights / 100.0) * 0.9 * _LEVELS ** 2)
        gain = gain_lut[np.asarray(img.convert("L"))]
        arr = np.asarray(img, np.float32) * gain[..., None]
        img = Image.fromarray(np.clip(arr + 0.5, 0, 255).astype(np.uint8))
    if dev.contrast:
        curve = np.clip((_LEVELS - 0.5) * (1 + dev.contrast / 100.0) + 0.5, 0.0, 1.0)
        img = img.point((curve * 255 + 0.5).astype(np.uint8).tolist() * 3)
    if dev.saturation:
        img = ImageEnhance.Color(img).enhance(1 + dev.saturation / 100.0)
    if dev.sharpen:
        img = img.filter(ImageFilter.UnsharpMask(radius=1.2,
                                                 percent=int(dev.sharpen * 2),
                                                 threshold=3))
    return img


def export_jpeg(path: Path, dev: Develop, dest: Path, quality: int,
                long_edge: int, strip_private: bool = True) -> None:
    """Volle Entwicklung in eine JPG-Datei schreiben.

    Geschrieben wird erst in eine .part-Datei: ein Abbruch hinterlaesst so
    keine halbe JPG unter dem richtigen Namen.
    """
    img = apply_geometry(develop_base(path, dev, half=False), dev)
    if long_edge and max(img.size) > long_edge:
        img.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    img = apply_rgb(img, dev, pending_exposure(path, dev))
    kwargs = dict(quality=quality, optimize=True,
                  subsampling=0 if quality >= 90 else 2)
    exif = _exif_bytes(path, img.size, strip_private)
    if exif:
        kwargs["exif"] = exif
    part = dest.with_name(f".{dest.name}.part")
    try:
        try:
            img.save(part, "JPEG", **kwargs)
        except OSError:
            # Bei stark verrauschten Bildern reicht Pillows Pufferschaetzung
            # fuer optimize=True nicht ("Suspension not allowed here") - dann
            # eben ohne Optimierung, die Datei wird nur etwas groesser
            img.save(part, "JPEG", **{**kwargs, "optimize": False})
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    _publish(part, dest)


def _exif_bytes(path: Path, size: tuple[int, int] | None = None,
                strip_private: bool = True) -> bytes | None:
    """EXIF fuer die Ausgabedatei - mit neutraler Orientierung und richtigen Massen.

    Die Aufnahmedaten sollen erhalten bleiben, Aufnahmeort und Seriennummern
    aber nur, wenn das ausdruecklich gewuenscht ist: exportierte JPGs sind
    das, was man weitergibt, und beides wandert sonst unbemerkt mit.
    """
    try:
        if path.suffix.lower() in RAW_EXT:
            if rawpy is None:
                return None
            with rawpy.imread(str(path)) as raw:
                thumb = raw.extract_thumb()
            if thumb.format != rawpy.ThumbFormat.JPEG:
                return None
            src = Image.open(io.BytesIO(thumb.data))
        else:
            src = Image.open(path)
        exif = src.getexif()
        if not exif:
            return None
        exif[0x0112] = 1          # wir haben schon gedreht
        for tag in (0x0100, 0x0101):   # Masse des Vorschaubilds
            exif.pop(tag, None)
        # Die Pixelmasse stehen im Exif-IFD, nicht im Basis-IFD
        sub = exif.get_ifd(0x8769)
        if size and 0x8769 in exif:
            sub[0xA002], sub[0xA003] = size
        if strip_private:
            exif.pop(0x8825, None)
            exif.get_ifd(0x8825).clear()
            for tag in PRIVATE_EXIF_TAGS:
                sub.pop(tag, None)
        return exif.tobytes()
    except Exception:
        return None


# --------------------------------------------------------------------------
# EXIF-Anzeige
# --------------------------------------------------------------------------

def read_exif(path: Path) -> dict:
    """EXIF direkt aus der Datei - klappt fuer JPG und die meisten RAWs."""
    if exifread is None:
        return {}
    try:
        with open(path, "rb") as fh:
            tags = exifread.process_file(fh, details=False)
    except Exception:
        return {}
    if not tags:
        return {}

    def val(key):
        tag = tags.get(key)
        return str(tag).strip() if tag is not None else ""

    def ratio(key):
        tag = tags.get(key)
        try:
            return tag.values[0].num / tag.values[0].den
        except Exception:
            return None

    return _clean({
        "camera": " ".join(x for x in (val("Image Make"), val("Image Model")) if x),
        "lens": val("EXIF LensModel") or val("MakerNote LensModel"),
        "aperture": _fmt_aperture(ratio("EXIF FNumber")),
        "shutter": _fmt_shutter(tags.get("EXIF ExposureTime")),
        "iso": val("EXIF ISOSpeedRatings"),
        "focal": _fmt_focal(ratio("EXIF FocalLength")),
        "date": val("EXIF DateTimeOriginal"),
    })


def _exif_from_pil(img: Image.Image) -> dict:
    """Rueckfall fuer Formate, die exifread nicht kennt (z. B. Canon CR3)."""
    try:
        base = img.getexif()
        ifd = base.get_ifd(0x8769)
    except Exception:
        return {}

    iso = ifd.get(0x8827)
    if isinstance(iso, (tuple, list)):
        iso = iso[0] if iso else None
    return _clean({
        "camera": " ".join(str(base.get(t, "")).strip() for t in (0x010F, 0x0110)),
        "lens": str(ifd.get(0xA434, "")).strip(),
        "aperture": _fmt_aperture(ifd.get(0x829D)),
        "shutter": _fmt_shutter(ifd.get(0x829A)),
        "iso": str(iso) if iso else "",
        "focal": _fmt_focal(ifd.get(0x920A)),
        "date": str(ifd.get(0x9003, "")).strip(),
    })


def _clean(data: dict) -> dict:
    out = {k: v for k, v in data.items() if v}
    # Doppelte Herstellernamen wie "NIKON CORPORATION NIKON Z 6" kuerzen
    words, seen = [], set()
    for word in out.get("camera", "").split():
        if word.upper() not in seen:
            seen.add(word.upper())
            words.append(word)
    if words:
        out["camera"] = " ".join(words)
    return out


def _fmt_aperture(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    return f"f/{f:.1f}".rstrip("0").rstrip(".") if f else ""


def _fmt_shutter(value) -> str:
    try:
        if hasattr(value, "values"):  # exifread Ratio
            secs = value.values[0].num / value.values[0].den
        else:
            secs = float(value)
    except Exception:
        return ""
    if not secs or secs < 0:
        return ""
    inverse = 1 / secs
    # Kurze Zeiten als Bruch; 0,4 oder 0,8 s aber nicht als "1/2" bzw. "1/1"
    if secs < 0.3 or (secs < 1 and abs(inverse - round(inverse)) < 0.01):
        return f"1/{round(inverse)} s"
    return f"{round(secs, 1):g} s"


def _fmt_focal(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{f:g} mm" if f else ""


# --------------------------------------------------------------------------
# Hintergrund-Threads
# --------------------------------------------------------------------------

class Failed:
    """Ergebnis einer Hintergrund-Arbeit, die nicht geklappt hat.

    Wird wie ein Ergebnis zwischengespeichert - sonst wuerde dieselbe defekte
    Datei nach jedem Fehlschlag sofort wieder angefordert.
    """

    def __init__(self, message: str) -> None:
        self.message = message


class CacheWorker(threading.Thread):
    """Hintergrund-Thread mit kleinem Ergebnis-Speicher.

    Angefordert wird immer der komplette aktuelle Bedarf (z. B. das Bild und
    seine Nachbarn). Was danach nicht mehr gebraucht wird, verwirft der Thread
    ungelesen - beim schnellen Durchblaettern stauen sich sonst hunderte
    veraltete Auftraege, die minutenlang abgearbeitet werden und das aktuelle
    Bild aus dem Speicher verdraengen.
    """

    def __init__(self, func, size: int) -> None:
        super().__init__(daemon=True)
        self.func = func
        self.size = size
        self.requests: queue.LifoQueue = queue.LifoQueue()
        self.done: queue.Queue = queue.Queue()
        self.cache: OrderedDict = OrderedDict()
        self.lock = threading.Lock()
        self.wanted: set = set()

    def get(self, key):
        """Ergebnis, Failed - oder None, solange es noch nicht da ist."""
        with self.lock:
            result = self.cache.get(key)
            if result is not None:
                self.cache.move_to_end(key)
            return result

    def request(self, jobs: list[tuple]) -> None:
        """jobs: (Schluessel, *Argumente), das Wichtigste zuerst."""
        with self.lock:
            self.wanted = {job[0] for job in jobs}
        for job in reversed(jobs):  # LIFO -> das erste Element zuletzt einreihen
            if self.get(job[0]) is None:
                self.requests.put(job)

    def run(self) -> None:
        while True:
            key, *args = self.requests.get()
            with self.lock:
                if key not in self.wanted or key in self.cache:
                    continue
            try:
                result = self.func(*args)
            except Exception as exc:
                result = Failed(f"{type(exc).__name__}: {exc}")
            with self.lock:
                self.cache[key] = result
                while len(self.cache) > self.size:
                    self.cache.popitem(last=False)
            self.done.put(key)


# --------------------------------------------------------------------------
# Datenmodell
# --------------------------------------------------------------------------

@dataclass
class Entry:
    """Eine Aufnahme - bei Gruppierung RAW und JPG derselben Aufnahme zusammen."""

    stem: str
    reldir: str
    files: list[Path] = field(default_factory=list)
    slot: int = 0
    sidecars: list[Path] = field(default_factory=list)   # XMP & Co., wandern mit

    @property
    def preview(self) -> Path:
        for f in self.files:  # JPG ist zum Sichten schneller als RAW
            if f.suffix.lower() in JPG_EXT:
                return f
        return self.files[0]

    @property
    def raw(self) -> Path | None:
        return next((f for f in self.files if f.suffix.lower() in RAW_EXT), None)

    @property
    def master(self) -> Path:
        """Die Datei, aus der entwickelt wird - RAW hat Vorrang."""
        return self.raw or self.files[0]

    @property
    def label(self) -> str:
        exts = "+".join(f.suffix.lstrip(".").upper() for f in self.files)
        extra = sorted({f.suffix.lstrip(".").upper() for f in self.sidecars})
        return f"{self.stem}  [{exts}]" + (f" +{'+'.join(extra)}" if extra else "")


@dataclass
class Slot:
    name: str = ""
    path: Path | None = None

    @property
    def title(self) -> str:
        return self.name or (self.path.name if self.path else "")


@dataclass
class Job:
    """Eine Aufnahme beim Ausfuehren - als Momentaufnahme.

    Der Arbeits-Thread darf nichts Lebendes anfassen: waehrend er laeuft,
    kann in der Oberflaeche weiter zugeordnet und umgestellt werden.
    """

    key: str             # str(entry.preview), zum Wiederfinden
    slot: int
    stem: str
    reldir: str
    target: Path
    master: Path
    files: list[Path]    # zu kopieren/verschieben - leer bei "statt der RAW-Datei"
    dev: Develop
    develop: bool


# --------------------------------------------------------------------------
# Anwendung
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PhotoSelection")
        # Pixelangaben unten sind fuer 96 dpi gedacht - bei 125/150 % mitwachsen
        self.ui_scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        self.geometry(f"{self.px(1500)}x{self.px(900)}")
        self.minsize(self.px(980), self.px(600))

        self.source: Path | None = None
        self.slots: dict[int, Slot] = {i: Slot() for i in range(1, 10)}
        self.entries: list[Entry] = []
        self.view: list[Entry] = []
        self.index = 0
        self.undo_stack: list[dict] = []
        self.develop: dict[str, Develop] = {}      # Schluessel: Pfad der Master-Datei
        self._dev_clipboard: Develop | None = None
        # Eintraege der Zuordnungsdatei zu Dateien, die gerade nicht eingelesen
        # sind (z. B. Unterordner bei ausgeschaltetem "Unterordner") - sie
        # werden beim Speichern unveraendert mitgeschrieben
        self._foreign_marks: dict = {"assignments": {}, "develop": {}}

        self.loader = CacheWorker(load_preview, CACHE_SIZE)
        self.loader.start()
        self.full_loader = CacheWorker(load_full, FULL_CACHE_SIZE)
        self.full_loader.start()
        self.developer = CacheWorker(develop_base, DEV_CACHE_SIZE)
        self.developer.start()
        # Tk darf nur aus dem Hauptthread bedient werden - Arbeits-Threads
        # melden sich ueber diese Queue, abgeholt wird in _poll()
        self.worker_q: queue.Queue = queue.Queue()
        self._cancel = threading.Event()

        # zoom: Bildschirmpixel je Pixel des Originals - 1.0 ist echte 100 %
        self.zoom: float | None = None
        self.center = (0.5, 0.5)
        self._zoom_info = None        # (Einpassen-Zoom, Breite, Hoehe) in Originalpixeln
        self._drag = None
        self._crop_drag = None
        self._crop_before = None
        self._photo = None
        self._img_box = None          # (left, top, breite, hoehe) auf der Leinwand
        self._straight_size = None    # Masse des geraden Bildes, Bezug fuer den Zuschnitt
        self._geo_cache = None
        self._resize_job = None
        self._render_job = None
        self._drag_job = None
        self._save_job = None
        self._poll_job = None
        self._dev_pending = None
        self._busy = False
        self._close_after_run = False
        self._progress = ""
        self._save_error = ""
        self._error_shown = False
        self._suspend_trace = False

        self.recursive = tk.BooleanVar(value=False)
        self.group_pairs = tk.BooleanVar(value=True)
        self.keep_tree = tk.BooleanVar(value=False)
        self.move_files = tk.BooleanVar(value=False)
        self.filter_mode = tk.StringVar(value="Alle")
        self.sort_mode = tk.StringVar(value=SORT_MODES[0])
        self.show_exif = tk.BooleanVar(value=True)
        self.show_hist = tk.BooleanVar(value=False)
        self.show_clip = tk.BooleanVar(value=False)
        self.keep_zoom = tk.BooleanVar(value=False)
        self.dev_mode = tk.BooleanVar(value=False)
        self.crop_mode = tk.BooleanVar(value=False)

        self.export_cfg = {"develop": False, "mode": "zusaetzlich",
                           "quality": 92, "long_edge": 0, "strip_gps": True}

        self._load_slots()
        self._build_ui()
        self._bind_keys()
        self._poll_job = self.after(40, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def px(self, value: float) -> int:
        """Pixelangabe fuer 96 dpi auf die tatsaechliche Bildschirmaufloesung."""
        return round(value * self.ui_scale)

    def report_callback_exception(self, exc, val, tb) -> None:
        """Fehler sichtbar machen - unter pythonw gibt es keine Konsole."""
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")
        except OSError:
            pass
        try:
            self.status.config(text=f"Fehler: {val}  (Details in {LOG_FILE})")
        except (AttributeError, tk.TclError):
            pass
        if not self._error_shown:   # nur einmal - Folgefehler stehen im Protokoll
            self._error_shown = True
            messagebox.showerror("PhotoSelection",
                                 f"Unerwarteter Fehler:\n{val}\n\nDetails stehen in\n{LOG_FILE}")

    # -- Aufbau ------------------------------------------------------------

    def _build_ui(self) -> None:
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        self.btn_source = ttk.Button(bar, text="Quellordner…", command=self.choose_source)
        self.btn_source.pack(side=tk.LEFT)
        self.lbl_source = ttk.Label(bar, text="- kein Ordner -", width=34, anchor="w")
        self.lbl_source.pack(side=tk.LEFT, padx=(6, 12))
        self.chk_recursive = ttk.Checkbutton(bar, text="Unterordner", variable=self.recursive,
                                             command=self.rescan)
        self.chk_recursive.pack(side=tk.LEFT)
        self.chk_pairs = ttk.Checkbutton(bar, text="RAW+JPG als Paar",
                                         variable=self.group_pairs, command=self.rescan)
        self.chk_pairs.pack(side=tk.LEFT, padx=(10, 12))
        ttk.Label(bar, text="Anzeigen:").pack(side=tk.LEFT)
        self.cmb_filter = ttk.Combobox(bar, textvariable=self.filter_mode, width=15,
                                       state="readonly",
                                       values=["Alle", "Nur zugeordnete", "Nur offene"]
                                              + [f"Nur Ordner {i}" for i in range(1, 10)])
        self.cmb_filter.pack(side=tk.LEFT, padx=6)
        self.cmb_filter.bind("<<ComboboxSelected>>", lambda e: self._combo_done(self.refresh_view))
        ttk.Label(bar, text="Sortierung:").pack(side=tk.LEFT, padx=(6, 0))
        self.cmb_sort = ttk.Combobox(bar, textvariable=self.sort_mode, width=10,
                                     state="readonly", values=list(SORT_MODES))
        self.cmb_sort.pack(side=tk.LEFT, padx=6)
        self.cmb_sort.bind("<<ComboboxSelected>>", lambda e: self._combo_done(self.resort))

        self.btn_run = ttk.Button(bar, text="Zuordnungen ausführen",
                                  command=self.run_assignments)
        self.btn_run.pack(side=tk.RIGHT)
        self.btn_export = ttk.Button(bar, text="Export…", command=self.open_export_dialog)
        self.btn_export.pack(side=tk.RIGHT, padx=6)
        ttk.Checkbutton(bar, text="Struktur erhalten",
                        variable=self.keep_tree).pack(side=tk.RIGHT, padx=8)
        ttk.Checkbutton(bar, text="Verschieben",
                        variable=self.move_files).pack(side=tk.RIGHT)

        main = ttk.Frame(self)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.pane = ttk.PanedWindow(main, orient=tk.HORIZONTAL)
        self.pane.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        left = ttk.Frame(self.pane)
        self._build_slot_panel(left)
        listbox_frame = ttk.Frame(left)
        listbox_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.listbox = tk.Listbox(listbox_frame, activestyle="none", exportselection=False,
                                  takefocus=False, font=("Consolas", 9),
                                  selectbackground="#3d7eff", selectforeground="white")
        scroll = ttk.Scrollbar(listbox_frame, orient=tk.VERTICAL, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)
        # Danach gehoeren die Tasten wieder dem Bild - sonst wirkt z. B. die
        # Leertaste auf den Listeneintrag statt auf das angezeigte Bild
        self.listbox.bind("<ButtonRelease-1>", lambda e: self.canvas.focus_set())
        self.pane.add(left, weight=0)

        centre = ttk.Frame(self.pane)
        self.canvas = tk.Canvas(centre, bg="#1e1e1e", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.pane.add(centre, weight=1)

        self.dev_panel = self._build_dev_panel(main)

        # Die Trennlinie erst setzen, wenn die Leiste wirklich Breite hat -
        # vorher wird sashpos auf 0 begrenzt und die linke Leiste mit den
        # Zielordnern bleibt zugeklappt
        def place_sash(event) -> None:
            if event.width > 2 * self.px(380):
                self.pane.unbind("<Configure>", self._sash_binding)
                self.after_idle(lambda: self.pane.sashpos(0, self.px(380)))
        self._sash_binding = self.pane.bind("<Configure>", place_sash, add="+")

        self.status = ttk.Label(self, anchor="w", padding=(8, 4), relief=tk.SUNKEN,
                                text="Quellordner wählen…")
        self.status.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(self, anchor="w", padding=(8, 2), foreground="#666",
                  text="1–9 zuordnen · Leertaste/Enter = Ordner 1 + weiter · "
                       "0/Entf löschen · ←/→ blättern · Strg+Z rückgängig · "
                       "E Bearbeiten · K Zuschnitt · I EXIF · H Histogramm · "
                       "J Clipping · Z 100 % · L Zoom halten · R drehen · F Vollbild"
                  ).pack(side=tk.BOTTOM, fill=tk.X)

    def _combo_done(self, action) -> None:
        """Nach der Auswahl die Tasten wieder dem Bild geben.

        Sonst bleibt der Fokus in der Combobox: 1-9 und die Pfeile kommen
        nicht mehr an, und Pfeil hoch/runter aendern sogar die Auswahl.
        """
        self.canvas.focus_set()
        action()

    def _build_slot_panel(self, parent) -> None:
        box = ttk.LabelFrame(parent, text="Zielordner (Tasten 1–9)", padding=(6, 4))
        box.pack(fill=tk.X, padx=6, pady=(6, 0))
        box.columnconfigure(2, weight=1)

        self.slot_widgets: dict[int, dict] = {}
        for i in range(1, 10):
            tk.Label(box, text=str(i), width=2, bg=SLOT_COLORS[i], fg="#111",
                     font=("Segoe UI", 9, "bold")).grid(row=i, column=0, padx=(0, 4),
                                                        pady=1, sticky="w")
            name_var = tk.StringVar(value=self.slots[i].name)
            name_var.trace_add("write", lambda *_, s=i: self._on_slot_rename(s))
            name_entry = ttk.Entry(box, textvariable=name_var, width=11)
            name_entry.grid(row=i, column=1, padx=(0, 4), pady=1)
            for key in ("<Return>", "<KP_Enter>", "<Escape>"):   # fertig benannt
                name_entry.bind(key, lambda e: self.canvas.focus_set())
            btn = ttk.Button(box, width=24, command=lambda s=i: self.choose_slot_folder(s))
            btn.grid(row=i, column=2, pady=1, sticky="ew")
            count = ttk.Label(box, text="", width=5, anchor="e")
            count.grid(row=i, column=3, padx=(4, 0), pady=1)
            ttk.Button(box, text="✕", width=2,
                       command=lambda s=i: self.clear_slot_folder(s)).grid(
                row=i, column=4, padx=(2, 0), pady=1)
            self.slot_widgets[i] = {"name": name_var, "button": btn, "count": count}
        self._refresh_slot_panel()

    def _build_dev_panel(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=(8, 6))
        self.dev_vars: dict[str, tk.Variable] = {}

        head = ttk.Frame(frame)
        head.pack(fill=tk.X)
        ttk.Label(head, text="Bearbeitung", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(head, text="Zurücksetzen", width=13,
                   command=self.reset_develop).pack(side=tk.RIGHT)

        row = ttk.Frame(frame)
        row.pack(fill=tk.X, pady=(4, 8))
        ttk.Button(row, text="Kopieren", command=self.copy_develop).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 3))
        ttk.Button(row, text="Einfügen", command=self.paste_develop).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=3)
        ttk.Button(row, text="Auf alle", command=self.apply_develop_to_all).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=(3, 0))

        self._slider(frame, "exposure", "Belichtung", -2.0, 3.0, "{:+.2f} EV")
        wb_row = ttk.Frame(frame)
        wb_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(wb_row, text="Weißabgleich", width=13).pack(side=tk.LEFT)
        self.dev_vars["wb"] = tk.StringVar(value="Kamera")
        cmb = ttk.Combobox(wb_row, textvariable=self.dev_vars["wb"], width=9,
                           state="readonly", values=["Kamera", "Auto", "Kelvin"])
        cmb.pack(side=tk.LEFT)
        cmb.bind("<<ComboboxSelected>>", lambda e: self._combo_done(self._on_dev_change))
        self._slider(frame, "kelvin", "Kelvin", 2000, 12000, "{:.0f} K")
        self._slider(frame, "tint", "Tönung", -100, 100, "{:+.0f}")
        self.dev_vars["auto_bright"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Auto-Helligkeit", variable=self.dev_vars["auto_bright"],
                        command=self._on_dev_change).pack(anchor="w", pady=(4, 0))
        dn_row = ttk.Frame(frame)
        dn_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(dn_row, text="Rauschen", width=13).pack(side=tk.LEFT)
        self.dev_vars["denoise"] = tk.StringVar(value="aus")
        cmb = ttk.Combobox(dn_row, textvariable=self.dev_vars["denoise"], width=9,
                           state="readonly", values=list(DENOISE))
        cmb.pack(side=tk.LEFT)
        cmb.bind("<<ComboboxSelected>>", lambda e: self._combo_done(self._on_dev_change))

        ttk.Separator(frame).pack(fill=tk.X, pady=8)
        self._slider(frame, "highlights", "Lichter", -100, 100, "{:+.0f}")
        self._slider(frame, "shadows", "Schatten", -100, 100, "{:+.0f}")
        self._slider(frame, "contrast", "Kontrast", -100, 100, "{:+.0f}")
        self._slider(frame, "saturation", "Sättigung", -100, 100, "{:+.0f}")
        self._slider(frame, "sharpen", "Schärfen", 0, 100, "{:.0f}")

        ttk.Separator(frame).pack(fill=tk.X, pady=8)
        ttk.Label(frame, text="Zuschnitt", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ratio_row = ttk.Frame(frame)
        ratio_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(ratio_row, text="Verhältnis", width=13).pack(side=tk.LEFT)
        self.dev_vars["ratio"] = tk.StringVar(value="frei")
        cmb = ttk.Combobox(ratio_row, textvariable=self.dev_vars["ratio"], width=9,
                           state="readonly", values=list(RATIOS))
        cmb.pack(side=tk.LEFT)
        cmb.bind("<<ComboboxSelected>>", lambda e: self._combo_done(self._on_ratio_change))
        self._slider(frame, "angle", "Drehung", -45.0, 45.0, "{:+.1f}°")
        crop_row = ttk.Frame(frame)
        crop_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Checkbutton(crop_row, text="Zuschnitt zeichnen (K)", variable=self.crop_mode,
                        command=self.show_current).pack(side=tk.LEFT)
        ttk.Button(crop_row, text="↺", width=3, command=self.reset_crop).pack(side=tk.RIGHT)

        ttk.Label(frame, foreground="#666", wraplength=self.px(250), justify="left",
                  text="Die Vorschau entwickelt das RAW in halber Auflösung. "
                       "Ohne Bearbeitungsmodus zeigt sie das Kamera-JPEG, "
                       "Belichtung und Weißabgleich dann nur genähert.\n\n"
                       "Auto-Helligkeit aus: die Belichtung greift linear im RAW, "
                       "Lichter lassen sich dann eher retten — das Ausgangsbild "
                       "ist aber deutlich flauer."
                  ).pack(anchor="w", pady=(10, 0))
        return frame

    def _slider(self, parent, key: str, label: str, lo, hi, fmt: str) -> None:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=1)
        ttk.Label(row, text=label, width=13).pack(side=tk.LEFT)
        default = getattr(Develop(), key)
        var = tk.DoubleVar(value=default)
        self.dev_vars[key] = var
        value_lbl = ttk.Label(row, text=fmt.format(default), width=9, anchor="e")
        value_lbl.pack(side=tk.RIGHT)
        scale = ttk.Scale(row, from_=lo, to=hi, variable=var, orient=tk.HORIZONTAL)
        scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        var.trace_add("write", lambda *_, k=key, l=value_lbl, f=fmt:
                      self._on_slider(k, l, f))
        scale.bind("<Double-Button-1>", lambda e, k=key: self._reset_slider(k))
        # Sonst verstellen die Pfeiltasten beim Blaettern nebenbei den Regler
        scale.bind("<ButtonRelease-1>", lambda e: self.canvas.focus_set(), add="+")

    def _bind_keys(self) -> None:
        def b(sequence, func):
            def guarded(event, func=func):
                widget = event.widget
                # Nur im Hauptfenster (nicht etwa im Export-Dialog), und
                # solange ein Eingabefeld den Fokus hat, gehoeren die Tasten
                # diesem. Bei der aufgeklappten Combobox-Liste ist widget nur
                # ein Name - dann ebenfalls nichts tun.
                if not isinstance(widget, tk.Misc) or widget.winfo_toplevel() is not self:
                    return None
                if isinstance(widget, (ttk.Entry, tk.Entry, tk.Spinbox)):
                    return None
                func()
                return "break"
            self.bind_all(sequence, guarded)

        def letter(char, func):   # auch bei eingeschalteter Feststelltaste
            b(f"<KeyPress-{char}>", func)
            b(f"<KeyPress-{char.upper()}>", func)

        b("<Right>", lambda: self.step(1))
        b("<Left>", lambda: self.step(-1))
        b("<Next>", lambda: self.step(10))
        b("<Prior>", lambda: self.step(-10))
        b("<Home>", lambda: self.goto(0))
        b("<End>", lambda: self.goto(len(self.view) - 1))
        b("<space>", lambda: self.assign(1, toggle=True, advance=True))
        b("<Return>", lambda: self.assign(1, toggle=True, advance=True))
        b("<KP_Enter>", lambda: self.assign(1, toggle=True, advance=True))
        for i in range(1, 10):
            b(f"<KeyPress-{i}>", lambda s=i: self.assign(s, toggle=True))
            b(f"<KeyPress-KP_{i}>", lambda s=i: self.assign(s, toggle=True))
        b("<KeyPress-0>", lambda: self.assign(0))
        b("<KeyPress-KP_0>", lambda: self.assign(0))
        b("<Delete>", lambda: self.assign(0))
        b("<BackSpace>", lambda: self.assign(0))
        b("<Control-z>", self.undo)
        b("<Control-Z>", self.undo)
        letter("e", self.toggle_develop)
        letter("k", self.toggle_crop)
        letter("i", lambda: self._toggle_overlay(self.show_exif))
        letter("h", lambda: self._toggle_overlay(self.show_hist))
        letter("j", lambda: self._toggle_overlay(self.show_clip))
        letter("l", self.toggle_keep_zoom)
        b("<KeyPress-r>", lambda: self.rotate(90))
        b("<KeyPress-R>", lambda: self.rotate(-90))
        letter("z", lambda: self.set_zoom(None if self.zoom else 1.0))
        letter("f", self.toggle_fullscreen)
        b("<F11>", self.toggle_fullscreen)
        b("<Escape>", lambda: self.attributes("-fullscreen", False))

    def _toggle_overlay(self, var: tk.BooleanVar) -> None:
        var.set(not var.get())
        self.show_current()

    def toggle_keep_zoom(self) -> None:
        self.keep_zoom.set(not self.keep_zoom.get())
        self.status.config(text="Zoom bleibt beim Blättern erhalten." if self.keep_zoom.get()
                           else "Beim Blättern wird wieder eingepasst.")

    # -- Zielordner --------------------------------------------------------

    def _load_slots(self) -> None:
        try:
            data = json.loads(SLOTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        slots = data.get("slots")
        for key, item in (slots if isinstance(slots, dict) else {}).items():
            i = _clean_slot(key)
            if not i or not isinstance(item, dict):
                continue
            path = item.get("path")
            self.slots[i] = Slot(name=str(item.get("name", ""))[:40],
                                 path=Path(path) if isinstance(path, str) else None)

        export = data.get("export")
        if isinstance(export, dict):
            self.export_cfg["develop"] = bool(export.get("develop", False))
            self.export_cfg["strip_gps"] = bool(export.get("strip_gps", True))
            if export.get("mode") in ("zusaetzlich", "statt"):
                self.export_cfg["mode"] = export["mode"]
            try:
                self.export_cfg["quality"] = min(max(int(export.get("quality", 92)), 60), 100)
                edge = int(export.get("long_edge", 0))
                self.export_cfg["long_edge"] = edge if edge in LONG_EDGES.values() else 0
            except (TypeError, ValueError, OverflowError):
                pass

    def _save_slots(self) -> None:
        data = {"slots": {str(i): {"name": s.name,
                                   "path": str(s.path) if s.path else None}
                          for i, s in self.slots.items()},
                "export": self.export_cfg}
        try:
            _write_atomic(SLOTS_FILE, json.dumps(data, indent=1))
        except OSError:
            pass

    def choose_slot_folder(self, i: int) -> None:
        start = str(self.slots[i].path or self.source or Path.home())
        path = filedialog.askdirectory(title=f"Zielordner für Taste {i}", initialdir=start)
        if not path:
            return
        self.slots[i].path = Path(path)
        if not self.slots[i].name:
            self.slot_widgets[i]["name"].set(Path(path).name[:14])
        self._save_slots()
        self._refresh_slot_panel()

    def clear_slot_folder(self, i: int) -> None:
        self.slots[i].path = None
        self._save_slots()
        self._refresh_slot_panel()

    def _on_slot_rename(self, i: int) -> None:
        if i not in self.slot_widgets:
            return
        self.slots[i].name = self.slot_widgets[i]["name"].get()
        self._save_slots()

    def _refresh_slot_panel(self) -> None:
        counts = {i: 0 for i in range(1, 10)}
        for entry in self.entries:
            if entry.slot:
                counts[entry.slot] += 1
        for i, widgets in self.slot_widgets.items():
            path = self.slots[i].path
            widgets["button"].config(text=self._short(path, 28) if path else "Ordner wählen…")
            widgets["count"].config(text=str(counts[i]) if counts[i] else "")
        if self._busy:
            self.btn_run.config(text="Abbrechen")
            return
        total = sum(counts.values())
        suffix = " + JPG" if self.export_cfg["develop"] else ""
        self.btn_run.config(text=f"Zuordnungen ausführen ({total}){suffix}")

    # -- Quellordner und Scan ---------------------------------------------

    def choose_source(self) -> None:
        if self._busy:
            return
        path = filedialog.askdirectory(title="Quellordner mit Fotos wählen")
        if not path:
            return
        self._flush_marks()          # gehoert noch zum alten Ordner
        self.source = Path(path)
        self.lbl_source.config(text=self._short(self.source, 34))
        self.undo_stack.clear()
        self.develop.clear()
        self.rescan()

    @staticmethod
    def _short(path, width: int = 40) -> str:
        text = str(path)
        return text if len(text) <= width else "…" + text[-(width - 1):]

    def rescan(self) -> None:
        if not self.source or self._busy:
            return
        # Noch nicht geschriebene Zuordnungen zuerst sichern - _load_marks
        # liest gleich aus der Datei
        self._flush_marks()
        try:
            found = self._scan_files(self.source)
        except OSError as exc:
            messagebox.showerror("PhotoSelection", f"Ordner nicht lesbar:\n{exc}")
            found = []
        images = [f for f in found if f.suffix.lower() not in SIDECAR_EXT]
        sidecars = [f for f in found if f.suffix.lower() in SIDECAR_EXT]

        groups: OrderedDict[tuple, Entry] = OrderedDict()
        for f in images:
            reldir = str(f.parent.relative_to(self.source))
            key = (reldir, f.stem.lower()) if self.group_pairs.get() else (reldir, f.name.lower())
            entry = groups.get(key)
            if entry is None:
                entry = groups[key] = Entry(stem=f.stem, reldir=reldir)
            entry.files.append(f)

        for entry in groups.values():
            entry.files.sort(key=lambda p: (p.suffix.lower() not in JPG_EXT, p.name))
        self._attach_sidecars(list(groups.values()), sidecars)

        self.entries = list(groups.values())
        self._sort_entries()
        self._load_marks()
        self.index = 0
        self.refresh_view()

    def _attach_sidecars(self, entries: list[Entry], sidecars: list[Path]) -> None:
        """Begleitdateien ihrer Aufnahme zuordnen.

        IMG_1.xmp gehoert zur Aufnahme IMG_1 (bei getrennten RAW und JPG zur
        RAW-Datei), IMG_1.CR2.xmp genau zur Datei IMG_1.CR2.
        """
        by_stem: dict[tuple, list[Entry]] = {}
        for entry in entries:
            by_stem.setdefault((entry.reldir, entry.stem.lower()), []).append(entry)
        for f in sidecars:
            reldir = str(f.parent.relative_to(self.source))
            base = f.name[:-len(f.suffix)]
            inner = Path(base).suffix.lower()
            stem = Path(base).stem if inner in ALL_EXT else base
            candidates = by_stem.get((reldir, stem.lower()), [])
            if not candidates:
                continue   # verwaiste Begleitdatei - bleibt, wo sie ist
            exact = [e for e in candidates
                     if any(p.name.lower() == base.lower() for p in e.files)]
            with_raw = [e for e in candidates if e.raw is not None]
            (exact or with_raw or candidates)[0].sidecars.append(f)

    def _sort_entries(self) -> None:
        if self.sort_mode.get() == "Dateidatum":
            def mtime(entry: Entry) -> float:
                try:
                    return entry.master.stat().st_mtime
                except OSError:
                    return 0.0
            self.entries.sort(key=lambda e: (mtime(e), _natural_key(e.stem)))
        else:
            self.entries.sort(key=lambda e: (_natural_key(e.reldir), _natural_key(e.stem)))

    def resort(self) -> None:
        self._sort_entries()
        self.refresh_view()

    def _scan_files(self, root: Path) -> list[Path]:
        """Bilder und Begleitdateien einsammeln.

        Verknuepfungen (Symlinks und Windows-Junctions) werden uebersprungen:
        der Scan soll den Quellordner nicht verlassen, und eine Datei, die nur
        ueber eine Verknuepfung erreichbar ist, wuerde beim Verschieben von
        ganz woanders weggeholt. os.walk folgt Junctions, deshalb von Hand.
        """
        wanted = ALL_EXT | SIDECAR_EXT
        found: list[Path] = []
        pending = [root]
        while pending:
            folder = pending.pop()
            try:
                with os.scandir(folder) as it:
                    items = sorted(it, key=lambda e: e.name)
            except OSError:
                if folder == root:
                    raise
                continue   # unlesbarer Unterordner
            subdirs = []
            for item in items:
                if _is_link(item):
                    continue
                if item.is_dir(follow_symlinks=False):
                    subdirs.append(Path(item.path))
                elif (item.is_file(follow_symlinks=False)
                      and Path(item.name).suffix.lower() in wanted):
                    found.append(Path(item.path))
            if self.recursive.get():
                pending.extend(reversed(subdirs))   # Tiefensuche in Namensfolge
        return found

    # -- Speichern ---------------------------------------------------------

    def _load_marks(self) -> None:
        self._foreign_marks = {"assignments": {}, "develop": {}}
        path = self.source / MARKS_FILE if self.source else None
        if not path or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        # Die Datei reist mit dem Ordner mit und kann beschädigt oder
        # präpariert sein - deshalb wird alles daraus geprüft
        if not isinstance(data, dict):
            return
        assignments = data.get("assignments")
        develop = data.get("develop")
        assignments = assignments if isinstance(assignments, dict) else {}
        develop = develop if isinstance(develop, dict) else {}

        known = set()
        for entry in self.entries:
            for f in entry.files:
                rel = self._rel(f)
                known.add(rel)
                slot = _clean_slot(assignments.get(rel))
                if slot and not entry.slot:
                    entry.slot = slot
            stored = develop.get(self._rel(entry.master))
            if stored:
                self.develop[str(entry.master)] = Develop.from_dict(stored)

        # Eintraege zu Dateien, die gerade nicht eingelesen sind, aufheben -
        # aber nur, solange es die Datei noch gibt
        for name, value in assignments.items():
            slot = _clean_slot(value)
            if name not in known and slot and self._exists_in_source(name):
                self._foreign_marks["assignments"][name] = slot
        for name, value in develop.items():
            if name not in known and isinstance(value, dict) and self._exists_in_source(name):
                self._foreign_marks["develop"][name] = Develop.from_dict(value).to_dict()

    def _exists_in_source(self, name) -> bool:
        """Liegt diese Datei (Pfad aus der Zuordnungsdatei) im Quellordner?"""
        if not isinstance(name, str) or ":" in name or "\\" in name:
            return False
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts:
            return False
        return (self.source / rel).is_file()

    def _save_marks(self) -> None:
        """Zuordnungen und Bearbeitung jetzt schreiben (und anstehendes Speichern erledigen)."""
        if self._save_job:
            self.after_cancel(self._save_job)
            self._save_job = None
        if not self.source:
            return
        assignments = dict(self._foreign_marks["assignments"])
        assignments.update({self._rel(f): e.slot
                            for e in self.entries if e.slot for f in e.files})
        develop = dict(self._foreign_marks["develop"])
        for entry in self.entries:
            dev = self.develop.get(str(entry.master))
            if dev is not None and not dev.is_default():
                develop[self._rel(entry.master)] = dev.to_dict()
        try:
            _write_atomic(self.source / MARKS_FILE,
                          json.dumps({"assignments": assignments, "develop": develop}, indent=1))
            self._save_error = ""
        except OSError as exc:   # z. B. schreibgeschützte Speicherkarte
            self._save_error = f"Zuordnungen nicht gespeichert: {exc.strerror or exc}"

    def _mark_dirty(self) -> None:
        """Gleich speichern - gebuendelt, statt bei jedem Tastendruck die Datei zu schreiben."""
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(400, self._save_marks_later)

    def _save_marks_later(self) -> None:
        self._save_job = None
        self._save_marks()
        if self._save_error:
            self._update_status()

    def _flush_marks(self) -> None:
        if self._save_job:
            self._save_marks()

    def _rel(self, f: Path) -> str:
        return f.relative_to(self.source).as_posix()

    # -- Liste und Navigation ---------------------------------------------

    def refresh_view(self) -> None:
        current = self.view[self.index] if 0 <= self.index < len(self.view) else None
        mode = self.filter_mode.get()
        if mode == "Nur zugeordnete":
            self.view = [e for e in self.entries if e.slot]
        elif mode == "Nur offene":
            self.view = [e for e in self.entries if not e.slot]
        elif mode.startswith("Nur Ordner "):
            wanted = int(mode.rsplit(" ", 1)[1])
            self.view = [e for e in self.entries if e.slot == wanted]
        else:
            self.view = list(self.entries)

        self.listbox.delete(0, tk.END)
        for i, entry in enumerate(self.view):
            self.listbox.insert(tk.END, self._row_text(entry))
            if entry.slot:
                self.listbox.itemconfig(i, foreground=LIST_COLORS[entry.slot])

        self.index = self.view.index(current) if current in self.view else \
            max(0, min(self.index, len(self.view) - 1))
        self._refresh_slot_panel()
        self._sync_dev_panel()
        self.show_current()

    def _row_text(self, entry: Entry) -> str:
        dev = self.develop.get(str(entry.master))
        mark = "✎" if dev is not None and not dev.is_default() else " "
        return f"{entry.slot if entry.slot else '·'}{mark} {entry.label}"

    def goto(self, index: int) -> None:
        if not self.view:
            return
        self.index = max(0, min(index, len(self.view) - 1))
        if not self.keep_zoom.get():   # mit L bleibt der Ausschnitt fuer Serienvergleiche
            self.zoom = None
            self.center = (0.5, 0.5)
        self._sync_dev_panel()
        self.show_current()

    def step(self, delta: int) -> None:
        self.goto(self.index + delta)

    def _on_list_select(self, _event) -> None:
        sel = self.listbox.curselection()
        if sel and sel[0] != self.index:
            self.goto(sel[0])

    def _update_row(self, index: int) -> None:
        entry = self.view[index]
        self.listbox.delete(index)
        self.listbox.insert(index, self._row_text(entry))
        self.listbox.itemconfig(index, foreground=LIST_COLORS.get(entry.slot, "black"))
        self._select_row()

    def _select_row(self) -> None:
        self.listbox.selection_clear(0, tk.END)
        if self.view:
            self.listbox.selection_set(self.index)
            self.listbox.see(self.index)

    # -- Zuordnen ----------------------------------------------------------

    def assign(self, slot: int, toggle: bool = False, advance: bool = False) -> None:
        if not self.view:
            return
        entry = self.view[self.index]
        new = 0 if (toggle and entry.slot == slot) else slot
        if new == entry.slot:
            return

        self._push_undo({"type": "assign", "key": str(entry.preview),
                         "old": entry.slot, "new": new})
        entry.slot = new
        self._mark_dirty()

        if self.filter_mode.get() != "Alle":
            self.refresh_view()
            # Fiel das Bild aus der gefilterten Ansicht heraus, steht der
            # Index schon auf dem naechsten - weiterschalten wuerde eins
            # ueberspringen
            if not any(e is entry for e in self.view):
                advance = False
        else:
            self._update_row(self.index)
            self._refresh_slot_panel()

        if advance:
            self.step(1)
        else:
            self.show_current()

    # -- Undo --------------------------------------------------------------

    def _push_undo(self, action: dict) -> None:
        self.undo_stack.append(action)
        # Abgeschlossene Datei-Vorgaenge zuletzt verdraengen - die lassen sich
        # anders kaum zuruecknehmen, Zuordnungen und Regler schon
        while len(self.undo_stack) > UNDO_LIMIT:
            oldest = next((i for i, a in enumerate(self.undo_stack) if a["type"] != "files"), 0)
            del self.undo_stack[oldest]

    def _entry_by_key(self, key: str) -> Entry | None:
        """Eintraege werden beim Neueinlesen neu erzeugt - ueber den Pfad wiederfinden."""
        return next((e for e in self.entries if str(e.preview) == key), None)

    def _entries_by_key(self) -> dict[str, Entry]:
        """Wie _entry_by_key, fuer viele auf einmal."""
        return {str(e.preview): e for e in self.entries}

    def undo(self) -> None:
        if self._busy:
            return
        if not self.undo_stack:
            self.status.config(text="Nichts rückgängig zu machen.")
            return
        action = self.undo_stack.pop()

        if action["type"] == "assign":
            entry = self._entry_by_key(action["key"])
            if entry is None:
                self.status.config(text="Rückgängig nicht möglich - Bild nicht mehr da.")
                return
            entry.slot = action["old"]
            self._mark_dirty()
            self.refresh_view()
            if entry in self.view:
                self.goto(self.view.index(entry))
            self.status.config(text=f"Rückgängig: {entry.stem} → "
                                    f"{action['old'] or 'keine Zuordnung'}")
            return

        if action["type"] in ("develop", "develop_many"):
            items = action.get("items") or [(action["key"], action["old"])]
            restored = 0
            by_key = self._entries_by_key()
            for key, old in items:
                entry = by_key.get(key)
                if entry is not None:
                    self.develop[str(entry.master)] = Develop.from_dict(old)
                    restored += 1
            if not restored:
                self.status.config(text="Rückgängig nicht möglich - Bild nicht mehr da.")
                return
            self._dev_pending = None
            self._mark_dirty()
            self.refresh_view()
            self.status.config(text=f"Rückgängig: Bearbeitung von {restored} Aufnahme(n)")
            return

        # Abgeschlossener Kopier-, Verschiebe- oder Entwicklungs-Vorgang
        ops = action["ops"]
        moved = sum(1 for _, _, kind, _ in ops if kind == "move")
        detail = ("Verschobene Dateien werden zurückgeschoben, "
                  "Kopien und entwickelte JPG gelöscht." if moved
                  else "Die erzeugten Dateien im Ziel werden gelöscht.")
        if not messagebox.askokcancel(
                "Rückgängig", f"{len(ops)} Datei(en) wieder zurücknehmen?\n{detail}"):
            self.undo_stack.append(action)
            return

        failed = changed = blocked = 0
        for src, dest, kind, signature in ops:
            try:
                src, dest = Path(src), Path(dest)
                if not dest.exists():
                    continue
                # Nur zurücknehmen, was noch unverändert unsere Datei ist -
                # sonst löschen wir womöglich etwas, das jemand anders dort
                # inzwischen abgelegt hat. Groesse, Zeitstempel und Datei-ID
                # zusammen; die Groesse allein ist bei RAWs oft gleich.
                if _signature(dest) != signature:
                    changed += 1
                    continue
                if kind == "move":
                    # Liegt am Ursprungsort inzwischen etwas anderes, bleibt
                    # unsere Datei im Ziel - shutil.move wuerde es unter
                    # Windows still ueberschreiben
                    if src.exists():
                        blocked += 1
                        continue
                    src.parent.mkdir(parents=True, exist_ok=True)
                    _, problem = self._transfer(dest, src, move=True)
                    if problem:
                        failed += 1
                else:
                    dest.unlink()
            except Exception:
                failed += 1

        # Nur Ordner entfernen, die dieser Vorgang selbst angelegt hat
        for folder in sorted(action.get("dirs", []), key=lambda p: -len(Path(p).parts)):
            try:
                Path(folder).rmdir()
            except OSError:
                pass

        self.rescan()   # Dateien liegen wieder am Ursprungsort
        by_key = self._entries_by_key()
        for key, slot in action.get("slots", []):
            entry = by_key.get(key)
            if entry is not None:
                entry.slot = slot
        self._save_marks()
        self.refresh_view()
        self.status.config(
            text=f"Rückgängig: {len(ops) - failed - changed - blocked} von {len(ops)} Datei(en)"
                 + (f", {changed} seither verändert und deshalb behalten" if changed else "")
                 + (f", {blocked} im Ziel gelassen, weil am Ursprungsort inzwischen "
                    "eine andere Datei liegt" if blocked else "")
                 + (f", {failed} fehlgeschlagen" if failed else ""))

    # -- Bearbeitung -------------------------------------------------------

    def dev_for(self, entry: Entry) -> Develop:
        return self.develop.setdefault(str(entry.master), Develop())

    def current_entry(self) -> Entry | None:
        return self.view[self.index] if self.view else None

    def toggle_develop(self) -> None:
        self.dev_mode.set(not self.dev_mode.get())
        if self.dev_mode.get():
            # Neben das PanedWindow, nicht hinein: dort stellte dessen
            # Platzverteilung die gesetzte Trennlinie wieder zurueck und das
            # Panel blieb schmal. So bekommt es immer die Breite seines Inhalts.
            self.dev_panel.pack(side=tk.RIGHT, fill=tk.Y, before=self.pane)
            self._sync_dev_panel()
        else:
            self.crop_mode.set(False)
            self.dev_panel.pack_forget()
        self.show_current()

    def toggle_crop(self) -> None:
        if not self.dev_mode.get():
            self.toggle_develop()
        self.crop_mode.set(not self.crop_mode.get())
        entry = self.current_entry()
        if entry is not None:
            dev = self.dev_for(entry)
            if self.crop_mode.get():
                if dev.crop is None:
                    dev.crop = (0.0, 0.0, 1.0, 1.0)
            elif dev.crop == (0.0, 0.0, 1.0, 1.0):
                dev.crop = None    # ganzes Bild ist kein Zuschnitt
        self.show_current()

    def _sync_dev_panel(self) -> None:
        """Regler auf das aktuelle Bild setzen, ohne ein Neurendern auszuloesen."""
        entry = self.current_entry()
        if entry is None or not hasattr(self, "dev_vars"):
            return
        dev = self.dev_for(entry)
        self._suspend_trace = True
        try:
            for key, var in self.dev_vars.items():
                var.set(getattr(dev, key))
        finally:
            self._suspend_trace = False

    def _on_slider(self, key: str, label, fmt: str) -> None:
        value = self.dev_vars[key].get()
        label.config(text=fmt.format(value))
        self._on_dev_change()

    def _reset_slider(self, key: str) -> None:
        self.dev_vars[key].set(getattr(Develop(), key))

    def _on_ratio_change(self) -> None:
        entry = self.current_entry()
        if entry is not None:
            dev = self.dev_for(entry)
            dev.ratio = self.dev_vars["ratio"].get()
            if dev.crop:
                dev.crop = self._constrain(dev.crop, dev.ratio)
        self._on_dev_change()

    def _on_dev_change(self) -> None:
        """Regleraenderung in die Einstellungen uebernehmen und neu zeichnen."""
        if self._suspend_trace:
            return
        entry = self.current_entry()
        if entry is None:
            return
        dev = self.dev_for(entry)
        before = dev.to_dict()
        for key, var in self.dev_vars.items():
            value = var.get()
            current = getattr(dev, key)
            if isinstance(current, int) and not isinstance(current, bool):
                value = int(round(value))
            elif isinstance(current, float):
                value = round(float(value), 2)
            setattr(dev, key, value)
        if dev.to_dict() == before:
            return
        # Ein Reglerzug soll ein Undo-Schritt sein, nicht hundert: solange die
        # Aenderung desselben Bildes noch nicht uebernommen ist, wird gesammelt
        if self._dev_pending != str(entry.preview):
            self._push_undo({"type": "develop", "key": str(entry.preview), "old": before})
            self._dev_pending = str(entry.preview)

        if self._render_job:
            self.after_cancel(self._render_job)
        self._render_job = self.after(180, self._after_dev_change)

    def _after_dev_change(self) -> None:
        self._render_job = None
        self._dev_pending = None
        self._mark_dirty()
        if self.view:
            self._update_row(self.index)
        self.show_current()

    def reset_develop(self) -> None:
        entry = self.current_entry()
        if entry is None:
            return
        self._push_undo({"type": "develop", "key": str(entry.preview),
                         "old": self.dev_for(entry).to_dict()})
        self.develop[str(entry.master)] = Develop()
        self.crop_mode.set(False)
        self._sync_dev_panel()
        self._after_dev_change()

    def reset_crop(self) -> None:
        entry = self.current_entry()
        if entry is None:
            return
        dev = self.dev_for(entry)
        self._push_undo({"type": "develop", "key": str(entry.preview),
                         "old": dev.to_dict()})
        dev.crop = (0.0, 0.0, 1.0, 1.0) if self.crop_mode.get() else None
        self._after_dev_change()

    def copy_develop(self) -> None:
        entry = self.current_entry()
        if entry is None:
            return
        self._dev_clipboard = self.dev_for(entry).copy()
        self.status.config(text=f"Einstellungen von {entry.stem} kopiert.")

    def paste_develop(self) -> None:
        entry = self.current_entry()
        if entry is None or self._dev_clipboard is None:
            return
        target = self.dev_for(entry)
        self._push_undo({"type": "develop", "key": str(entry.preview),
                         "old": target.to_dict()})
        self.develop[str(entry.master)] = self._dev_clipboard.transferred_to(target)
        self._sync_dev_panel()
        self._after_dev_change()

    def apply_develop_to_all(self) -> None:
        """Einstellungen auf alle angezeigten Aufnahmen - der Filter bestimmt, welche."""
        entry = self.current_entry()
        if entry is None:
            return
        targets = list(self.view)
        shown = "" if self.filter_mode.get() == "Alle" else f" (Filter „{self.filter_mode.get()}“)"
        if not messagebox.askokcancel(
                "PhotoSelection",
                f"Einstellungen von {entry.stem} auf alle {len(targets)} angezeigten "
                f"Aufnahmen{shown} übertragen?\n\n"
                "Zuschnitt, Drehung und Geraderichten bleiben bildspezifisch."):
            return
        source = self.dev_for(entry).copy()
        self._push_undo({"type": "develop_many",
                         "items": [(str(t.preview), self.dev_for(t).to_dict())
                                   for t in targets]})
        for target in targets:
            self.develop[str(target.master)] = source.transferred_to(self.dev_for(target))
        self._save_marks()
        self.refresh_view()
        self.status.config(text=f"Einstellungen auf {len(targets)} Aufnahmen übertragen.")

    # -- Anzeige -----------------------------------------------------------

    def show_current(self) -> None:
        self._select_row()
        if not self.view:
            self.canvas.delete("all")
            self._photo = None
            self._update_status()
            return

        entry = self.view[self.index]
        self.loader.request([(self.view[i].preview, self.view[i].preview)
                             for i in (self.index, self.index + 1, self.index - 1,
                                       self.index + 2, self.index - 2)
                             if 0 <= i < len(self.view)])

        dev = self.dev_for(entry)
        prev = self._preview_of(entry)
        full_size = prev.full_size if prev is not None else None
        # Braucht der Zoom mehr Pixel, als die Vorschau hat? Dann wird die volle
        # Aufloesung im Hintergrund geholt und solange die Vorschau vergroessert
        want_full = (self.zoom is not None and not self.crop_mode.get()
                     and prev is not None and prev.image is not None
                     and self.zoom * self._ratio(full_size, prev.image) > 1.05)
        base, extra_ev, gains, note = None, 0.0, None, ""

        if self.dev_mode.get():
            raw = is_raw(entry.master)
            half_key = (str(entry.master), True) + dev.base_key(raw)
            full_key = (str(entry.master), False) + dev.base_key(raw)
            half = self.developer.get(half_key)
            need_full = (self.zoom is not None and not self.crop_mode.get()
                         and isinstance(half, Image.Image)
                         and self.zoom * self._ratio(full_size, half) > 1.05)
            jobs = [(half_key, entry.master, dev.copy(), True)]
            if need_full:
                jobs.append((full_key, entry.master, dev.copy(), False))
            self.developer.request(jobs)
            full = self.developer.get(full_key) if need_full else None
            for result in (full, half):
                if isinstance(result, Image.Image):
                    base = result
                    break
            if isinstance(half, Failed):
                note = "Entwicklung fehlgeschlagen – zeige das Kamera-JPEG"
            elif half is None:
                note = "entwickle…"
            elif need_full and full is None:
                note = "entwickle 100 %…"
            if base is not None:
                extra_ev = pending_exposure(entry.master, dev)

        if base is None:
            if prev is None:
                self.canvas.delete("all")
                self.canvas.create_text(self.canvas.winfo_width() // 2,
                                        self.canvas.winfo_height() // 2,
                                        text="lade…", fill="#888", font=("Segoe UI", 14))
                self._update_status()
                return
            if prev.error or prev.image is None:
                self._render_error(prev.error)
                self._update_status()
                return
            # Notbehelf: das Kamera-JPEG mit genäherter Belichtung und WB
            base = prev.image
            if want_full:
                full = self.full_loader.get(entry.preview)
                if full is None:
                    self.full_loader.request([(entry.preview, entry.preview)])
                    note = note or "lade 100 %…"
                elif isinstance(full, Image.Image):
                    base = full
                else:
                    note = note or "100 % nicht verfügbar – zeige die Vorschau vergrößert"
            if dev.wb == "Kelvin":
                gains = _kelvin_gains(dev.kelvin, dev.tint)
            extra_ev = dev.exposure
        if not want_full:
            self.full_loader.request([])   # nicht mehr gebraucht

        self._render(base, dev, prev, extra_ev, gains, full_size, note)
        self._update_status()

    def _preview_of(self, entry: Entry) -> Preview | None:
        prev = self.loader.get(entry.preview)
        if isinstance(prev, Failed):   # load_preview faengt selbst alles ab - zur Sicherheit
            return Preview(error=prev.message)
        return prev

    @staticmethod
    def _ratio(full_size, img: Image.Image) -> float:
        """Originalpixel je Pixel von img."""
        return max(full_size) / max(img.size) if full_size else 1.0

    def _render_error(self, message: str) -> None:
        self.canvas.delete("all")
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.create_text(cw // 2, ch // 2, fill="#e06c75", font=("Segoe UI", 12),
                                text=f"Bild kann nicht gelesen werden:\n{message}")
        self._photo = None

    def _straightened(self, base: Image.Image, dev: Develop) -> Image.Image:
        """Gedrehtes Bild - zwischengespeichert.

        Drehen kostet bei 3000 px mehrere hundert Millisekunden, und Schwenken
        oder Zuschnitt-Ziehen zeichnet staendig neu.
        """
        key = (dev.orient, round(dev.angle, 2))
        cache = self._geo_cache
        if cache is None or cache[0] is not base or cache[1] != key:
            cache = self._geo_cache = (base, key, straighten(base, dev))
        return cache[2]

    def _render(self, base: Image.Image, dev: Develop, prev: Preview | None,
                extra_ev: float = 0.0, gains=None, full_size=None, note: str = "") -> None:
        self.canvas.delete("all")
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 2 or ch < 2:
            return

        cropping = self.crop_mode.get()
        straight = self._straightened(base, dev)
        self._straight_size = straight.size
        img = straight if cropping else crop_to(straight, dev.crop)

        w, h = img.size
        ratio = self._ratio(full_size, base)
        fit = min(cw / w, ch / h)
        scale = fit if (self.zoom is None or cropping) else self.zoom * ratio
        self._zoom_info = (fit / ratio, w * ratio, h * ratio)
        disp_w = min(cw, max(1, round(w * scale)))
        disp_h = min(ch, max(1, round(h * scale)))
        # Das Runden auf ganze Bildschirmpixel kann den Ausschnitt rechnerisch
        # ueber den Bildrand schieben - deshalb hart begrenzen
        src_w, src_h = min(disp_w / scale, w), min(disp_h / scale, h)
        x0 = min(max(self.center[0] * w - src_w / 2, 0), w - src_w)
        y0 = min(max(self.center[1] * h - src_h / 2, 0), h - src_h)
        resample = Image.Resampling.BILINEAR if scale > 1 else Image.Resampling.LANCZOS
        view = img.resize((disp_w, disp_h), resample,
                          box=(x0, y0, min(x0 + src_w, w), min(y0 + src_h, h)))

        # Farbe erst auf dem Bildschirmausschnitt rechnen, nicht auf dem ganzen Bild
        if gains is not None or extra_ev:
            view = _apply_gains(view, gains, extra_ev)
        view = apply_rgb(view, dev)
        # Histogramm vor den Einblendungen - sonst zaehlen EXIF-Feld und
        # Clipping-Farben mit
        hist = histogram_of(view) if self.show_hist.get() and not cropping else None
        if self.show_clip.get():
            view = mark_clipping(view)

        entry = self.view[self.index]
        if self.show_exif.get() and prev is not None and not cropping:
            draw_exif_panel(view, prev, entry, self.ui_scale)
        if hist is not None:
            draw_histogram(view, hist, self.ui_scale)

        self._photo = ImageTk.PhotoImage(view)
        left, top = (cw - disp_w) // 2, (ch - disp_h) // 2
        self._img_box = (left, top, disp_w, disp_h)
        self.canvas.create_image(left, top, image=self._photo, anchor="nw")

        if cropping:
            self._draw_crop_overlay(dev)
        elif entry.slot:
            self._draw_slot_badge(entry, cw, ch)

        if note:
            self.canvas.create_text(cw // 2, ch - self.px(22), text=note, fill="#f2e14c",
                                    font=("Segoe UI", 11, "bold"))
        if self.zoom is not None and not cropping:
            self.canvas.create_text(self.px(14), ch - self.px(14), anchor="sw",
                                    text=f"{self.zoom * 100:.0f} %"
                                         + ("  ·  Zoom gehalten" if self.keep_zoom.get() else ""),
                                    fill="#dddddd", font=("Segoe UI", 10, "bold"))

    def _draw_slot_badge(self, entry: Entry, cw: int, ch: int) -> None:
        color = SLOT_COLORS[entry.slot]
        self.canvas.create_rectangle(3, 3, cw - 3, ch - 3, outline=color, width=self.px(5))
        title = self.slots[entry.slot].title
        text = f" {entry.slot} {title} " if title else f" {entry.slot} "
        label = self.canvas.create_text(cw - self.px(14), self.px(26), text=text, anchor="e",
                                        fill="#111", font=("Segoe UI", 11, "bold"))
        x0, y0, x1, y1 = self.canvas.bbox(label)
        pad = self.px(2)
        box = self.canvas.create_rectangle(x0 - pad, y0 - pad, x1 + pad, y1 + pad,
                                           fill=color, outline=color)
        self.canvas.tag_lower(box, label)

    def _draw_crop_overlay(self, dev: Develop) -> None:
        if not self._img_box:
            return
        left, top, dw, dh = self._img_box
        x0, y0, x1, y1 = dev.crop or (0.0, 0.0, 1.0, 1.0)
        cx0, cy0 = left + x0 * dw, top + y0 * dh
        cx1, cy1 = left + x1 * dw, top + y1 * dh

        shade = dict(fill="#000", outline="", stipple="gray50")
        self.canvas.create_rectangle(left, top, left + dw, cy0, **shade)
        self.canvas.create_rectangle(left, cy1, left + dw, top + dh, **shade)
        self.canvas.create_rectangle(left, cy0, cx0, cy1, **shade)
        self.canvas.create_rectangle(cx1, cy0, left + dw, cy1, **shade)

        self.canvas.create_rectangle(cx0, cy0, cx1, cy1, outline="#ffffff", width=2)
        for i in (1, 2):   # Drittel-Linien (Tk kennt keine Transparenz, daher gestrichelt)
            self.canvas.create_line(cx0 + (cx1 - cx0) * i / 3, cy0,
                                    cx0 + (cx1 - cx0) * i / 3, cy1,
                                    fill="#cccccc", dash=(3, 4))
            self.canvas.create_line(cx0, cy0 + (cy1 - cy0) * i / 3,
                                    cx1, cy0 + (cy1 - cy0) * i / 3,
                                    fill="#cccccc", dash=(3, 4))
        r = self.px(6)
        for hx, hy in ((cx0, cy0), (cx1, cy0), (cx0, cy1), (cx1, cy1)):
            self.canvas.create_rectangle(hx - r, hy - r, hx + r, hy + r,
                                         fill="#ffffff", outline="#333")

    def _update_status(self) -> None:
        self._refresh_slot_panel()
        if self._busy and self._progress:
            self.status.config(text=self._progress)   # Fortschritt nicht ueberschreiben
            return
        if not self.view:
            text = "Keine Bilder im gewählten Ordner." if self.source else "Quellordner wählen…"
        else:
            entry = self.view[self.index]
            try:
                size = sum(f.stat().st_size for f in entry.files if f.exists()) / 1e6
            except OSError:
                size = 0.0
            prev = self._preview_of(entry)
            sharp = f"Schärfe {prev.sharpness:.0f}" if prev and prev.image else "…"
            target = (self.slots[entry.slot].title or f"Ordner {entry.slot}") \
                if entry.slot else "offen"
            dev = self.dev_for(entry)
            mode = "Entwicklung" if self.dev_mode.get() else "Vorschau"
            edited = " · bearbeitet" if not dev.is_default() else ""
            text = (f"{self.index + 1}/{len(self.view)}   {entry.label}   {size:.1f} MB   "
                    f"→ {target}   {sharp}   {mode}{edited}")
        if self._save_error:
            text += f"   ⚠ {self._save_error}"
        self.status.config(text=text)

    def _poll(self) -> None:
        refresh = False
        current = self.view[self.index].preview if self.view else None
        for worker in (self.loader, self.full_loader):
            try:
                while True:
                    if worker.done.get_nowait() == current:
                        refresh = True
            except queue.Empty:
                pass
        try:
            while True:
                self.developer.done.get_nowait()
                if self.dev_mode.get():
                    refresh = True
        except queue.Empty:
            pass
        if refresh:
            self.show_current()

        try:
            while True:
                kind, payload = self.worker_q.get_nowait()
                if kind == "progress":
                    self._progress = payload
                    self.status.config(text=payload)
                elif kind == "done":
                    self._run_done(*payload)
        except queue.Empty:
            pass
        self._poll_job = self.after(40, self._poll)

    # -- Maus, Zoom, Drehen ------------------------------------------------

    def _on_canvas_resize(self, _event) -> None:
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(80, self.show_current)

    def _schedule_render(self) -> None:
        """Beim Ziehen hoechstens alle 15 ms neu zeichnen statt bei jedem Maus-Ereignis."""
        if self._drag_job is None:
            self._drag_job = self.after(15, self._scheduled_render)

    def _scheduled_render(self) -> None:
        self._drag_job = None
        self.show_current()

    def rotate(self, deg: int) -> None:
        entry = self.current_entry()
        if entry is None:
            return
        dev = self.dev_for(entry)
        self._push_undo({"type": "develop", "key": str(entry.preview),
                         "old": dev.to_dict()})
        dev.orient = (dev.orient + deg) % 360
        dev.crop = None    # der alte Ausschnitt passt nicht mehr
        self._after_dev_change()

    def set_zoom(self, value: float | None) -> None:
        """value: Bildschirmpixel je Originalpixel - 1.0 ist echte 100 %."""
        self.zoom = value
        if value is None:
            self.center = (0.5, 0.5)
        self.show_current()

    def _on_wheel(self, event) -> None:
        if self.crop_mode.get() or not self._zoom_info:
            return
        fit = self._zoom_info[0]
        current = fit if self.zoom is None else self.zoom
        new = max(fit, min(current * (1.25 if event.delta > 0 else 0.8), 8.0))
        self.zoom = None if new <= fit * 1.001 else new
        self.show_current()

    def _on_press(self, event) -> None:
        self.canvas.focus_set()
        if self.crop_mode.get():
            self._start_crop(event)
        else:
            self._drag = (event.x, event.y)

    def _on_drag(self, event) -> None:
        if self.crop_mode.get():
            self._update_crop(event)
            return
        if not self._drag or self.zoom is None or not self._zoom_info:
            return
        _, width, height = self._zoom_info
        dx, dy = event.x - self._drag[0], event.y - self._drag[1]
        self._drag = (event.x, event.y)
        self.center = (
            min(max(self.center[0] - dx / (self.zoom * width), 0.0), 1.0),
            min(max(self.center[1] - dy / (self.zoom * height), 0.0), 1.0),
        )
        self._schedule_render()

    def _on_release(self, _event) -> None:
        self._drag = None
        if self._crop_drag is None:
            return
        kind, _, start = self._crop_drag
        self._crop_drag = None
        entry = self.current_entry()
        if entry is None:
            return
        dev = self.dev_for(entry)
        x0, y0, x1, y1 = dev.crop or (0.0, 0.0, 1.0, 1.0)
        if kind == "new" and (x1 - x0 < 0.01 or y1 - y0 < 0.01):
            dev.crop = start   # nur geklickt, nichts aufgezogen
        # Ein Undo-Schritt nur, wenn sich wirklich etwas geaendert hat
        if self._crop_before is not None and dev.to_dict() != self._crop_before:
            self._push_undo({"type": "develop", "key": str(entry.preview),
                             "old": self._crop_before})
        self._crop_before = None
        self._after_dev_change()

    # -- Zuschnitt mit der Maus -------------------------------------------

    def _canvas_to_rel(self, x: int, y: int) -> tuple[float, float]:
        left, top, dw, dh = self._img_box
        return (min(max((x - left) / dw, 0.0), 1.0),
                min(max((y - top) / dh, 0.0), 1.0))

    def _start_crop(self, event) -> None:
        entry = self.current_entry()
        if entry is None or not self._img_box:
            return
        dev = self.dev_for(entry)
        self._crop_before = dev.to_dict()
        rect = dev.crop or (0.0, 0.0, 1.0, 1.0)
        left, top, dw, dh = self._img_box
        rx, ry = self._canvas_to_rel(event.x, event.y)

        grab = self.px(10)
        corners = {"nw": (rect[0], rect[1]), "ne": (rect[2], rect[1]),
                   "sw": (rect[0], rect[3]), "se": (rect[2], rect[3])}
        for name, (cx, cy) in corners.items():
            if abs(cx * dw + left - event.x) <= grab and abs(cy * dh + top - event.y) <= grab:
                self._crop_drag = ("corner", name, rect)
                return
        if rect[0] < rx < rect[2] and rect[1] < ry < rect[3]:
            self._crop_drag = ("move", (rx, ry), rect)
            return
        self._crop_drag = ("new", (rx, ry), rect)
        dev.crop = (rx, ry, rx, ry)

    def _update_crop(self, event) -> None:
        entry = self.current_entry()
        if entry is None or self._crop_drag is None or not self._img_box:
            return
        dev = self.dev_for(entry)
        kind, anchor, start = self._crop_drag
        rx, ry = self._canvas_to_rel(event.x, event.y)
        fixed = None    # Ecke, die beim Erzwingen des Seitenverhaeltnisses stehen bleibt

        if kind == "new":
            ax, ay = anchor
            rect = (min(ax, rx), min(ay, ry), max(ax, rx), max(ay, ry))
            fixed = ("s" if ry < ay else "n") + ("e" if rx < ax else "w")
        elif kind == "corner":
            x0, y0, x1, y1 = start
            if anchor == "nw":
                rect = (min(rx, x1 - 0.02), min(ry, y1 - 0.02), x1, y1)
            elif anchor == "ne":
                rect = (x0, min(ry, y1 - 0.02), max(rx, x0 + 0.02), y1)
            elif anchor == "sw":
                rect = (min(rx, x1 - 0.02), y0, x1, max(ry, y0 + 0.02))
            else:
                rect = (x0, y0, max(rx, x0 + 0.02), max(ry, y0 + 0.02))
            fixed = {"nw": "se", "ne": "sw", "sw": "ne", "se": "nw"}[anchor]
        else:
            ax, ay = anchor
            dx, dy = rx - ax, ry - ay
            w, h = start[2] - start[0], start[3] - start[1]
            nx = min(max(start[0] + dx, 0.0), 1.0 - w)
            ny = min(max(start[1] + dy, 0.0), 1.0 - h)
            rect = (nx, ny, nx + w, ny + h)

        rect = tuple(min(max(v, 0.0), 1.0) for v in rect)
        dev.crop = self._constrain(rect, dev.ratio, fixed) if fixed else rect
        self._schedule_render()

    def _constrain(self, rect, ratio_name: str, fixed: str = "nw"):
        """Seitenverhaeltnis erzwingen - die Ecke `fixed` bleibt stehen.

        Gerechnet wird relativ, also mit den Massen des geraden Bildes
        gewichtet (grobe Drehung und Geraderichten sind darin schon enthalten).
        """
        target = RATIOS.get(ratio_name)
        if not target or not self._straight_size:
            return rect
        iw, ih = self._straight_size
        k = iw / (target * ih)          # relative Hoehe je relativer Breite
        x0, y0, x1, y1 = rect
        east, south = fixed[1] == "e", fixed[0] == "s"
        fx, fy = (x1 if east else x0), (y1 if south else y0)
        room_w = fx if east else 1.0 - fx
        room_h = fy if south else 1.0 - fy
        width = max(x1 - x0, (y1 - y0) / k)    # dem groesseren Ausschlag folgen
        width = min(width, room_w, room_h / k)
        height = width * k
        nx0 = fx - width if east else fx
        ny0 = fy - height if south else fy
        return (nx0, ny0, nx0 + width, ny0 + height)

    def toggle_fullscreen(self) -> None:
        self.attributes("-fullscreen", not self.attributes("-fullscreen"))

    # -- Export-Einstellungen ---------------------------------------------

    def open_export_dialog(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("Export")
        dlg.transient(self)
        dlg.resizable(False, False)
        body = ttk.Frame(dlg, padding=12)
        body.pack(fill=tk.BOTH, expand=True)

        develop = tk.BooleanVar(value=self.export_cfg["develop"])
        mode = tk.StringVar(value=self.export_cfg["mode"])
        quality = tk.IntVar(value=self.export_cfg["quality"])
        strip_private = tk.BooleanVar(value=self.export_cfg["strip_gps"])
        edge = tk.StringVar(value=next((k for k, v in LONG_EDGES.items()
                                        if v == self.export_cfg["long_edge"]), "Original"))

        ttk.Checkbutton(body, text="RAW und bearbeitete Aufnahmen als JPG entwickeln",
                        variable=develop).pack(anchor="w")
        ttk.Radiobutton(body, text=f"zusätzlich zur RAW-Datei (Unterordner „{DEV_SUBDIR}“)",
                        variable=mode, value="zusaetzlich").pack(anchor="w", padx=20)
        ttk.Radiobutton(body, text="statt der RAW-Datei (Originale bleiben in der Quelle)",
                        variable=mode, value="statt").pack(anchor="w", padx=20)

        row = ttk.Frame(body)
        row.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(row, text="JPEG-Qualität", width=14).pack(side=tk.LEFT)
        ttk.Spinbox(row, from_=60, to=100, textvariable=quality, width=6).pack(side=tk.LEFT)
        row = ttk.Frame(body)
        row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row, text="Lange Kante", width=14).pack(side=tk.LEFT)
        ttk.Combobox(row, textvariable=edge, values=list(LONG_EDGES), width=12,
                     state="readonly").pack(side=tk.LEFT)

        ttk.Checkbutton(body, text="Standort (GPS) und Seriennummern aus der Ausgabe entfernen",
                        variable=strip_private).pack(anchor="w", pady=(10, 0))

        ttk.Label(body, foreground="#666", wraplength=self.px(380), justify="left",
                  text="Entwickelt wird mit den Einstellungen der jeweiligen Aufnahme. "
                       "RAWs ohne Bearbeitung werden neutral entwickelt, das dauert je "
                       "RAW rund 4–25 Sekunden. Unbearbeitete JPGs werden nicht neu "
                       "komprimiert, sondern wie gewohnt kopiert.\n\n"
                       "Kamera, Objektiv, Blende, Zeit und ISO werden immer übernommen. "
                       "Aufnahmeort, Seriennummern und Herstellerdaten nur, wenn der "
                       "Haken oben entfernt wird — exportierte JPGs sind das, was man "
                       "weitergibt."
                  ).pack(anchor="w", pady=(6, 0))

        def save():
            try:
                q = min(max(int(quality.get()), 60), 100)
            except (tk.TclError, ValueError):   # Unsinn im Eingabefeld
                q = self.export_cfg["quality"]
            self.export_cfg.update(develop=develop.get(), mode=mode.get(), quality=q,
                                   strip_gps=strip_private.get(),
                                   long_edge=LONG_EDGES[edge.get()])
            self._save_slots()
            self._refresh_slot_panel()
            dlg.destroy()

        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(buttons, text="Übernehmen", command=save).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Abbrechen", command=dlg.destroy).pack(side=tk.RIGHT, padx=6)
        dlg.grab_set()

    # -- Ausführen ---------------------------------------------------------

    @staticmethod
    def _same_dir(a: Path, b: Path) -> bool:
        try:
            return a.resolve() == b.resolve()
        except OSError:
            return a == b

    def run_assignments(self) -> None:
        if self._busy:   # der Knopf heisst waehrenddessen "Abbrechen"
            self._cancel.set()
            self._progress = "Breche nach der aktuellen Datei ab …"
            self.status.config(text=self._progress)
            return
        if not self.source:
            return
        self._flush_marks()
        assigned = [(e, self.slots[e.slot]) for e in self.entries if e.slot]
        missing = sorted({e.slot for e, s in assigned if not s.path})
        if missing:
            messagebox.showwarning("PhotoSelection",
                                   "Für diese Tasten ist kein Zielordner gesetzt: "
                                   + ", ".join(str(i) for i in missing))
            return
        if not assigned:
            messagebox.showinfo("PhotoSelection", "Es ist nichts zugeordnet.")
            return
        if any(self._same_dir(s.path, self.source) for _, s in assigned):
            messagebox.showwarning("PhotoSelection", "Ein Zielordner ist der Quellordner.")
            return

        cfg = dict(self.export_cfg)
        move = self.move_files.get()
        plan: list[Job] = []
        for entry, slot in assigned:
            dev = self.dev_for(entry).copy()
            # Entwickelt werden RAWs und bearbeitete Aufnahmen - ein
            # unbearbeitetes JPG nur neu zu komprimieren kostet bloss Qualitaet
            develop = cfg["develop"] and (entry.raw is not None or not dev.is_default())
            replace = develop and cfg["mode"] == "statt"
            plan.append(Job(key=str(entry.preview), slot=entry.slot, stem=entry.stem,
                            reldir=entry.reldir, target=Path(slot.path), master=entry.master,
                            files=[] if replace else entry.files + entry.sidecars,
                            dev=dev, develop=develop))

        n_files = sum(len(j.files) for j in plan)
        n_dev = sum(j.develop for j in plan)
        lines = [f"  {i}: {sum(1 for j in plan if j.slot == i)} Aufnahme(n) → "
                 f"{self.slots[i].path}" for i in sorted({j.slot for j in plan})]
        text = f"{len(plan)} Aufnahme(n) verarbeiten?\n\n" + "\n".join(lines)
        if n_dev:
            text += f"\n\n{n_dev} JPG entwickeln ("
            text += (f"Unterordner „{DEV_SUBDIR}“)" if cfg["mode"] != "statt"
                     else "statt der Originale)")
            text += f"\nQualität {cfg['quality']}, lange Kante: " + (
                f"{cfg['long_edge']} px" if cfg["long_edge"] else "Original")
        if n_files:
            text += f"\n\n{n_files} Datei(en) {'verschieben' if move else 'kopieren'}"
        if move and any(j.develop and not j.files for j in plan):
            text += "\n\nHinweis: Originale entwickelter Aufnahmen bleiben im Quellordner, " \
                    "da sie durch die entwickelte JPG ersetzt werden."
        if not messagebox.askokcancel("PhotoSelection", text):
            return

        self._cancel.clear()
        self._set_busy(True)
        threading.Thread(target=self._run_worker, daemon=True,
                         args=(plan, move, self.keep_tree.get(), cfg)).start()

    def _set_busy(self, busy: bool) -> None:
        """Waehrend des Ausfuehrens nichts zulassen, was die Grundlage aendert."""
        self._busy = busy
        self._progress = ""
        state = tk.DISABLED if busy else tk.NORMAL
        for widget in (self.btn_source, self.chk_recursive, self.chk_pairs, self.btn_export):
            widget.config(state=state)
        self.cmb_sort.config(state=tk.DISABLED if busy else "readonly")
        self._refresh_slot_panel()

    def _run_worker(self, plan: list[Job], move: bool, keep_tree: bool, cfg: dict) -> None:
        ops: list[tuple[str, str, str, tuple]] = []
        created_dirs: list[str] = []
        done_keys: list[tuple[str, int]] = []
        skipped = 0
        errors: list[str] = []
        cancelled = False
        total = max(1, sum(len(j.files) + j.develop for j in plan))
        done = 0

        try:
            for job in plan:
                if self._cancel.is_set():
                    cancelled = True
                    break
                job_ok = True
                rel_dir = Path(job.reldir) if (keep_tree and job.reldir != ".") else Path()
                folder = job.target / rel_dir
                master = job.master       # wandert beim Verschieben mit
                dev_stem = job.stem

                targets = []
                if job.files:
                    try:
                        self._ensure_dir(folder, job.target, created_dirs)
                        targets = self._plan_names(job.files, folder, job.stem)
                    except Exception as exc:
                        job_ok = False
                        errors.append(f"{job.stem}: {exc}")
                for src, dest, already_there in targets:
                    done += 1
                    if src == job.master:
                        dev_stem = Path(dest.name).stem   # IMG_1_2.jpg zu IMG_1_2.CR2
                    if already_there:
                        skipped += 1
                        continue
                    try:
                        kind, problem = self._transfer(src, dest, move)
                        ops.append((str(src), str(dest), kind, _signature(dest)))
                        if kind == "move" and src == job.master:
                            master = dest
                        if problem:
                            job_ok = False
                            errors.append(f"{src.name}: {problem}")
                    except Exception as exc:
                        job_ok = False
                        errors.append(f"{src.name}: {exc}")
                    self.worker_q.put(("progress",
                                       f"{'Verschiebe' if move else 'Kopiere'} "
                                       f"{done}/{total} …  (Knopf „Abbrechen“ hält an)"))

                if job.develop:
                    if self._cancel.is_set():
                        cancelled = True
                        break    # Dateien sind schon da, Entwicklung fehlt - bleibt zugeordnet
                    done += 1
                    self.worker_q.put(("progress", f"Entwickle {job.stem} ({done}/{total}) …"))
                    try:
                        dev_folder = folder if not job.files else \
                            job.target / DEV_SUBDIR / rel_dir
                        self._ensure_dir(dev_folder, job.target, created_dirs)
                        dest = _free_path(dev_folder / (dev_stem + ".jpg"))
                        export_jpeg(master, job.dev, dest, cfg["quality"],
                                    cfg["long_edge"], cfg["strip_gps"])
                        ops.append((str(master), str(dest), "develop", _signature(dest)))
                    except Exception as exc:
                        job_ok = False
                        errors.append(f"{job.stem}: Entwicklung fehlgeschlagen: {exc}")

                if job_ok:
                    done_keys.append((job.key, job.slot))
        except Exception as exc:   # darf den Abschluss nicht verhindern
            errors.append(f"Abbruch: {type(exc).__name__}: {exc}")
        finally:
            self.worker_q.put(("done", (ops, created_dirs, done_keys, skipped,
                                        errors, move, cancelled)))

    @staticmethod
    def _ensure_dir(folder: Path, root: Path, created: list[str]) -> None:
        """Ordner anlegen und vermerken, welche davon neu sind.

        Nur diese darf ein Rueckgaengig spaeter wieder entfernen - Ordner, die
        es vorher schon gab, gehoeren dem Nutzer.
        """
        missing = []
        probe = folder
        while not probe.exists() and probe != probe.parent:
            missing.append(probe)
            probe = probe.parent
        folder.mkdir(parents=True, exist_ok=True)
        created.extend(str(p) for p in missing if p != root)

    @staticmethod
    def _plan_names(files: list[Path], folder: Path, stem: str) -> list[tuple[Path, Path, bool]]:
        """Zielnamen fuer alle Dateien einer Aufnahme: (Quelle, Ziel, schon da).

        Eine inhaltsgleiche Datei am Ziel gilt als schon kopiert. Alles andere
        wird nie ueberschrieben - dann weicht die ganze Aufnahme gemeinsam auf
        _1, _2 ... aus. RAW, JPG und XMP muessen gleich heissen, sonst findet
        z. B. Lightroom die Bearbeitung nicht mehr.
        """
        for n in range(10000):
            plan = []
            for src in files:
                dest = folder / (src.name if n == 0 else _numbered(src.name, stem, n))
                if not dest.exists():
                    plan.append((src, dest, False))
                elif _same_content(src, dest):
                    plan.append((src, dest, True))
                else:
                    break   # belegt - naechste Nummer fuer alle
            else:
                return plan
        raise FileExistsError(f"kein freier Name für {stem} in {folder}")

    @staticmethod
    def _transfer(src: Path, dest: Path, move: bool) -> tuple[str, str | None]:
        """Kopieren oder verschieben, ohne je zu ueberschreiben.

        Gibt die tatsaechliche Art ("move"/"copy") und ggf. ein Problem zurueck.
        Kopiert wird ueber eine .part-Datei - ein Abbruch hinterlaesst so keine
        halbe Datei unter dem richtigen Namen.
        """
        if dest.exists():
            raise FileExistsError(f"existiert bereits: {dest}")
        if move:
            try:
                os.rename(src, dest)   # gleiches Laufwerk: sofort und vollstaendig
                return "move", None
            except OSError:
                if dest.exists():
                    raise
        part = dest.with_name(f".{dest.name}.part")
        try:
            shutil.copy2(src, part)
        except BaseException:
            try:
                part.unlink()
            except OSError:
                pass
            raise
        _publish(part, dest)
        if not move:
            return "copy", None
        try:
            os.unlink(src)   # erst, wenn die Kopie vollstaendig am Ziel liegt
        except OSError as exc:
            return "copy", f"kopiert, Original aber nicht entfernt: {exc}"
        return "move", None

    def _run_done(self, ops, created_dirs, done_keys, skipped, errors, move, cancelled) -> None:
        self._set_busy(False)
        if ops:
            self._push_undo({"type": "files", "ops": ops, "dirs": created_dirs,
                             "slots": list(done_keys)})
        # Erledigte Zuordnungen zurücksetzen, damit nichts doppelt läuft - aber
        # nur, wenn sie seit dem Start nicht geändert wurden
        by_key = self._entries_by_key()
        for key, slot in done_keys:
            entry = by_key.get(key)
            if entry is not None and entry.slot == slot:
                entry.slot = 0
        self._save_marks()
        if self._close_after_run:
            self._on_close()
            return

        kinds = [k for _, _, k, _ in ops]
        msg = ("Abgebrochen.\n\n" if cancelled else "") + (
            f"{kinds.count('move')} verschoben, {kinds.count('copy')} kopiert, "
            f"{kinds.count('develop')} entwickelt.\n"
            f"{skipped} übersprungen (inhaltsgleich bereits vorhanden).\n\n"
            "Strg+Z macht den Vorgang rückgängig.")
        if errors:
            msg += "\n\nFehler:\n" + "\n".join(errors[:10])
            if len(errors) > 10:
                msg += f"\n… und {len(errors) - 10} weitere"
            msg += ("\n\nDiese Aufnahmen bleiben zugeordnet und werden beim "
                    "nächsten Ausführen erneut versucht.")
            messagebox.showwarning("PhotoSelection", msg)
        else:
            messagebox.showinfo("PhotoSelection", msg)

        if move:
            self.rescan()
        else:
            self.refresh_view()

    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askokcancel(
                    "PhotoSelection",
                    "Die Zuordnungen werden gerade ausgeführt.\n\n"
                    "Nach der aktuellen Datei abbrechen und beenden?"):
                return
            self._cancel.set()
            self._close_after_run = True   # _run_done schliesst dann
            self._progress = "Beende nach der aktuellen Datei …"
            self.status.config(text=self._progress)
            return
        self._save_marks()
        self._save_slots()
        # Anstehende Timer abbestellen, sonst laufen sie ins zerstoerte Fenster
        for job in (self._poll_job, self._resize_job, self._render_job, self._drag_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self.destroy()


# --------------------------------------------------------------------------
# Overlays
# --------------------------------------------------------------------------

@functools.lru_cache(maxsize=16)
def _font(size: int):
    for name in ("segoeui.ttf", "DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def histogram_of(img: Image.Image) -> np.ndarray:
    """RGB-Histogramm - PIL zaehlt in C, deutlich schneller als numpy je Kanal."""
    return np.asarray(img.convert("RGB").histogram(), dtype=np.int64).reshape(3, 256)


def mark_clipping(view: Image.Image) -> Image.Image:
    """Ausgefressene Lichter rot, abgesoffene Schatten blau einfaerben."""
    arr = np.array(view.convert("RGB"))
    peak = arr.max(axis=2)
    arr[peak >= HI_CLIP] = (255, 40, 40)
    arr[peak <= LO_CLIP] = (40, 90, 255)
    return Image.fromarray(arr)


def draw_exif_panel(view: Image.Image, prev: Preview, entry: Entry, scale: float = 1.0) -> None:
    """Aufnahmedaten und Schaerfe-Score halbtransparent oben links."""
    exif = prev.exif
    lines = [entry.stem]
    if exif.get("camera"):
        lines.append(exif["camera"])
    if exif.get("lens"):
        lines.append(exif["lens"])
    shot = "  ".join(x for x in (exif.get("focal"), exif.get("aperture"),
                                 exif.get("shutter"),
                                 f"ISO {exif['iso']}" if exif.get("iso") else "") if x)
    if shot:
        lines.append(shot)
    if exif.get("date"):
        lines.append(exif["date"])

    def s(value: float) -> int:   # Masse fuer 96 dpi, mitwachsend
        return round(value * scale)

    font, bold = _font(s(14)), _font(s(15))
    pad, line_h = s(10), s(19)
    width = max([int(bold.getlength(lines[0]))]
                + [int(font.getlength(t)) for t in lines[1:]])
    width = min(max(width, s(190)), max(view.width - s(24), s(190))) + 2 * pad
    height = line_h * len(lines) + s(26) + 2 * pad

    panel = Image.new("RGBA", (width, height), (0, 0, 0, 150))
    draw = ImageDraw.Draw(panel)
    y = pad
    draw.text((pad, y), lines[0], font=bold, fill=(255, 255, 255, 255))
    y += line_h + s(2)
    for text in lines[1:]:
        draw.text((pad, y), text, font=font, fill=(225, 225, 225, 255))
        y += line_h

    # Schaerfe als Balken - log-skaliert, 500 gilt als knackig
    score = prev.sharpness
    frac = min(np.log10(score + 1) / np.log10(501), 1.0)
    colour = (61, 220, 132, 255) if frac > 0.75 else              (242, 225, 76, 255) if frac > 0.5 else (224, 108, 117, 255)
    draw.text((pad, y + s(2)), f"Schärfe {score:.0f}", font=font, fill=(200, 200, 200, 255))
    bar_x = pad + s(96)
    draw.rectangle([bar_x, y + s(8), width - pad, y + s(16)], fill=(255, 255, 255, 40))
    draw.rectangle([bar_x, y + s(8), bar_x + (width - pad - bar_x) * frac, y + s(16)],
                   fill=colour)

    view.paste(panel, (s(12), s(12)), panel)


def draw_histogram(view: Image.Image, hist: np.ndarray, scale: float = 1.0) -> None:
    """RGB-Histogramm unten rechts, gefuellt damit es auch auf hellen Bildern liest."""
    def s(value: float) -> int:
        return round(value * scale)

    w, h = min(s(272), max(view.width - s(24), s(160))), s(104)
    pad = s(8)
    panel = Image.new("RGBA", (w, h), (0, 0, 0, 195))
    inner_w, inner_h = w - 2 * pad, h - 2 * pad
    peak = max(float(hist.max()), 1.0)

    for channel, (r, g, b) in enumerate([(255, 85, 85), (80, 235, 120), (95, 155, 255)]):
        points = [(pad + inner_w * i / 255,
                   h - pad - inner_h * min(hist[channel, i] / peak, 1.0) ** 0.5)
                  for i in range(256)]
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        pen = ImageDraw.Draw(layer)
        pen.polygon(points + [(w - pad, h - pad), (pad, h - pad)], fill=(r, g, b, 70))
        pen.line(points, fill=(r, g, b, 235), width=max(1, s(2)))
        panel = Image.alpha_composite(panel, layer)

    ImageDraw.Draw(panel).rectangle([pad, pad, w - pad, h - pad],
                                    outline=(255, 255, 255, 60))
    view.paste(panel, (view.width - w - s(12), view.height - h - s(12)), panel)


def _enable_dpi_awareness() -> None:
    """Unter Windows selbst skalieren statt vom System hochgerechnet zu werden.

    Sonst vergroessert Windows bei 125/150 % das fertige Fenster - Schrift
    und vor allem die Vorschau werden unscharf, was gerade beim Beurteilen
    der Schaerfe stoert. Muss vor dem ersten Fenster passieren.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # systemweit DPI-bewusst
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()     # vor Windows 8.1
        except (AttributeError, OSError):
            pass


def main() -> None:
    if rawpy is None:
        print("Hinweis: rawpy fehlt - RAW-Dateien werden nicht angezeigt.")
    if exifread is None:
        print("Hinweis: exifread fehlt - EXIF nur aus dem Vorschaubild.")
    _enable_dpi_awareness()
    App().mainloop()


if __name__ == "__main__":
    main()
