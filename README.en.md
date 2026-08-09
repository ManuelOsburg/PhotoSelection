# PhotoSelection

*[Deutsche Fassung](README.md) · The application's interface is in German.*

A lean photo previewer for JPG and RAW. Flip through a shoot, assign each frame
to one of nine destination folders with a keystroke, then copy or move
everything in one go. One file, no installation, no catalogue.

If you want it, the selection can be developed straight to JPG during that final
step — but it stays switched off and out of sight until you turn it on.

## Running it

```
python photo_selection.py
```

or double-click `start.bat`.

## The workflow

1. Pick a **source folder** — every JPG/RAW inside is read in.
2. In the **destination folders (keys 1–9)** panel on the left, point each key
   you plan to use at a folder and optionally name it (say `1 = Picks`,
   `2 = Maybe`, `3 = Reject`). This setting is stored permanently and carries
   over to the next shoot.
3. Browse with ←/→ and assign with **1–9**. Pressing the same key again clears
   the assignment.
4. **Run assignments** copies (or moves) everything at once.

Nothing is ever deleted. Assignments live in `.photoselection.json` inside the
source folder, so you can stop halfway through and pick up later.

## Keys

| Key | Effect |
|---|---|
| `1`–`9` | Assign the frame to that destination folder (press again to clear) |
| `Space`, `Enter` | Assign to folder 1 and advance (again on the same frame clears it) |
| `0`, `Del` | Clear the assignment |
| `←` `→` | Browse · `PgUp` `PgDn` in steps of ten · `Home` `End` first/last |
| `Ctrl+Z` | Undo — assignments, edits **and** completed copy/move runs |
| `E` | Show/hide the editing panel · `K` draw a crop |
| `I` | Show/hide shooting data |
| `H` | Histogram |
| `J` | Clipping warning: blown highlights red, crushed shadows blue |
| `Z` | Toggle fit ↔ 100 % (the wheel zooms freely, dragging pans) |
| `R` / `Shift+R` | Rotate the view |
| `F`, `F11` | Full screen · `Esc` back |

## Judging aids

- **Shooting data** (camera, lens, focal length, aperture, shutter, ISO, date)
  is read straight from the file via `exifread`; where that fails (Canon CR3,
  for instance) it comes from the embedded preview image.
- **Sharpness score**: Laplacian variance over the middle 60 % of the frame, so
  deliberate background blur doesn't drag it down. The bar is logarithmic;
  roughly 500 and up reads as crisp. Treat it as a way to *compare similar
  frames*, not as an absolute verdict — a low-texture subject scores low even
  when perfectly sharp.
- **Clipping** is computed on the displayed preview, which for RAW means the
  camera's own JPEG — headroom still present in the RAW file is not visible
  there.

## Editing and development (optional)

Culling works without any of this — the panel stays hidden until you press `E`,
and JPG export is off by default.

`E` opens the sliders on the right: exposure, white balance (camera / auto /
Kelvin with tint), auto brightness, noise reduction, highlights, shadows,
contrast, saturation, sharpening. Below that the crop: aspect ratio,
straightening, and `K` to drag out the crop rectangle (drag a corner to resize,
drag inside to move). Double-clicking a slider resets it.

Settings are non-destructive — the original files are never touched. They belong
to the frame, are stored in `.photoselection.json`, and can be carried over to
other frames with **Copy / Paste / Apply to all** (the crop stays
frame-specific). Edited frames are marked with ✎ in the list.

**Output:** the **Export…** dialog controls whether JPGs are developed when you
run the assignments, along with quality and long edge. Two modes:

- *in addition to the RAW file* — originals go to the destination folder as
  usual, the developed JPGs into its `entwickelt` subfolder.
- *instead of the RAW file* — only the developed JPG lands in the destination.
  The originals stay in the source folder even with "move" enabled, so nothing
  can be lost.

