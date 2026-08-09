# PhotoSelection

Schlanke Foto-Voransicht für JPG und RAW. Durchblättern, per Tastendruck einem
von neun Zielordnern zuordnen, am Ende alles in einem Rutsch kopieren oder
verschieben. Eine Datei, keine Installation, kein Katalog.

Wer will, entwickelt die Auswahl beim Ausführen gleich als JPG — das ist aber
ausgeschaltet und ausgeblendet, solange du es nicht anschaltest.

## Start

```
python photo_selection.py
```

oder `start.bat` doppelklicken.

## Ablauf

1. **Quellordner…** wählen — alle JPG/RAW darin werden eingelesen.
2. Rechts im Feld **Zielordner (Tasten 1–9)** für jede Taste, die du benutzen
   willst, einen Ordner setzen und optional benennen (z. B. `1 = Auswahl`,
   `2 = Vielleicht`, `3 = Ausschuss`). Diese Einstellung bleibt dauerhaft
   gespeichert, auch für den nächsten Ordner.
3. Mit ←/→ durchblättern und mit **1–9** zuordnen. Nochmal dieselbe Taste hebt
   die Zuordnung wieder auf.
4. **Zuordnungen ausführen** kopiert (oder verschiebt) alles auf einmal.

Es wird nie gelöscht. Zuordnungen landen in `.photoselection.json` im
Quellordner — du kannst die Sichtung jederzeit unterbrechen und fortsetzen.

## Tasten

| Taste | Wirkung |
|---|---|
| `1`–`9` | Bild dem jeweiligen Zielordner zuordnen (nochmal drücken = aufheben) |
| `Leertaste`, `Enter` | Ordner 1 zuordnen und zum nächsten Bild (nochmal auf demselben Bild = aufheben) |
| `0`, `Entf` | Zuordnung aufheben |
| `←` `→` | Blättern · `Bild↑` `Bild↓` in Zehnerschritten · `Pos1` `Ende` Anfang/Ende |
| `Strg+Z` | Rückgängig — Zuordnungen, Bearbeitung **und** abgeschlossene Kopier-/Verschiebe-Vorgänge |
| `E` | Bearbeitung ein-/ausblenden · `K` Zuschnitt zeichnen |
| `I` | Aufnahmedaten ein-/ausblenden |
| `H` | Histogramm |
| `J` | Clipping-Warnung: ausgefressene Lichter rot, abgesoffene Schatten blau |
| `Z` | Zwischen Einpassen und 100 % umschalten (Mausrad zoomt stufenlos, Ziehen schwenkt) |
| `R` / `Umschalt+R` | Ansicht drehen |
| `F`, `F11` | Vollbild · `Esc` zurück |

## Beurteilungs-Hilfen

- **Aufnahmedaten** (Kamera, Objektiv, Brennweite, Blende, Zeit, ISO, Datum)
  werden per `exifread` direkt aus der Datei gelesen; wo das nicht geht
  (z. B. Canon CR3), aus dem eingebetteten Vorschaubild.
- **Schärfe-Score**: Laplace-Varianz der mittleren 60 % des Bildes — Bokeh am
  Rand verfälscht das Ergebnis also nicht. Der Balken ist logarithmisch
  skaliert; ab etwa 500 ist eine Aufnahme knackig. Der Wert ist zum *Vergleich
  ähnlicher Aufnahmen* gedacht, nicht als absolutes Urteil: ein Motiv mit wenig
  Struktur liegt auch scharf niedrig.
- **Clipping** arbeitet auf der angezeigten Vorschau, bei RAW also auf dem
  kamerainternen JPEG — Reserven in der RAW-Datei sind damit nicht sichtbar.

## Bearbeitung und Entwicklung (optional)

Das Sichten funktioniert ohne das alles — der Bereich ist ausgeblendet, bis du
`E` drückst, und der JPG-Export ist ab Werk aus.

