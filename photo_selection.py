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

import io
import json
import math
import os
import queue
import shutil
import threading
import tkinter as tk
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageTk

try:
    import rawpy
except ImportError:  # RAW-Unterstuetzung ist optional
    rawpy = None

try:
    import exifread
except ImportError:  # EXIF-Einblendung faellt dann auf das Vorschaubild zurueck
    exifread = None

JPG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}
RAW_EXT = {
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf",
    ".orf", ".rw2", ".pef", ".dng", ".raw", ".rwl", ".srw", ".3fr", ".iiq",
    ".mos", ".mrw", ".x3f",
}
ALL_EXT = JPG_EXT | RAW_EXT

MARKS_FILE = ".photoselection.json"                        # pro Quellordner
SLOTS_FILE = Path.home() / ".photoselection_slots.json"    # global
DEV_SUBDIR = "entwickelt"      # Unterordner fuer "zusaetzlich zur RAW-Datei"

CACHE_SIZE = 8                 # schnelle Vorschauen im Speicher
DEV_CACHE_SIZE = 3             # entwickelte Bilder im Speicher (gross!)
MAX_DECODE_PX = 3000    # harte Obergrenze der gecachten Vorschau
DRAFT_PX = 2560         # was von libjpeg angefordert wird, siehe _draft_size()
ANALYSIS_PX = 900
UNDO_LIMIT = 200

SLOT_COLORS = {
    1: "#3ddc84", 2: "#4ea1ff", 3: "#ffb454", 4: "#ff6b8b", 5: "#b48ead",
    6: "#f2e14c", 7: "#4dd0e1", 8: "#e06c75", 9: "#9aa0a6",
}
HI_CLIP, LO_CLIP = 250, 6
LUMA = np.float32([0.299, 0.587, 0.114])

RATIOS = {"frei": None, "3:2": 3 / 2, "2:3": 2 / 3, "4:3": 4 / 3, "3:4": 3 / 4,
          "1:1": 1.0, "16:9": 16 / 9}
DENOISE = {"aus": 0, "leicht": 1, "stark": 2}   # rawpy.FBDDNoiseReductionMode
LONG_EDGES = OrderedDict([("Original", 0), ("4096 px", 4096), ("3000 px", 3000),
                          ("2048 px", 2048), ("1600 px", 1600), ("1200 px", 1200)])


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
    except (TypeError, ValueError):
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

    def base_key(self) -> tuple:
        """Parameter, die eine erneute RAW-Entwicklung erfordern.

        Bei eingeschalteter Auto-Helligkeit wird die Belichtung erst nach der
        Entwicklung angewendet - sie taucht deshalb hier nicht auf und der
        Regler wirkt ohne Wartezeit.
        """
        return (0.0 if self.auto_bright else round(self.exposure, 2),
                self.wb, self.kelvin, self.tint, self.auto_bright, self.denoise)

    def is_default(self) -> bool:
        return self == Develop()

    def copy(self) -> "Develop":
        return Develop(**asdict(self))

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
            except (TypeError, ValueError, KeyError):
                continue   # Standardwert behalten
        return dev


@dataclass
class Preview:
    """Ergebnis des schnellen Hintergrund-Ladens."""

    image: Image.Image | None = None
    exif: dict = field(default_factory=dict)
    sharpness: float = 0.0
    hist: np.ndarray | None = None
    error: str = ""


# --------------------------------------------------------------------------
# Schnelle Vorschau (eingebettetes JPEG)
# --------------------------------------------------------------------------

def load_preview(path: Path) -> Preview:
    try:
        if path.suffix.lower() in RAW_EXT and rawpy is not None:
            img, thumb_exif = _load_raw_thumb(path)
        else:
            img = Image.open(path)
            target = _draft_size(img.size, DRAFT_PX)
            if target:
                img.draft("RGB", target)
            img.load()
            img = ImageOps.exif_transpose(img)
            thumb_exif = _exif_from_pil(img)

        if img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > MAX_DECODE_PX:
            img.thumbnail((MAX_DECODE_PX, MAX_DECODE_PX), Image.Resampling.LANCZOS)

        sharp, hist = analyse(img)
        return Preview(image=img, exif=read_exif(path) or thumb_exif,
                       sharpness=sharp, hist=hist)
    except Exception as exc:
        return Preview(error=f"{type(exc).__name__}: {exc}")


def _load_raw_thumb(path: Path) -> tuple[Image.Image, dict]:
    with rawpy.imread(str(path)) as raw:
        flip = getattr(raw.sizes, "flip", 0)
        try:
            thumb = raw.extract_thumb()
        except Exception:
            thumb = None

        if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
            img = Image.open(io.BytesIO(thumb.data))
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

        if thumb is not None and thumb.format == rawpy.ThumbFormat.BITMAP:
            return _apply_flip(Image.fromarray(thumb.data), flip), {}

        return Image.fromarray(raw.postprocess(half_size=True, use_camera_wb=True)), {}


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


