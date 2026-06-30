# dji-osv2mp4

> **🇵🇱 Polska wersja poniżej | 🇬🇧 English version below**

---

## 🇵🇱 dji-osv2mp4 — po polsku

Narzędzie CLI do konwersji plików .OSV z kamer DJI Osmo 360 / Avata360
na standardowe .mp4 za pomocą **ffmpeg** — bez Windows, bez SikuliX, bez DJI Studio.

**Działa natywnie na Linux, w tym SteamOS!**

Narzędzie powstało, aby uwolnić użytkowników od cholernego zastrzeżonego formatu .OSV.
Firma DJI nie udostępnia ŻADNEGO API (dla porównania Insta360 udostępnia SDK dla swoich
kamer 360), przez co eksport i obróbka metadanych dla dużej liczby plików jest
EKSTREMALNIE frustrująca.

### Funkcje

#### 1. Konwersja wsadowa `.OSV` → `.mp4`

Używa **ffmpeg** z filtrem `v360` do konwersji wideo 360° z podwójnego rybiego oka na:

- **Pełną panoramę 360° equirectangular** (zszyta z obu strumieni)
- **Pojedyncze rybie oko** (surowy widok, jeden strumień)

Obsługiwane rozdzielczości:

| Rozdzielczość | Wymiar wyjścia |
|---------------|----------------|
| `4K`          | 3840×1920      |
| `5K`          | 5120×2560      |
| `6K`          | 5760×2880      |
| `8K`          | 7680×3840      |

Presety jakości (CRF):

| Preset         | CRF  |
|----------------|------|
| `low` (niska)  | 28   |
| `medium`       | 23   |
| `recommended`  | 23   |
| `high` (wysoka)| 18   |

Obsługuje też wszystkie presety x265 (`ultrafast`, `fast`, `medium`, `slow`, itd.).

#### 2. Synchronizacja metadanych

Naprawia metadane wyeksportowanych plików `.mp4` za pomocą **ExifTool**.

- Kopiuje metadane z odpowiadającego pliku `.OSV`
- Dopasowuje strefę czasową i znaczniki czasu
- Ustawia model kamery

### Wymagania

- **ffmpeg** (z libx265 i filtrem v360)
- **Python 3.8+**
- **ExifTool** (do wstrzykiwania metadanych)

#### Instalacja zależności

```bash
# Debian / Ubuntu
sudo apt install ffmpeg libimage-exiftool-perl

# SteamOS / Arch Linux (pacman)
sudo pacman -S ffmpeg perl-image-exiftool

# SteamOS (Homebrew)
brew install ffmpeg exiftool

# macOS (Homebrew)
brew install ffmpeg exiftool
```

### Użycie

#### 1. Konwersja wsadowa (`main.py`)

```bash
python main.py <katalog_wejściowy> <katalog_wyjściowy> [opcje]
```

| Argument                | Opis                              |
| ----------------------- | --------------------------------- |
| `input_dir`             | Katalog z plikami `.OSV`          |
| `output_dir`            | Katalog na pliki `.mp4`           |

##### Opcje

| Opcja                        | Opis                                              |
| ---------------------------- | ------------------------------------------------- |
| `-r, --resolution`           | `4K`, `5K`, `6K`, `8K` (domyślnie: `4K`)         |
| `-q, --quality`              | `low`, `medium`, `high`, `recommended` (domyślnie: `recommended`) |
| `-p, --preset`               | Preset x265: `ultrafast`..`veryslow` (domyślnie: `fast`) |
| `--mode`                     | `stitch` (pełna panorama 360°) lub `raw` (pojedyncze rybie oko) |
| `--stream`                   | Indeks strumienia w trybie `raw`: `0` lub `1`     |
| `--dlog`                     | Tryb D-Log M: 10-bit, płaski profil kolorów, bez korekcji – do gradacji w DaVinci Resolve z oficjalnym LUT-em DJI |
| `--bit-depth`                | `8bit` lub `10bit` (domyślnie: `8bit`). Automatycznie `10bit` przy `--dlog` |
| `--calibrated`               | Używa danych kalibracyjnych obiektywu bezpośrednio z pliku dla lepszego złączenia (eksperymentalne) |
| `--dji-stitch`               | Używa zaawansowanego złączenia naśladującego DJI Studio (dopasowywanie kolorów, blendowanie gradientowe zamiast zwykłego nałożenia, wymusza `--calibrated`) |
| `--use-gyro`                 | Włącza stabilizację obrazu za pomocą żyroskopu (wymaga filtra ffmpeg `vidstab`) |
| `--gyro-sensitivity`         | Czułość stabilizacji żyroskopowej `1-20` (domyślnie: `5`) |
| `--gyro-zoom`                | Współczynnik przybliżenia dla stabilizacji, aby ukryć czarne ramki (domyślnie: `1.0`) |
| `--keep-osv`                 | Zamiast generować `.mp4`, przebudowuje plik z zachowaniem pełnej struktury `.OSV` i metadanych żyroskopu (wymaga `osvtoolbox`) |
| `--use-container`            | Wykonuje `ffmpeg` z wnętrza kontenera `archlinix` (np. na Steam Deck, jeśli hostowy `ffmpeg` nie ma biblioteki `vidstab`) |
| `--v360-options`             | Niestandardowe opcje filtra v360                   |
| `--eq-options`               | Niestandardowe opcje korekcji kolorów              |
| `--no-skip-existing`         | Nadpisuj istniejące pliki wyjściowe                |
| `--dry-run`                  | Pokaż komendy ffmpeg bez wykonywania               |