`E` öffnet rechts die Regler: Belichtung, Weißabgleich (Kamera / Auto / Kelvin
mit Tönung), Auto-Helligkeit, Rauschminderung, Lichter, Schatten, Kontrast,
Sättigung, Schärfen. Darunter der Zuschnitt: Seitenverhältnis, Geraderichten und
`K` zum Aufziehen des Ausschnitts (Ecken ziehen zum Ändern, innen ziehen zum
Verschieben). Doppelklick auf einen Regler setzt ihn zurück.

Die Einstellungen sind nicht-destruktiv — an den Originaldateien ändert sich
nichts. Sie gehören zur Aufnahme, werden in `.photoselection.json` gespeichert
und lassen sich mit **Kopieren / Einfügen / Auf alle** auf andere Aufnahmen
übertragen (der Zuschnitt bleibt dabei bildspezifisch). Bearbeitete Aufnahmen
sind in der Liste mit ✎ markiert.

**Ausgabe:** Über **Export…** legst du fest, ob beim Ausführen der Zuordnungen
JPGs entwickelt werden, dazu Qualität und lange Kante. Zwei Betriebsarten:

- *zusätzlich zur RAW-Datei* — Originale wandern wie gewohnt in den Zielordner,
  die entwickelten JPGs in dessen Unterordner `entwickelt`.
- *statt der RAW-Datei* — nur die entwickelte JPG landet im Zielordner. Die
  Originale bleiben im Quellordner liegen, auch bei „Verschieben" — es geht also
  nichts verloren.