def analyse(img: Image.Image) -> tuple[float, np.ndarray]:
    """Schaerfe-Score (Laplace-Varianz der Bildmitte) und RGB-Histogramm."""
    factor = max(1, int(max(img.size) / ANALYSIS_PX))
    small = img.reduce(factor) if factor > 1 else img   # schnelle Kastenfilterung
    # PIL zaehlt das Histogramm in C - deutlich schneller als numpy je Kanal
    hist = np.asarray(small.histogram()[:768], dtype=np.int64).reshape(3, 256)

    grey = np.asarray(small, dtype=np.float32) @ LUMA
    h, w = grey.shape
    # Nur die mittleren 60 % bewerten - Bokeh am Rand ist ja gewollt
    grey = grey[int(h * 0.2):int(h * 0.8), int(w * 0.2):int(w * 0.8)]
    if grey.size < 64:
        return 0.0, hist
    lap = (4 * grey[1:-1, 1:-1] - grey[:-2, 1:-1] - grey[2:, 1:-1]
           - grey[1:-1, :-2] - grey[1:-1, 2:])
    return float(lap.var()), hist


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
    """
    return dev.exposure if (is_raw(path) and dev.auto_bright) else 0.0


def develop_base(path: Path, dev: Develop, half: bool = True) -> Image.Image:
    """Basis-Bild: RAW entwickeln bzw. JPG laden, inklusive Weissabgleich."""
    if is_raw(path):
        return _develop_raw(path, dev, half)

    img = Image.open(path)
    img.load()
    img = ImageOps.exif_transpose(img).convert("RGB")
    gains = _kelvin_gains(dev.kelvin, dev.tint) if dev.wb == "Kelvin" else None
    if dev.wb == "Auto":
        arr = np.asarray(img, np.float32)
        mean = arr.reshape(-1, 3).mean(axis=0)
        gains = np.float32(mean.mean() / np.maximum(mean, 1e-3))
    if dev.exposure or gains is not None:
        img = _apply_gains(img, gains, dev.exposure)
    return img


def _raw_wb_reference(raw) -> np.ndarray | None:
    """Weissabgleich der Kamera als R,G,B - None, wenn unbrauchbar."""
    for attr in ("camera_whitebalance", "daylight_whitebalance"):
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


def _apply_gains(img: Image.Image, gains, exposure: float) -> Image.Image:
    """Belichtung und Farbfaktoren in linearem Licht anwenden."""
    arr = np.asarray(img, np.float32) / 255.0
    lin = np.power(arr, 2.2)
    if exposure:
        lin *= 2.0 ** exposure
    if gains is not None:
        lin *= np.asarray(gains, np.float32)
    return Image.fromarray(
        (np.power(np.clip(lin, 0.0, 1.0), 1 / 2.2) * 255 + 0.5).astype(np.uint8))


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


def apply_geometry(img: Image.Image, dev: Develop) -> Image.Image:
    img = straighten(img, dev)
    if dev.crop:
        x0, y0, x1, y1 = dev.crop
        box = (round(x0 * img.width), round(y0 * img.height),
               round(x1 * img.width), round(y1 * img.height))
        if box[2] - box[0] >= 16 and box[3] - box[1] >= 16:
            img = img.crop(box)
    return img


def apply_rgb(img: Image.Image, dev: Develop, extra_ev: float = 0.0) -> Image.Image:
    """Belichtungsrest, Tonwerte, Kontrast, Saettigung, Schaerfen."""
    if extra_ev:
        img = _apply_gains(img, None, extra_ev)
    if any((dev.highlights, dev.shadows, dev.contrast, dev.saturation)):
        arr = np.asarray(img, np.float32) / 255.0
        if dev.highlights or dev.shadows:
            lum = arr @ LUMA
            gain = np.ones_like(lum)
            if dev.shadows:
                gain += (dev.shadows / 100.0) * 0.9 * (1.0 - lum) ** 2
            if dev.highlights:
                gain += (dev.highlights / 100.0) * 0.9 * lum ** 2
            arr = np.clip(arr * gain[..., None], 0.0, 1.0)
        if dev.contrast:
            arr = np.clip((arr - 0.5) * (1 + dev.contrast / 100.0) + 0.5, 0.0, 1.0)
        if dev.saturation:
            lum = (arr @ LUMA)[..., None]
            arr = np.clip(lum + (arr - lum) * (1 + dev.saturation / 100.0), 0.0, 1.0)
        img = Image.fromarray((arr * 255 + 0.5).astype(np.uint8))
    if dev.sharpen:
        img = img.filter(ImageFilter.UnsharpMask(radius=1.2,
                                                 percent=int(dev.sharpen * 2),
                                                 threshold=3))
    return img


def export_jpeg(path: Path, dev: Develop, dest: Path, quality: int,
                long_edge: int, strip_gps: bool = True) -> None:
    """Volle Entwicklung in eine JPG-Datei schreiben."""
    img = apply_geometry(develop_base(path, dev, half=False), dev)
    if long_edge and max(img.size) > long_edge:
        img.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    img = apply_rgb(img, dev, pending_exposure(path, dev))
    kwargs = dict(quality=quality, optimize=True,
                  subsampling=0 if quality >= 90 else 2)
    exif = _exif_bytes(path, strip_gps)
    if exif:
        kwargs["exif"] = exif
    img.save(dest, "JPEG", **kwargs)


def _exif_bytes(path: Path, strip_gps: bool = True) -> bytes | None:
    """EXIF fuer die Ausgabedatei - mit neutraler Orientierung.

    Die Aufnahmedaten sollen erhalten bleiben, der Aufnahmeort aber nur, wenn
    er ausdruecklich gewuenscht ist: exportierte JPGs sind das, was man
    weitergibt, und GPS-Koordinaten wandern sonst unbemerkt mit.
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
        for tag in (0x0100, 0x0101, 0xA002, 0xA003):   # alte Bildmasse entfernen
            exif.pop(tag, None)
        if strip_gps:
            exif.pop(0x8825, None)
            exif.get_ifd(0x8825).clear()
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
    if not secs:
        return ""
    return f"1/{round(1 / secs)} s" if secs < 1 else f"{secs:g} s"


