# Movie Organizer

A Python script that scans a folder of downloaded movies, cleans up their titles,
and moves each one into a tidy, Plex-friendly library structure:

```
Movie Title (Year)/
├── Movie Title (Year).mkv
├── Movie Title (Year).srt
└── Extras/
    └── Deleted Scene.mkv
```

Its standout feature is **quality-aware deduplication**: when a movie already
exists in your library, the script uses `ffprobe` to compare the incoming file
against the existing one (resolution → bitrate → file size) and keeps only the
better copy, deleting the loser automatically.

---

## Features

- **Title + year extraction** — parses `Movie.Title.2021.1080p.WEB-DL.x265.mkv`
  into `Movie Title (2021)`, stripping quality/codec tags, site prefixes, and
  separators along the way.
- **Quality-aware dedup** — if the destination already has the movie, it runs an
  `ffprobe` comparison and keeps the higher-quality version:
  1. Higher resolution wins.
  2. If resolution ties, a >10% bitrate difference decides it.
  3. If bitrate is inconclusive, a >15% file-size difference decides it.
  4. Otherwise the newer file is kept.
- **Subtitle handling** — keeps sidecar subtitle files (`.srt`, `.sub`, `.idx`,
  `.ass`, `.ssa`, `.vtt`) alongside the movie.
- **Extras preservation** — folders like `Extras`, `Specials`, `Features`,
  `Bonus`, `Deleted`, `Interviews`, and `Trailers` are moved intact rather than
  flattened.
- **ffprobe caching** — quality analysis results are cached in memory so the same
  file is never probed twice in one run.
- **Parallel processing** — a configurable thread pool processes multiple items
  at once.
- **Logging** — moves, deletions, quality decisions, and failures are all logged.

---

## Requirements

- Python 3.7+
- [`python-dotenv`](https://pypi.org/project/python-dotenv/)
- **`ffprobe`** (ships with [FFmpeg](https://ffmpeg.org/)) — must be installed and
  available on your system `PATH`.

Install the Python dependency:

```bash
pip install python-dotenv
```

Install FFmpeg (which provides `ffprobe`):

```bash
# Debian/Ubuntu
sudo apt install ffmpeg

# macOS (Homebrew)
brew install ffmpeg

# Windows (winget)
winget install Gyan.FFmpeg
```

> **Note:** If `ffprobe` isn't available, the script still runs, but quality
> comparisons fall back to "couldn't analyze" and the incoming file is generally
> preferred. For the dedup feature to work as intended, install FFmpeg.

---

## Configuration

The script reads its settings from a `.env` file in the same directory. Create
one before running:

```env
# Required
SOURCE_DIR=/path/to/your/downloads
DEST_DIR=/path/to/your/movie/library

# Optional
LOG_DIR=/path/to/logs   # defaults to the script's own directory
MAX_WORKERS=1           # threads; keep low for HDDs, raise to 8+ for SSD/NVMe
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `SOURCE_DIR` | Yes | — | Folder to scan for movies. |
| `DEST_DIR` | Yes | — | Root of your organized library where movies are moved. |
| `LOG_DIR` | No | script directory | Where `movie_organizer.log` is written. |
| `MAX_WORKERS` | No | `1` | Number of worker threads. Keep at 1–2 for spinning HDDs; 8+ is fine for SSD/NVMe. |

---

## Usage

Once your `.env` is set up:

```bash
python MovieOrganizerScript.py
```

The script prints live progress to the terminal, for example:

```
Starting movie organization...
Found 25 item(s) to process via 4 parallel workers.

Progress: 25/25 (100%) | Processing: Some Movie (2021)

Complete! Processed: 22 | Skipped: 2 | Lower Quality Dropped: 1
```

### File types

| Category | Extensions |
|---|---|
| Video (main + counted for moves) | `.mkv` `.mp4` `.avi` `.mov` `.wmv` `.flv` `.webm` `.m4v` `.m2ts` |
| Subtitles (kept) | `.srt` `.sub` `.idx` `.ass` `.ssa` `.vtt` |

Files with any other extension are ignored and left behind.

---

## Output structure

Each movie is placed in its own `Title (Year)` folder. The main video file is
renamed to match the folder; subtitles and other kept files retain their original
names:

```
DEST_DIR/
└── Movie Title (Year)/
    ├── Movie Title (Year).mkv
    ├── Movie Title (Year).srt
    └── Extras/
        └── Original Extra Filename.mkv
```

Special folders (see the Extras list above) are moved with their original
structure and filenames preserved.

---

## How deduplication works

When a movie already exists in `DEST_DIR`:

1. The script finds the existing video file in the target folder.
2. It probes both the incoming and existing files with `ffprobe`.
3. `compare_quality()` decides the winner using resolution → bitrate → file size,
   in that order.
4. **If the new file is better**, the old one is deleted and the new one moves in.
5. **If the new file is worse**, the *source* item is deleted and the existing
   library file is left untouched (reported as "Lower Quality Dropped").

Result outcomes reported per item:

| Status | Meaning |
|---|---|
| `processed` | At least one file was moved into the library. |
| `skipped` | No title/year match, or nothing to move. |
| `skipped_quality` | Source was a lower-quality duplicate and was deleted. |

---

## Logging

A log file named `movie_organizer.log` is written to `LOG_DIR` (or the script's
directory by default). It records moved files, quality decisions ("Higher
resolution", "Lower bitrate", etc.), deletions, and any errors, with timestamps.

---

## Notes & caveats

- **Test on a copy first.** The script *moves* and *deletes* files, including
  deleting existing library files it judges to be lower quality. Point
  `SOURCE_DIR` at a small test folder before running it on your real library.
- **Title matching relies on a year.** `extract_title_year()` needs a 4-digit
  year in the name to produce a clean `Title (Year)`. Items without a detectable
  year are skipped and left in place.
- **Quality comparison needs ffprobe.** Without it, the dedup logic can't make an
  informed decision and will lean toward keeping the incoming file.
- **HDD vs SSD:** high `MAX_WORKERS` values can *slow down* spinning drives due to
  seek thrashing. Only raise it for SSD/NVMe storage.
- The script calls `os._exit(0)` on completion to exit immediately without
  waiting on lingering threads.

---