##### Przykłady

```bash
# Konwersja wszystkich .OSV do panoramy 4K
python main.py ./input ./output

# Konwersja do 8K z wysoką jakością, wolnym presetem
python main.py ./input ./output -r 8K -q high -p slow

# Konwersja pojedynczego rybiego oka (bez szycia)
python main.py ./input ./output --mode raw --stream 0

# Eksport w D-Log M 10-bit do gradacji w DaVinci Resolve
python main.py ./input ./output --mode raw --dlog

# Złączenie sferyczne klasy DJI (z maską sferyczną) i kalibracją
python main.py ./input ./output --mode stitch --dji-stitch

# Użycie stabilizacji żyroskopowej w trybie raw zachowując format struktury .OSV
python main.py ./input ./output --mode raw --use-gyro --keep-osv

# Podgląd (zobacz co zostanie wykonane)
python main.py ./input ./output --dry-run
```

##### Nazewnictwo plików

```
DJI_20260626205418_0001_D.OSV
  -> DJI_20260626205418_0001_D.panorama.4K.mp4   (tryb stitch)
  -> DJI_20260626205418_0001_D.fisheye.4K.mp4    (tryb raw)
```

#### 2. Naprawa metadanych (`inject.py`)

```bash
python inject.py "<wzorzec_glob>" [strefa_czasowa] [opcje]
```

| Argument      | Opis                                              |
| ------------- | ------------------------------------------------- |
| `dir`         | Wzorzec glob dla plików .mp4 (np. `"*.mp4"`)      |
| `correct_tz`  | Docelowa strefa czasowa (np. `2` dla UTC+2)       |

##### Opcje

| Opcja                  | Opis                                                    |
| ---------------------- | ------------------------------------------------------- |
| `-t, --default-tz`     | Strefa czasowa kamery (domyślnie: 8 dla UTC+8)          |
| `-o, --osv-dir`        | Katalog z plikami .OSV (jeśli w innym miejscu)          |
| `-f, --overwrite`      | Wymuś nadpisanie istniejących metadanych                |
| `-d, --debug`          | Wyświetl komendy ExifTool                               |

##### Przykłady

```bash
# Napraw metadane dla wszystkich .mp4, ustaw strefę na UTC+2
python inject.py "*.mp4" 2

# Wymuś nadpisanie, kamera ustawiona na UTC+8, my chcemy UTC+1
python inject.py "./*.mp4" 1 -t 8 -f
```

### Przykładowy przepływ pracy

> **Uwaga:** `main.py` zawsze wymaga dwóch argumentów: katalogu źródłowego (z `.OSV`) i katalogu docelowego (na `.mp4`). Pliki wyjściowe **nigdy** nie są zapisywane w tym samym katalogu co źródłowe.

#### Krok 1: Konwersja plików `.OSV`

**Opcja A** – `.OSV` są w bieżącym katalogu, wynik do podkatalogu `./output`:
```bash
python main.py . ./output -r 4K
```

**Opcja B** – `.OSV` są w podkatalogu `./osv`, wynik do podkatalogu `./mp4`:
```bash
python main.py ./osv ./mp4 -r 8K
```