def _fmt_focal(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{f:g} mm" if f else ""


# --------------------------------------------------------------------------
# Hintergrund-Threads
# --------------------------------------------------------------------------

class Loader(threading.Thread):
    """Schnelle Vorschauen."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.requests: queue.LifoQueue = queue.LifoQueue()
        self.done: queue.Queue = queue.Queue()
        self.cache: OrderedDict[Path, Preview] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, path: Path) -> Preview | None:
        with self.lock:
            prev = self.cache.get(path)
            if prev is not None:
                self.cache.move_to_end(path)
            return prev

    def request(self, paths: list[Path]) -> None:
        for path in reversed(paths):  # LIFO -> das erste Element zuletzt einreihen
            if self.get(path) is None:
                self.requests.put(path)

    def run(self) -> None:
        while True:
            path = self.requests.get()
            if self.get(path) is not None:
                continue
            prev = load_preview(path)
            with self.lock:
                self.cache[path] = prev
                while len(self.cache) > CACHE_SIZE:
                    self.cache.popitem(last=False)
            self.done.put(path)


class Developer(threading.Thread):
    """Echte RAW-Entwicklung fuer den Bearbeitungsmodus - langsam, daher extra."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.requests: queue.LifoQueue = queue.LifoQueue()
        self.done: queue.Queue = queue.Queue()
        self.cache: OrderedDict[tuple, Image.Image] = OrderedDict()
        self.lock = threading.Lock()
        self.wanted: tuple | None = None

    def get(self, key: tuple) -> Image.Image | None:
        with self.lock:
            img = self.cache.get(key)
            if img is not None:
                self.cache.move_to_end(key)
            return img

    def request(self, path: Path, dev: Develop) -> tuple:
        key = (str(path),) + dev.base_key()
        with self.lock:
            self.wanted = key
        if self.get(key) is None:
            self.requests.put((key, path, dev.copy()))
        return key

    def run(self) -> None:
        while True:
            key, path, dev = self.requests.get()
            with self.lock:
                stale = self.wanted != key
            if stale or self.get(key) is not None:
                continue   # Regler wurde inzwischen weitergezogen
            try:
                img = develop_base(path, dev, half=True)
            except Exception:
                img = None
            if img is not None:
                with self.lock:
                    self.cache[key] = img
                    while len(self.cache) > DEV_CACHE_SIZE:
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
        return f"{self.stem}  [{exts}]"


@dataclass
class Slot:
    name: str = ""
    path: Path | None = None

    @property
    def title(self) -> str:
        return self.name or (self.path.name if self.path else "")


# --------------------------------------------------------------------------
# Anwendung
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PhotoSelection")
        self.geometry("1500x900")
        self.minsize(980, 600)

        self.source: Path | None = None
        self.slots: dict[int, Slot] = {i: Slot() for i in range(1, 10)}
        self.entries: list[Entry] = []
        self.view: list[Entry] = []
        self.index = 0
        self.undo_stack: list[dict] = []
        self.develop: dict[str, Develop] = {}      # Schluessel: Pfad der Master-Datei
        self._dev_clipboard: Develop | None = None

        self.loader = Loader()
        self.loader.start()
        self.developer = Developer()
        self.developer.start()
        # Tk darf nur aus dem Hauptthread bedient werden - Arbeits-Threads
        # melden sich ueber diese Queue, abgeholt wird in _poll()
        self.worker_q: queue.Queue = queue.Queue()

        self.zoom: float | None = None
        self.center = (0.5, 0.5)
        self._drag = None
        self._crop_drag = None
        self._photo = None
        self._img_box = None          # (left, top, breite, hoehe) auf der Leinwand
        self._resize_job = None
        self._render_job = None
        self._poll_job = None
        self._dev_pending = None
        self._busy = False
        self._loading_dev = False
        self._suspend_trace = False

        self.recursive = tk.BooleanVar(value=False)
        self.group_pairs = tk.BooleanVar(value=True)
        self.keep_tree = tk.BooleanVar(value=False)
        self.move_files = tk.BooleanVar(value=False)
        self.filter_mode = tk.StringVar(value="Alle")
        self.show_exif = tk.BooleanVar(value=True)
        self.show_hist = tk.BooleanVar(value=False)
        self.show_clip = tk.BooleanVar(value=False)
        self.dev_mode = tk.BooleanVar(value=False)
        self.crop_mode = tk.BooleanVar(value=False)

        self.export_cfg = {"develop": False, "mode": "zusaetzlich",
                           "quality": 92, "long_edge": 0, "strip_gps": True}

        self._load_slots()
        self._build_ui()
        self._bind_keys()
        self._poll_job = self.after(40, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- Aufbau ------------------------------------------------------------

    def _build_ui(self) -> None:
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(bar, text="Quellordner…", command=self.choose_source).pack(side=tk.LEFT)
        self.lbl_source = ttk.Label(bar, text="- kein Ordner -", width=40, anchor="w")
        self.lbl_source.pack(side=tk.LEFT, padx=(6, 12))
        ttk.Checkbutton(bar, text="Unterordner", variable=self.recursive,
                        command=self.rescan).pack(side=tk.LEFT)
        ttk.Checkbutton(bar, text="RAW+JPG als Paar", variable=self.group_pairs,
                        command=self.rescan).pack(side=tk.LEFT, padx=(10, 12))
        ttk.Label(bar, text="Anzeigen:").pack(side=tk.LEFT)
        self.cmb_filter = ttk.Combobox(bar, textvariable=self.filter_mode, width=15,
                                       state="readonly",
                                       values=["Alle", "Nur zugeordnete", "Nur offene"]
                                              + [f"Nur Ordner {i}" for i in range(1, 10)])
        self.cmb_filter.pack(side=tk.LEFT, padx=6)
        self.cmb_filter.bind("<<ComboboxSelected>>", lambda e: self.refresh_view())

        self.btn_run = ttk.Button(bar, text="Zuordnungen ausführen",
                                  command=self.run_assignments)
        self.btn_run.pack(side=tk.RIGHT)
        ttk.Button(bar, text="Export…", command=self.open_export_dialog).pack(
            side=tk.RIGHT, padx=6)
        ttk.Checkbutton(bar, text="Struktur erhalten",
                        variable=self.keep_tree).pack(side=tk.RIGHT, padx=8)
        ttk.Checkbutton(bar, text="Verschieben",
                        variable=self.move_files).pack(side=tk.RIGHT)

        self.pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

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

        self.dev_panel = self._build_dev_panel(self.pane)
        self.after(120, lambda: self.pane.sashpos(0, 380))

        self.status = ttk.Label(self, anchor="w", padding=(8, 4), relief=tk.SUNKEN,
                                text="Quellordner wählen…")
        self.status.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(self, anchor="w", padding=(8, 2), foreground="#666",
                  text="1–9 zuordnen · Leertaste/Enter = Ordner 1 + weiter · "
                       "0/Entf löschen · ←/→ blättern · Strg+Z rückgängig · "
                       "E Bearbeiten · K Zuschnitt · I EXIF · H Histogramm · "
                       "J Clipping · Z 100 % · R drehen · F Vollbild"
                  ).pack(side=tk.BOTTOM, fill=tk.X)

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
            ttk.Entry(box, textvariable=name_var, width=11).grid(row=i, column=1,
                                                                 padx=(0, 4), pady=1)
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
        cmb.bind("<<ComboboxSelected>>", lambda e: self._on_dev_change())
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
        cmb.bind("<<ComboboxSelected>>", lambda e: self._on_dev_change())

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
        cmb.bind("<<ComboboxSelected>>", lambda e: self._on_ratio_change())
        self._slider(frame, "angle", "Drehung", -45.0, 45.0, "{:+.1f}°")
        crop_row = ttk.Frame(frame)
        crop_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Checkbutton(crop_row, text="Zuschnitt zeichnen (K)", variable=self.crop_mode,
                        command=self.show_current).pack(side=tk.LEFT)
        ttk.Button(crop_row, text="↺", width=3, command=self.reset_crop).pack(side=tk.RIGHT)

        ttk.Label(frame, foreground="#666", wraplength=250, justify="left",
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
        var = tk.DoubleVar(value=0.0)
        self.dev_vars[key] = var
        value_lbl = ttk.Label(row, text=fmt.format(0), width=9, anchor="e")
        value_lbl.pack(side=tk.RIGHT)
        scale = ttk.Scale(row, from_=lo, to=hi, variable=var, orient=tk.HORIZONTAL)
        scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        var.trace_add("write", lambda *_, k=key, l=value_lbl, f=fmt:
                      self._on_slider(k, l, f))
        scale.bind("<Double-Button-1>", lambda e, k=key: self._reset_slider(k))

    def _bind_keys(self) -> None:
        def b(sequence, func):
            # Solange ein Eingabefeld den Fokus hat, gehoeren die Tasten diesem
            def guarded(event, func=func):
                if isinstance(self.focus_get(), (ttk.Entry, tk.Entry, ttk.Combobox)):
                    return None
                func()
                return "break"
            self.bind_all(sequence, guarded)

        b("<Right>", lambda: self.step(1))
        b("<Left>", lambda: self.step(-1))
        b("<Next>", lambda: self.step(10))
        b("<Prior>", lambda: self.step(-10))
        b("<Home>", lambda: self.goto(0))
        b("<End>", lambda: self.goto(len(self.view) - 1))
        b("<space>", lambda: self.assign(1, toggle=True, advance=True))
        b("<Return>", lambda: self.assign(1, toggle=True, advance=True))
        for i in range(1, 10):
            b(f"<KeyPress-{i}>", lambda s=i: self.assign(s, toggle=True))
        b("<KeyPress-0>", lambda: self.assign(0))
        b("<Delete>", lambda: self.assign(0))
        b("<BackSpace>", lambda: self.assign(0))
        b("<Control-z>", self.undo)
        b("<KeyPress-e>", self.toggle_develop)
        b("<KeyPress-k>", self.toggle_crop)
        b("<KeyPress-i>", lambda: self._toggle_overlay(self.show_exif))
        b("<KeyPress-h>", lambda: self._toggle_overlay(self.show_hist))
        b("<KeyPress-j>", lambda: self._toggle_overlay(self.show_clip))
        b("<KeyPress-r>", lambda: self.rotate(90))
        b("<KeyPress-R>", lambda: self.rotate(-90))
        b("<KeyPress-z>", lambda: self.set_zoom(None if self.zoom else 1.0))
        b("<KeyPress-f>", self.toggle_fullscreen)
        b("<F11>", self.toggle_fullscreen)
        b("<Escape>", lambda: self.attributes("-fullscreen", False))

    def _toggle_overlay(self, var: tk.BooleanVar) -> None:
        var.set(not var.get())
        self.show_current()

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
            except (TypeError, ValueError):
                pass

    def _save_slots(self) -> None:
        data = {"slots": {str(i): {"name": s.name,
                                   "path": str(s.path) if s.path else None}
                          for i, s in self.slots.items()},
                "export": self.export_cfg}
        try:
            SLOTS_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
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
        total = sum(counts.values())
        suffix = " + JPG" if self.export_cfg["develop"] else ""
        self.btn_run.config(text=f"Zuordnungen ausführen ({total}){suffix}")

    # -- Quellordner und Scan ---------------------------------------------

    def choose_source(self) -> None:
        path = filedialog.askdirectory(title="Quellordner mit Fotos wählen")
        if not path:
            return
        self.source = Path(path)
        self.lbl_source.config(text=self._short(self.source, 40))
        self.undo_stack.clear()
        self.develop.clear()
        self.rescan()

    @staticmethod
    def _short(path, width: int = 40) -> str:
        text = str(path)
        return text if len(text) <= width else "…" + text[-(width - 1):]

    def rescan(self) -> None:
        if not self.source:
            return
        groups: OrderedDict[tuple, Entry] = OrderedDict()
        for f in self._scan_files(self.source):
            reldir = str(f.parent.relative_to(self.source))
            key = (reldir, f.stem.lower()) if self.group_pairs.get() else (reldir, f.name.lower())
            entry = groups.get(key)
            if entry is None:
                entry = groups[key] = Entry(stem=f.stem, reldir=reldir)
            entry.files.append(f)

        for entry in groups.values():
            entry.files.sort(key=lambda p: (p.suffix.lower() not in JPG_EXT, p.name))

        self.entries = list(groups.values())
        self._load_marks()
        self.index = 0
        self.refresh_view()

    def _scan_files(self, root: Path) -> list[Path]:
        found: list[Path] = []
        if self.recursive.get():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                found += [Path(dirpath) / n for n in sorted(filenames)
                          if Path(n).suffix.lower() in ALL_EXT]
        else:
            found = [p for p in sorted(root.iterdir())
                     if p.is_file() and p.suffix.lower() in ALL_EXT]
        return found

    # -- Speichern ---------------------------------------------------------

    def _load_marks(self) -> None:
        path = self.source / MARKS_FILE if self.source else None
        if not path or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        # Die Datei reist mit dem Ordner mit und kann beschädigt oder
        # präpariert sein - deshalb wird alles daraus geprüft
        assignments = data.get("assignments")
        develop = data.get("develop")
        assignments = assignments if isinstance(assignments, dict) else {}
        develop = develop if isinstance(develop, dict) else {}

        for entry in self.entries:
            for f in entry.files:
                slot = _clean_slot(assignments.get(self._rel(f)))
                if slot:
                    entry.slot = slot
                    break
            stored = develop.get(self._rel(entry.master))
            if stored:
                self.develop[str(entry.master)] = Develop.from_dict(stored)

    def _save_marks(self) -> None:
        if not self.source:
            return
        assignments = {self._rel(f): e.slot
                       for e in self.entries if e.slot for f in e.files}
        develop = {}
        for entry in self.entries:
            dev = self.develop.get(str(entry.master))
            if dev is not None and not dev.is_default():
                develop[self._rel(entry.master)] = dev.to_dict()
        try:
            (self.source / MARKS_FILE).write_text(
                json.dumps({"assignments": assignments, "develop": develop}, indent=1),
                encoding="utf-8")
        except OSError:
            pass  # z. B. schreibgeschützte Speicherkarte

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
                self.listbox.itemconfig(i, foreground=SLOT_COLORS[entry.slot])

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
        self.listbox.itemconfig(index, foreground=SLOT_COLORS.get(entry.slot, "black"))
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
        self._save_marks()

        if self.filter_mode.get() != "Alle":
            self.refresh_view()
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
        del self.undo_stack[:-UNDO_LIMIT]

    def _entry_by_key(self, key: str) -> Entry | None:
        """Eintraege werden beim Neueinlesen neu erzeugt - ueber den Pfad wiederfinden."""
        return next((e for e in self.entries if str(e.preview) == key), None)

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
            self._save_marks()
            self.refresh_view()
            if entry in self.view:
                self.goto(self.view.index(entry))
            self.status.config(text=f"Rückgängig: {entry.stem} → "
                                    f"{action['old'] or 'keine Zuordnung'}")
            return

        if action["type"] in ("develop", "develop_many"):
            items = action.get("items") or [(action["key"], action["old"])]
            restored = 0
            for key, old in items:
                entry = self._entry_by_key(key)
                if entry is not None:
                    self.develop[str(entry.master)] = Develop.from_dict(old)
                    restored += 1
            if not restored:
                self.status.config(text="Rückgängig nicht möglich - Bild nicht mehr da.")
                return
            self._dev_pending = None
            self._save_marks()
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

        failed = changed = 0
        for src, dest, kind, size in ops:
            try:
                dest = Path(dest)
                if not dest.exists():
                    continue
                # Nur zurücknehmen, was noch unverändert unsere Datei ist -
                # sonst löschen wir womöglich etwas, das jemand anders dort
                # inzwischen abgelegt hat
                if dest.stat().st_size != size:
                    changed += 1
                    continue
                if kind == "move":
                    Path(src).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dest), str(src))
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
        for key, slot in action.get("slots", []):
            entry = self._entry_by_key(key)
            if entry is not None:
                entry.slot = slot
        self._save_marks()
        self.refresh_view()
        self.status.config(
            text=f"Rückgängig: {len(ops) - failed - changed} von {len(ops)} Datei(en)"
                 + (f", {changed} seither verändert und deshalb behalten" if changed else "")
                 + (f", {failed} fehlgeschlagen" if failed else ""))

    # -- Bearbeitung -------------------------------------------------------

    def dev_for(self, entry: Entry) -> Develop:
        return self.develop.setdefault(str(entry.master), Develop())

    def current_entry(self) -> Entry | None:
        return self.view[self.index] if self.view else None

    def toggle_develop(self) -> None:
        self.dev_mode.set(not self.dev_mode.get())
        if self.dev_mode.get():
            self.pane.add(self.dev_panel, weight=0)
            self.after(50, lambda: self.pane.sashpos(1, self.pane.winfo_width() - 280))
            self._sync_dev_panel()
        else:
            self.crop_mode.set(False)
            self.pane.forget(self.dev_panel)
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
        self._save_marks()
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
        self._push_undo({"type": "develop", "key": str(entry.preview),
                         "old": self.dev_for(entry).to_dict()})
        new = self._dev_clipboard.copy()
        new.crop = None      # Zuschnitt ist bildspezifisch
        self.develop[str(entry.master)] = new
        self._sync_dev_panel()
        self._after_dev_change()

    def apply_develop_to_all(self) -> None:
        entry = self.current_entry()
        if entry is None:
            return
        targets = [e for e in self.entries if e.slot] or list(self.entries)
        if not messagebox.askokcancel(
                "PhotoSelection",
                f"Einstellungen von {entry.stem} auf {len(targets)} Aufnahme(n) "
                "übertragen?\nDer Zuschnitt wird nicht übertragen."):
            return
        source = self.dev_for(entry)
        self._push_undo({"type": "develop_many",
                         "items": [(str(t.preview), self.dev_for(t).to_dict())
                                   for t in targets]})
        for target in targets:
            new = source.copy()
            new.crop = self.develop.get(str(target.master), Develop()).crop
            self.develop[str(target.master)] = new
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
        self.loader.request([self.view[i].preview
                             for i in (self.index, self.index + 1, self.index - 1,
                                       self.index + 2, self.index - 2)
                             if 0 <= i < len(self.view)])

        dev = self.dev_for(entry)
        base, self._loading_dev = None, False
        extra_ev = 0.0
        if self.dev_mode.get():
            key = self.developer.request(entry.master, dev)
            base = self.developer.get(key)
            self._loading_dev = base is None
            extra_ev = pending_exposure(entry.master, dev)

        prev = self.loader.get(entry.preview)
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
            if dev.wb == "Kelvin":
                base = _apply_gains(base, _kelvin_gains(dev.kelvin, dev.tint), 0.0)
            extra_ev = dev.exposure

        self._render(base, dev, prev, extra_ev)
        self._update_status()

    def _render_error(self, message: str) -> None:
        self.canvas.delete("all")
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.create_text(cw // 2, ch // 2, fill="#e06c75", font=("Segoe UI", 12),
                                text=f"Bild kann nicht gelesen werden:\n{message}")
        self._photo = None

    def _render(self, base: Image.Image, dev: Develop, prev: Preview | None,
                extra_ev: float = 0.0) -> None:
        self.canvas.delete("all")
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 2 or ch < 2:
            return

        cropping = self.crop_mode.get()
        img = straighten(base, dev) if cropping else apply_geometry(base, dev)

        w, h = img.size
        fit = min(cw / w, ch / h)
        scale = fit if (self.zoom is None or cropping) else self.zoom
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

        view = apply_rgb(view, dev, extra_ev)
        if self.show_clip.get():
            view = mark_clipping(view)

        entry = self.view[self.index]
        if self.show_exif.get() and prev is not None and not cropping:
            draw_exif_panel(view, prev, entry)
        if self.show_hist.get() and not cropping:
            hist = histogram_of(view) if self.dev_mode.get() else \
                (prev.hist if prev is not None else None)
            if hist is not None:
                draw_histogram(view, hist)

        self._photo = ImageTk.PhotoImage(view)
        left, top = (cw - disp_w) // 2, (ch - disp_h) // 2
        self._img_box = (left, top, disp_w, disp_h)
        self.canvas.create_image(left, top, image=self._photo, anchor="nw")

        if cropping:
            self._draw_crop_overlay(dev)
        elif entry.slot:
            self._draw_slot_badge(entry, cw, ch)

        if self._loading_dev:
            self.canvas.create_text(cw // 2, ch - 22, text="entwickle…", fill="#f2e14c",
                                    font=("Segoe UI", 11, "bold"))

    def _draw_slot_badge(self, entry: Entry, cw: int, ch: int) -> None:
        color = SLOT_COLORS[entry.slot]
        self.canvas.create_rectangle(3, 3, cw - 3, ch - 3, outline=color, width=5)
        title = self.slots[entry.slot].title
        text = f" {entry.slot} {title} " if title else f" {entry.slot} "
        self.canvas.create_rectangle(cw - 14 - 9 * len(text), 12, cw - 12, 40,
                                     fill=color, outline=color)
        self.canvas.create_text(cw - 14, 26, text=text, anchor="e", fill="#111",
                                font=("Segoe UI", 11, "bold"))

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
        for hx, hy in ((cx0, cy0), (cx1, cy0), (cx0, cy1), (cx1, cy1)):
            self.canvas.create_rectangle(hx - 6, hy - 6, hx + 6, hy + 6,
                                         fill="#ffffff", outline="#333")

    def _update_status(self) -> None:
        self._refresh_slot_panel()
        if not self.view:
            self.status.config(text="Keine Bilder im gewählten Ordner."
                               if self.source else "Quellordner wählen…")
            return
        entry = self.view[self.index]
        size = sum(f.stat().st_size for f in entry.files if f.exists()) / 1e6
        prev = self.loader.get(entry.preview)
        sharp = f"Schärfe {prev.sharpness:.0f}" if prev and prev.image else "…"
        target = (self.slots[entry.slot].title or f"Ordner {entry.slot}") \
            if entry.slot else "offen"
        dev = self.dev_for(entry)
        mode = "Entwicklung" if self.dev_mode.get() else "Vorschau"
        edited = " · bearbeitet" if not dev.is_default() else ""
        self.status.config(
            text=f"{self.index + 1}/{len(self.view)}   {entry.label}   {size:.1f} MB   "
                 f"→ {target}   {sharp}   {mode}{edited}")

    def _poll(self) -> None:
        refresh = False
        try:
            while True:
                path = self.loader.done.get_nowait()
                if self.view and path == self.view[self.index].preview:
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
        self.zoom = value
        if value is None:
            self.center = (0.5, 0.5)
        self.show_current()

    def _display_image_size(self) -> tuple[int, int] | None:
        entry = self.current_entry()
        if entry is None:
            return None
        prev = self.loader.get(entry.preview)
        if prev is None or prev.image is None:
            return None
        return prev.image.size

    def _on_wheel(self, event) -> None:
        if self.crop_mode.get():
            return
        size = self._display_image_size()
        if size is None:
            return
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        fit = min(cw / size[0], ch / size[1])
        current = fit if self.zoom is None else self.zoom
        new = max(fit * 0.9, min(current * (1.25 if event.delta > 0 else 0.8), 8.0))
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
        size = self._display_image_size()
        if not self._drag or self.zoom is None or size is None:
            return
        dx, dy = event.x - self._drag[0], event.y - self._drag[1]
        self._drag = (event.x, event.y)
        self.center = (
            min(max(self.center[0] - dx / (self.zoom * size[0]), 0.0), 1.0),
            min(max(self.center[1] - dy / (self.zoom * size[1]), 0.0), 1.0),
        )
        self.show_current()

    def _on_release(self, _event) -> None:
        self._drag = None
        if self._crop_drag is not None:
            self._crop_drag = None
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
        self._push_undo({"type": "develop", "key": str(entry.preview),
                         "old": dev.to_dict()})
        rect = dev.crop or (0.0, 0.0, 1.0, 1.0)
        left, top, dw, dh = self._img_box
        rx, ry = self._canvas_to_rel(event.x, event.y)

        corners = {"nw": (rect[0], rect[1]), "ne": (rect[2], rect[1]),
                   "sw": (rect[0], rect[3]), "se": (rect[2], rect[3])}
        for name, (cx, cy) in corners.items():
            if abs(cx * dw + left - event.x) <= 10 and abs(cy * dh + top - event.y) <= 10:
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

        if kind == "new":
            ax, ay = anchor
            rect = (min(ax, rx), min(ay, ry), max(ax, rx), max(ay, ry))
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
        else:
            ax, ay = anchor
            dx, dy = rx - ax, ry - ay
            w, h = start[2] - start[0], start[3] - start[1]
            nx = min(max(start[0] + dx, 0.0), 1.0 - w)
            ny = min(max(start[1] + dy, 0.0), 1.0 - h)
            rect = (nx, ny, nx + w, ny + h)

        rect = tuple(min(max(v, 0.0), 1.0) for v in rect)
        dev.crop = self._constrain(rect, dev.ratio)
        self.show_current()

    def _constrain(self, rect, ratio_name: str):
        """Seitenverhaeltnis erzwingen - relativ, also mit den Bildmassen gewichtet."""
        target = RATIOS.get(ratio_name)
        if not target:
            return rect
        # Die Kantenlaengen des angezeigten Bildes - sie beruecksichtigen bereits
        # grobe Drehung und Geraderichten
        size = self._img_box[2:] if self._img_box else self._display_image_size()
        if not size:
            return rect
        iw, ih = size
        x0, y0, x1, y1 = rect
        rel_w = x1 - x0
        rel_h = rel_w * iw / (target * ih)
        if y0 + rel_h > 1.0:
            rel_h = 1.0 - y0
            rel_w = rel_h * target * ih / iw
            x1 = min(x0 + rel_w, 1.0)
        return (x0, y0, x1, min(y0 + rel_h, 1.0))

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
        strip_gps = tk.BooleanVar(value=self.export_cfg["strip_gps"])
        edge = tk.StringVar(value=next((k for k, v in LONG_EDGES.items()
                                        if v == self.export_cfg["long_edge"]), "Original"))

        ttk.Checkbutton(body, text="RAW als JPG entwickeln", variable=develop).pack(anchor="w")
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

        ttk.Checkbutton(body, text="Standortdaten (GPS) aus der Ausgabe entfernen",
                        variable=strip_gps).pack(anchor="w", pady=(10, 0))

        ttk.Label(body, foreground="#666", wraplength=360, justify="left",
                  text="Entwickelt wird mit den Einstellungen der jeweiligen Aufnahme. "
                       "Aufnahmen ohne Bearbeitung werden neutral entwickelt. Das dauert "
                       "je RAW rund 4–25 Sekunden.\n\n"
                       "Kamera, Objektiv, Blende, Zeit und ISO werden immer übernommen. "
                       "Der Aufnahmeort nur, wenn der Haken oben entfernt wird — "
                       "exportierte JPGs sind das, was man weitergibt."
                  ).pack(anchor="w", pady=(6, 0))

        def save():
            self.export_cfg.update(develop=develop.get(), mode=mode.get(),
                                   quality=int(quality.get()),
                                   strip_gps=strip_gps.get(),
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

    def run_assignments(self) -> None:
        if self._busy or not self.source:
            return
        jobs = [(e, self.slots[e.slot]) for e in self.entries if e.slot]
        missing = sorted({e.slot for e, s in jobs if not s.path})
        if missing:
            messagebox.showwarning("PhotoSelection",
                                   "Für diese Tasten ist kein Zielordner gesetzt: "
                                   + ", ".join(str(i) for i in missing))
            return
        if not jobs:
            messagebox.showinfo("PhotoSelection", "Es ist nichts zugeordnet.")
            return
        if any(s.path == self.source for _, s in jobs):
            messagebox.showwarning("PhotoSelection", "Ein Zielordner ist der Quellordner.")
            return

        cfg = dict(self.export_cfg)
        move = self.move_files.get()
        replace = cfg["develop"] and cfg["mode"] == "statt"
        plan = [(e, s, self.dev_for(e).copy()) for e, s in jobs]
        n_files = 0 if replace else sum(len(e.files) for e, _, _ in plan)
        n_dev = sum(1 for e, _, _ in plan if cfg["develop"]) if cfg["develop"] else 0

        lines = [f"  {i}: {sum(len(e.files) for e, _, _ in plan if e.slot == i)} Datei(en) → "
                 f"{self.slots[i].path}" for i in sorted({e.slot for e, _, _ in plan})]
        text = f"{len(plan)} Aufnahme(n) verarbeiten?\n\n" + "\n".join(lines)
        if cfg["develop"]:
            text += f"\n\n{n_dev} JPG entwickeln ("
            text += f"Unterordner „{DEV_SUBDIR}“)" if not replace else "statt der Originale)"
            text += f"\nQualität {cfg['quality']}, lange Kante: " + (
                f"{cfg['long_edge']} px" if cfg["long_edge"] else "Original")
        if n_files:
            text += f"\n\n{n_files} Datei(en) {'verschieben' if move else 'kopieren'}"
        if replace and move:
            text += "\n\nHinweis: Originale bleiben im Quellordner, da sie durch die " \
                    "entwickelte JPG ersetzt werden."
        if not messagebox.askokcancel("PhotoSelection", text):
            return

        self._busy = True
        self.btn_run.config(state=tk.DISABLED)
        threading.Thread(target=self._run_worker, daemon=True,
                         args=(plan, move, self.keep_tree.get(), cfg)).start()

    def _run_worker(self, plan, move: bool, keep_tree: bool, cfg: dict) -> None:
        ops: list[tuple[str, str, str, int]] = []
        created_dirs: list[str] = []
        done_entries: list[tuple[Entry, str, int]] = []
        skipped = 0
        errors: list[str] = []
        replace = cfg["develop"] and cfg["mode"] == "statt"
        total = max(1, sum(0 if replace else len(e.files) for e, _, _ in plan)
                    + (len(plan) if cfg["develop"] else 0))
        done = 0

        for entry, slot, dev in plan:
            entry_ok = True
            key = str(entry.preview)
            rel_dir = Path(entry.reldir) if (keep_tree and entry.reldir != ".") else Path()

            if not replace:
                for src in entry.files:
                    done += 1
                    try:
                        self._ensure_dir(slot.path / rel_dir, slot.path, created_dirs)
                        dest = self._prepare(slot.path / rel_dir / src.name, src)
                        if dest is None:
                            skipped += 1
                            continue
                        if move:
                            shutil.move(str(src), str(dest))
                            ops.append((str(src), str(dest), "move",
                                        dest.stat().st_size))
                        else:
                            shutil.copy2(src, dest)
                            ops.append((str(src), str(dest), "copy",
                                        dest.stat().st_size))
                    except Exception as exc:
                        entry_ok = False
                        errors.append(f"{src.name}: {exc}")
                    self.worker_q.put(("progress",
                                       f"{'Verschiebe' if move else 'Kopiere'} "
                                       f"{done}/{total} …"))

            if cfg["develop"]:
                done += 1
                self.worker_q.put(("progress", f"Entwickle {entry.stem} ({done}/{total}) …"))
                try:
                    folder = slot.path / rel_dir if replace else \
                        slot.path / DEV_SUBDIR / rel_dir
                    self._ensure_dir(folder, slot.path, created_dirs)
                    dest = self._prepare(folder / (entry.stem + ".jpg"))
                    export_jpeg(entry.master, dev, dest, cfg["quality"],
                                cfg["long_edge"], cfg["strip_gps"])
                    ops.append((str(entry.master), str(dest), "develop",
                                dest.stat().st_size))
                except Exception as exc:
                    entry_ok = False
                    errors.append(f"{entry.stem}: Entwicklung fehlgeschlagen: {exc}")

            if entry_ok:
                done_entries.append((entry, key, entry.slot))

        self.worker_q.put(("done", (ops, created_dirs, done_entries, skipped,
                                    errors, move)))

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
    def _prepare(dest: Path, src: Path | None = None) -> Path | None:
        """Kollisionen aufloesen. None = ueberspringen.

        Gleich grosse Datei am Ziel gilt als schon vorhanden. Sonst wird
        ausgewichen - ueberschrieben wird nie.
        """
        if not dest.exists():
            return dest
        if src is not None and dest.stat().st_size == src.stat().st_size:
            return None
        return App._unique(dest)

    @staticmethod
    def _unique(dest: Path) -> Path:
        n = 1
        while True:
            candidate = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
            if not candidate.exists():
                return candidate
            n += 1

    def _run_done(self, ops, created_dirs, done_entries, skipped, errors, move) -> None:
        self._busy = False
        self.btn_run.config(state=tk.NORMAL)
        if ops:
            self._push_undo({"type": "files", "ops": ops, "dirs": created_dirs,
                             "slots": [(key, slot) for _, key, slot in done_entries]})
        # Erledigte Zuordnungen zurücksetzen, damit nichts doppelt läuft
        for entry, _, _ in done_entries:
            entry.slot = 0
        self._save_marks()

        kinds = [k for _, _, k, _ in ops]
        msg = (f"{kinds.count('move')} verschoben, {kinds.count('copy')} kopiert, "
               f"{kinds.count('develop')} entwickelt.\n"
               f"{skipped} übersprungen (bereits vorhanden).\n\n"
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
        self._save_marks()
        self._save_slots()
        # Anstehende Timer abbestellen, sonst laufen sie ins zerstoerte Fenster
        for job in (self._poll_job, self._resize_job, self._render_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self.destroy()


# --------------------------------------------------------------------------
# Overlays
# --------------------------------------------------------------------------

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
    arr = np.asarray(img.convert("RGB"), np.float32)
    return np.stack([np.histogram(arr[:, :, c], bins=256, range=(0, 255))[0]
                     for c in range(3)])


def mark_clipping(view: Image.Image) -> Image.Image:
    """Ausgefressene Lichter rot, abgesoffene Schatten blau einfaerben."""
    arr = np.array(view.convert("RGB"))
    peak = arr.max(axis=2)
    arr[peak >= HI_CLIP] = (255, 40, 40)
    arr[peak <= LO_CLIP] = (40, 90, 255)
    return Image.fromarray(arr)


def draw_exif_panel(view: Image.Image, prev: Preview, entry: Entry) -> None:
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

    font, bold = _font(14), _font(15)
    pad, line_h = 10, 19
    width = max([int(bold.getlength(lines[0]))]
                + [int(font.getlength(t)) for t in lines[1:]])
    width = min(max(width, 190), max(view.width - 24, 190)) + 2 * pad
    height = line_h * len(lines) + 26 + 2 * pad

    panel = Image.new("RGBA", (width, height), (0, 0, 0, 150))
    draw = ImageDraw.Draw(panel)
    y = pad
    draw.text((pad, y), lines[0], font=bold, fill=(255, 255, 255, 255))
    y += line_h + 2
    for text in lines[1:]:
        draw.text((pad, y), text, font=font, fill=(225, 225, 225, 255))
        y += line_h

    # Schaerfe als Balken - log-skaliert, 500 gilt als knackig
    score = prev.sharpness
    frac = min(np.log10(score + 1) / np.log10(501), 1.0)
    colour = (61, 220, 132, 255) if frac > 0.75 else \
             (242, 225, 76, 255) if frac > 0.5 else (224, 108, 117, 255)
    draw.text((pad, y + 2), f"Schärfe {score:.0f}", font=font, fill=(200, 200, 200, 255))
    bar_x = pad + 96
    draw.rectangle([bar_x, y + 8, width - pad, y + 16], fill=(255, 255, 255, 40))
    draw.rectangle([bar_x, y + 8, bar_x + (width - pad - bar_x) * frac, y + 16], fill=colour)

    view.paste(panel, (12, 12), panel)


def draw_histogram(view: Image.Image, hist: np.ndarray) -> None:
    """RGB-Histogramm unten rechts, gefuellt damit es auch auf hellen Bildern liest."""
    w, h = min(272, max(view.width - 24, 160)), 104
    pad = 8
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
        pen.line(points, fill=(r, g, b, 235), width=2)
        panel = Image.alpha_composite(panel, layer)

    ImageDraw.Draw(panel).rectangle([pad, pad, w - pad, h - pad],
                                    outline=(255, 255, 255, 60))
    view.paste(panel, (view.width - w - 12, view.height - h - 12), panel)


def main() -> None:
    if rawpy is None:
        print("Hinweis: rawpy fehlt - RAW-Dateien werden nicht angezeigt.")
    if exifread is None:
        print("Hinweis: exifread fehlt - EXIF nur aus dem Vorschaubild.")
    App().mainloop()


if __name__ == "__main__":
    main()