Kamera, Objektiv, Blende, Zeit und ISO werden in die erzeugte JPG übernommen —
Standortdaten standardmäßig nicht, siehe [Umgang mit fremden Daten](#umgang-mit-fremden-daten).
Überschrieben wird nie: existiert der Name schon, wird `_1`, `_2` … angehängt.
`Strg+Z` nimmt einen kompletten Durchlauf wieder zurück.

### Was du dabei wissen solltest

- **Die Vorschau beim Sichten ist das Kamera-JPEG.** Erst im Bearbeitungsmodus
  wird das RAW wirklich entwickelt (halbe Auflösung, im Hintergrund, unten
  erscheint „entwickle…"). Ohne den Modus zeigt die Vorschau Belichtung und
  Weißabgleich nur genähert, alles andere stimmt.
- **Auto-Helligkeit bestimmt, wo die Belichtung angreift.** Eingeschaltet
  (Standard) sorgt LibRaws Helligkeitsautomatik für ein kameraähnliches
  Ausgangsbild, und der Belichtungsregler wirkt danach — sofort, ohne neue
  Entwicklung. Ausgeschaltet greift er linear im RAW, dort lassen sich Lichter
  eher retten, das Ausgangsbild ist aber deutlich flauer. Beides zusammen geht
  nicht: mit aktiver Automatik würde eine Korrektur im RAW gleich wieder
  weggeregelt.
- **Schärfen ist Ausgabeschärfung** und wird nach dem Skalieren angewendet; in
  der Vorschau ist es deshalb nur ein Anhaltspunkt.
- Der Stapel-Export braucht je nach Kamera 4–25 Sekunden pro RAW, weil dafür in
  voller Auflösung entwickelt wird.

### Bekannte Eigenheiten einzelner Formate

Geprüft an Sony ARW, Canon CR2, Canon CR3 und DNG:

- **Canon CR3**: `exifread` kann das Format nicht lesen, die Aufnahmedaten
  kommen deshalb aus dem eingebetteten Vorschaubild. Objektiv und
  Aufnahmedatum fehlen dort — Kamera, Blende, Zeit, ISO und Brennweite sind da.
- **Gestitchte Panorama-DNGs** (z. B. von DJI-Drohnen) sind bereits demosaikt.
  LibRaw ignoriert dort den eingestellten Weißabgleich, deshalb wird die
  Kelvin-Korrektur bei solchen Dateien nach der Entwicklung angewendet.
  Außerdem ist das eingebettete Vorschaubild schon entzerrt und beschnitten,
  während die RAW-Daten den rohen Stich samt schwarzer Ränder enthalten —
  Vorschau und Entwicklung sehen dort also unterschiedlich aus.

## Optionen

- **Unterordner** — rekursiv einlesen. Zusammen mit **Struktur erhalten** wird
  die Ordnerstruktur im Ziel nachgebaut.
- **RAW+JPG als Paar** — Dateien mit gleichem Namen (`IMG_0001.CR3` +
  `IMG_0001.JPG`) gelten als eine Aufnahme und werden gemeinsam kopiert.
- **Verschieben** statt kopieren — mit Rückfrage, und `Strg+Z` schiebt zurück.
- **Anzeigen** — Filter auf zugeordnete, offene oder einen bestimmten Ordner.

Existiert eine Datei im Ziel bereits und ist gleich groß, wird sie
übersprungen; bei abweichender Größe wird `_1`, `_2` … angehängt.

## Technik

RAW-Dateien werden über das eingebettete Vorschau-JPEG angezeigt
(`rawpy.extract_thumb`), nicht entwickelt — das ist um ein Vielfaches
schneller. Nur wenn kein Vorschaubild vorhanden ist, wird mit halber Auflösung
entwickelt. Die Orientierung kommt aus dem EXIF-Tag, ersatzweise aus dem
LibRaw-Flip. Ein Hintergrund-Thread lädt die nächsten und vorherigen Bilder
vor und hält acht Bilder im Speicher.

Viele Kameras legen dieses Vorschaubild in voller Sensorgröße ab. Statt es
komplett zu dekodieren und dann zu verkleinern, fordert `Image.draft()` die
Verkleinerung direkt von libjpeg an — gemessen auf echten Dateien: Canon CR2
867 → 144 ms, Canon CR3 1198 → 196 ms. Alle geprüften Formate liegen damit
unter 200 ms pro Bild.

Ohne `rawpy` läuft das Programm weiter, zeigt dann aber nur JPG/TIF/PNG. Ohne
`exifread` kommen die Aufnahmedaten nur aus dem Vorschaubild.

## Umgang mit fremden Daten

Das Programm liest Ordner fremder Herkunft — Speicherkarten, Netzlaufwerke,
geteilte Ordner — und schreibt, verschiebt und löscht Dateien. Entsprechend:

- **Nichts wird überschrieben.** Gibt es die Zieldatei schon und ist gleich
  groß, wird übersprungen; sonst wird `_1`, `_2` … angehängt.
- **Rückgängig prüft vorher nach.** Eine Datei am Zielpfad wird nur entfernt,
  wenn sie noch genau die ist, die dieser Vorgang erzeugt hat. Wurde sie
  inzwischen ersetzt, bleibt sie liegen und die Statuszeile sagt es.
- **Ordner werden nur entfernt, wenn der Vorgang sie selbst angelegt hat.**
  Zielordner und vorher vorhandene Unterordner bleiben stehen.
- **Die `.photoselection.json` wird streng geprüft.** Sie liegt im Quellordner
  und reist mit ihm mit, kann also beschädigt oder präpariert sein. Unpassende
  Werte fallen auf den Standard zurück, statt das Programm abzureißen.
- **Standortdaten werden beim Export standardmäßig entfernt.** Kamera,
  Objektiv, Blende, Zeit und ISO bleiben erhalten, GPS nicht — exportierte JPGs
  sind das, was man weitergibt. Im Export-Dialog abschaltbar.
- Der Scan bleibt im Quellordner; Verzeichnis-Verknüpfungen werden nicht
  verfolgt, und Pfade aus der Zuordnungsdatei können nicht aus ihm ausbrechen.

Bildformate werden von Pillow und LibRaw geparst — das ist die eigentliche
Angriffsfläche bei Dateien unbekannter Herkunft. Beide über
`pip install -U -r requirements.txt` aktuell halten.

## Konfiguration

- `.photoselection.json` im Quellordner — die Zuordnungen dieses Ordners.
- `~/.photoselection_slots.json` — die neun Zielordner samt Namen, global.