Różnica to tylko nazwy katalogów:
- `main.py . ./output` → szuka `./*.OSV`, zapisuje do `./output/`
- `main.py ./osv ./mp4` → szuka `./osv/*.OSV`, zapisuje do `./mp4/`

#### Krok 2: Naprawa metadanych

```bash
python inject.py "./mp4/*.mp4" 2
```

Narzędzie automatycznie dopasowuje:
```
DJI_20260626205418_0001_D.panorama.4K.mp4  <->  DJI_20260626205418_0001_D.OSV
```

### Jak to działa

Plik `.OSV` to tak naprawdę standardowy kontener MP4/MOV zawierający:

- **Strumień 0**: Przednie/lewe rybie oko (HEVC, 3000×3000)
- **Strumień 1**: Tylne/prawe rybie oko (HEVC, 3000×3000)
- **Strumienie 2-6**: Metadane DJI (żyroskop, dane kamery)
- **Strumień 7**: Miniatura obrazu

Filtr `v360` używa mapowania `fisheye` do konwersji każdego obiektywu osobno na `equirectangular`
(standardowa projekcja 360°). W trybie `stitch` oba strumienie są konwertowane,
a następnie blendowane (z użyciem maski sferycznej w trybie `--dji-stitch`), tworząc pełną panoramę 360°.

---

## 🇬🇧 English version

This tool is meant to free users from the god damn DJI proprietary .OSV format.
The freaking company DOES NOT provide any API (in contrast, Insta360 provides an
SDK for its 360 cameras) for automation. As a result, exporting and handling
metadata for large amounts of video is EXTREMELY frustrating.

## Features

### 1. Batch Convert `.OSV` → `.mp4`

Uses **ffmpeg** with the `v360` filter to convert dual-fisheye 360° video into:

- **Full 360° equirectangular panorama** (stitched from both fisheye streams)
- **Single fisheye view** (raw, one stream only)

Supports:

| Resolution | Output size |
|------------|-------------|
| `4K`       | 3840×1920   |
| `5K`       | 5120×2560   |
| `6K`       | 5760×2880   |
| `8K`       | 7680×3840   |

Quality presets (CRF):

| Preset        | CRF  |
|---------------|------|
| `low`         | 28   |
| `medium`      | 23   |
| `recommended` | 23   |
| `high`        | 18   |

Also supports all x265 presets (`ultrafast`, `fast`, `medium`, `slow`, etc.).

### 2. Metadata Synchronization

Fixes metadata of exported `.mp4` files using **ExifTool**.

- Copies metadata from corresponding `.OSV` file
- Adjusts timezone and timestamps
- Sets camera model

## Requirements

- **ffmpeg** (with libx265 and v360 filter)
- **Python 3.8+**
- **ExifTool** (for metadata injection)

### Install dependencies

```bash
# Debian / Ubuntu
sudo apt install ffmpeg libimage-exiftool-perl

# SteamOS / Arch Linux (pacman)
sudo pacman -S ffmpeg perl-image-exiftool

# SteamOS (Homebrew)
brew install ffmpeg exiftool

# macOS (Homebrew)
brew install ffmpeg exiftool
```

## Usage

### 1. Batch Convert (`main.py`)

```bash
python main.py <input_dir> <output_dir> [options]
```

| Argument     | Description                          |
| ------------ | ------------------------------------ |
| `input_dir`  | Directory containing `.OSV` files    |
| `output_dir` | Output directory for `.mp4` files    |

#### Options