Camera, lens, aperture, shutter and ISO are carried into the resulting JPG —
location data is not, by default; see [Handling foreign data](#handling-foreign-data).
Nothing is ever overwritten: if the name is taken, `_1`, `_2` … is appended.
`Ctrl+Z` takes a whole run back.

### Things worth knowing

- **The preview while culling is the camera's JPEG.** Only in editing mode is
  the RAW actually developed (half resolution, in the background, with
  "entwickle…" shown at the bottom). Outside that mode the preview approximates
  exposure and white balance; everything else is exact.
- **Auto brightness decides where exposure applies.** Switched on (the default),
  LibRaw's brightness automation produces a camera-like starting image and the
  exposure slider acts afterwards — instantly, with no re-development. Switched
  off, exposure applies linearly in the RAW, where highlights are easier to
  recover, but the starting image is noticeably flatter. You cannot have both:
  with the automation active, a correction made in the RAW is simply regulated
  away again.
- **Sharpening is output sharpening** and is applied after scaling, so in the
  preview it is indicative only.
- Batch export takes 4–25 seconds per RAW depending on the camera, because it
  develops at full resolution.

### Known quirks per format

Verified against Sony ARW, Canon CR2, Canon CR3 and DNG:

- **Canon CR3**: `exifread` cannot read the format, so shooting data comes from
  the embedded preview. Lens and capture date are missing there — camera,
  aperture, shutter, ISO and focal length are present.
- **Stitched panorama DNGs** (from DJI drones, for example) are already
  demosaiced. LibRaw ignores the configured white balance for those, so the
  Kelvin correction is applied after development instead. Their embedded preview
  is also already rectified and cropped, while the RAW data holds the raw stitch
  including black edges — preview and development therefore look different.

## Options

- **Subfolders** — read recursively. Combined with **preserve structure**, the
  folder tree is rebuilt in the destination.
- **RAW+JPG as a pair** — files sharing a name (`IMG_0001.CR3` +
  `IMG_0001.JPG`) count as one frame and are copied together.
- **Move** instead of copy — with a confirmation, and `Ctrl+Z` moves it back.
- **Show** — filter to assigned, unassigned, or one particular folder.

If a file already exists in the destination at the same size it is skipped; at a
different size, `_1`, `_2` … is appended.

## How it works

RAW files are displayed from the embedded preview JPEG
(`rawpy.extract_thumb`) rather than developed — many times faster. Only when no
preview is present is the file developed at half resolution. Orientation comes
from the EXIF tag, falling back to the LibRaw flip. A background thread
preloads the next and previous frames and keeps eight images in memory.

Many cameras store that preview at full sensor size. Instead of decoding it
completely and then shrinking, `Image.draft()` asks libjpeg to do the reduction
during decoding — measured on real files: Canon CR2 867 → 144 ms, Canon CR3
1198 → 196 ms. Every format tested now stays under 200 ms per frame.

Without `rawpy` the program still runs but shows only JPG/TIF/PNG. Without
`exifread`, shooting data comes from the preview image alone.

## Handling foreign data

The program reads folders of foreign origin — memory cards, network shares,
folders someone handed you — and writes, moves and deletes files. Accordingly:

- **Nothing is ever overwritten.** If the destination file exists at the same
  size it is skipped; otherwise `_1`, `_2` … is appended.
- **Undo checks first.** A file at the destination path is removed only if it is
  still exactly the one this run created. If it has been replaced in the
  meantime it stays, and the status bar says so.
- **Folders are removed only if the run created them.** Destination folders and
  pre-existing subfolders are left alone.
- **`.photoselection.json` is validated strictly.** It sits in the source folder
  and travels with it, so it may be corrupted or crafted. Values that don't fit
  fall back to the default instead of taking the program down.
- **Location data is stripped from exports by default.** Camera, lens, aperture,
  shutter and ISO are kept, GPS is not — an exported JPG is the thing you hand
  out. Switchable in the Export dialog.
- The scan stays inside the source folder; directory links are not followed, and
  paths from the assignment file cannot escape it.

Image formats are parsed by Pillow and LibRaw — that is the real attack surface
for files of unknown origin. Keep both current via
`pip install -U -r requirements.txt`.

## Configuration

- `.photoselection.json` in the source folder — that folder's assignments.
- `~/.photoselection_slots.json` — the nine destination folders and their names,
  global.