| Option                     | Description                                      |
| -------------------------- | ------------------------------------------------ |
| `-r, --resolution`         | `4K`, `5K`, `6K`, `8K` (default: `4K`)          |
| `-q, --quality`            | `low`, `medium`, `high`, `recommended` (default: `recommended`) |
| `-p, --preset`             | x265 preset: `ultrafast`..`veryslow` (default: `fast`) |
| `--mode`                   | `stitch` (full 360° panorama) or `raw` (single fisheye) |
| `--stream`                 | Fisheye stream index in `raw` mode: `0` or `1`   |
| `--dlog`                   | D-Log M mode: 10-bit flat profile, no color correction – for grading in DaVinci Resolve with official DJI LUT |
| `--bit-depth`              | `8bit` or `10bit` (default: `8bit`). Auto `10bit` with `--dlog` |
| `--calibrated`             | Use lens distortion correction from DJI Avata360 calibration data for better stitching quality (experimental) |
| `--dji-stitch`             | Use DJI-style stitching with color matching and spherical gradient blending. Produces smoother seams. Implies `--calibrated`. |
| `--use-gyro`               | Enable gyroscope-based video stabilization (uses ffmpeg `vidstab`) |
| `--gyro-sensitivity`       | Gyro stabilization sensitivity `1-20` (default: `5`) |
| `--gyro-zoom`              | Zoom factor for gyro stabilization to hide black borders (default: `1.0`) |
| `--keep-osv`               | Preserve OSV file structure (metadata, gyro, camd) after conversion. Output will be `.OSV`. Requires `osvtoolbox`. |
| `--use-container`          | Use `ffmpeg` from `archlinix` container (useful for Steam Deck if host `ffmpeg` lacks `vidstab`) |
| `--v360-options`           | Custom v360 filter options                       |
| `--eq-options`             | Custom colour equalizer options                  |
| `--no-skip-existing`       | Overwrite existing output files                  |
| `--dry-run`                | Show ffmpeg commands without executing           |

#### Examples

```bash
# Convert all .OSV files to 4K panorama
python main.py ./input ./output

# Convert to 8K with high quality, slow preset
python main.py ./input ./output -r 8K -q high -p slow

# Convert single fisheye stream (no stitching)
python main.py ./input ./output --mode raw --stream 0

# Export in D-Log M 10-bit for grading in DaVinci Resolve
python main.py ./input ./output --mode raw --dlog

# DJI-style stitching with color matching and spherical mask blend:
python main.py ./input ./output --mode stitch --dji-stitch

# Stabilize with gyroscope data and preserve OSV metadata structure:
python main.py ./input ./output --mode raw --use-gyro --keep-osv

# Dry run (see what would be executed)
python main.py ./input ./output --dry-run
```

#### File Naming

```
DJI_20260626205418_0001_D.OSV
  -> DJI_20260626205418_0001_D.panorama.4K.mp4   (stitch mode)
  -> DJI_20260626205418_0001_D.fisheye.4K.mp4    (raw mode)
  -> DJI_20260626205418_0001_D.fisheye.4K.D-Log-M.mp4  (raw + --dlog)
```

### 2. Metadata Fix (`inject.py`)

```bash
python inject.py "<glob_path>" [timezone] [options]
```

| Argument      | Description                                  |
| ------------- | -------------------------------------------- |
| `dir`         | Glob pattern for .mp4 files (e.g. `"*.mp4"`) |
| `correct_tz`  | Target timezone offset (e.g. `2` for UTC+2)  |

#### Options

| Option                 | Description                                      |
| ---------------------- | ------------------------------------------------ |
| `-t, --default-tz`     | Camera timestamp timezone (default: 8 for UTC+8) |
| `-o, --osv-dir`        | Directory with .OSV files (if different location) |
| `-f, --overwrite`      | Force overwrite existing metadata                |
| `-d, --debug`          | Print ExifTool commands                          |

#### Examples

```bash
# Fix metadata for all .mp4 files in current dir, set timezone to UTC+2
python inject.py "*.mp4" 2

# Force overwrite, camera was set to UTC+8, we want UTC+1
python inject.py "./*.mp4" 1 -t 8 -f
```

## Workflow Example

### Step 1: Convert `.OSV` files

```bash
python main.py ./osv ./mp4 -r 8K
```

### Step 2: Fix metadata

```bash
python inject.py "./mp4/*.mp4" 2
```

The tool automatically matches:
```
DJI_20260626205418_0001_D.panorama.4K.mp4  <->  DJI_20260626205418_0001_D.OSV
```

## How it works

The `.OSV` file is actually a standard MP4/MOV container with:

- **Stream 0**: Front/left fisheye video (HEVC, 3000×3000)
- **Stream 1**: Back/right fisheye video (HEVC, 3000×3000)
- **Streams 2-6**: DJI metadata (gyro, camera data)
- **Stream 7**: Thumbnail image

The `v360` filter uses standard `fisheye` mapping to convert each lens stream to `equirectangular`
(standard 360° projection) separately. In `stitch` mode, both streams are converted and
then blended together (using a spherical gradient mask in `--dji-stitch` mode) for a full 360° panorama.

## License

MIT License