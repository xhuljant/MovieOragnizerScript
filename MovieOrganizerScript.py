import os
import re
import shutil
import logging
import sys
import subprocess
import json
import uuid
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# --- CONFIG ---
load_dotenv()  # reads the .env file in the current directory

SOURCE_DIR = os.getenv("SOURCE_DIR")
DEST_DIR = os.getenv("DEST_DIR")
LOG_DIR = os.getenv("LOG_DIR", os.path.dirname(__file__))
LOG_FILE = os.path.join(LOG_DIR, "movie_organizer.log")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "1"))  # Adjust based on drive speeds (keep low for HDDs, can raise to 8+ for NVMe/SSDs)

# New config
# LIBRARY_DIRS: semicolon-separated list of every location to check for existing copies.
#   e.g. LIBRARY_DIRS=D:\Movies;E:\OldMovies;F:\Archive
#   DEST_DIR is always folded in automatically, so you never have to repeat it here.
DELETED_RETENTION_DAYS = int(os.getenv("DELETED_RETENTION_DAYS", "30"))
DURATION_TOLERANCE_MIN = float(os.getenv("DURATION_TOLERANCE_MIN", "5"))
DELETED_FOLDER_NAME = ".deleted"

# --- LOGGING ---
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.m2ts'}
KEEP_EXTENSIONS = VIDEO_EXTENSIONS | {'.srt', '.sub', '.idx', '.ass', '.ssa', '.vtt'}
SPECIAL_FOLDERS = {'extra', 'extras', 'special', 'specials', 'feature', 'features', 'bonus', 'deleted', 'behind', 'interview', 'trailer', 'trailers'}

# Edition keywords -> canonical label. Order matters (first match wins).
EDITION_PATTERNS = [
    (r"director'?s?\s*cut", "Director's Cut"),
    (r"extended(\s*cut)?", "Extended"),
    (r"final\s*cut", "Final Cut"),
    (r"special\s*edition", "Special Edition"),
    (r"ultimate(\s*edition)?", "Ultimate"),
    (r"anniversary(\s*edition)?", "Anniversary"),
    (r"unrated", "Unrated"),
    (r"remastered", "Remastered"),
    (r"\bimax\b", "IMAX"),
    (r"theatrical(\s*cut)?", "Theatrical"),
    (r"uncut", "Uncut"),
    (r"redux", "Redux"),
    (r"recut", "Recut"),
]

# Cache ffprobe results in-memory during execution to prevent analyzing the same destination file repeatedly
FFPROBE_CACHE = {}

# Prevent two workers from racing on the same destination movie (destructive ops).
_LOCKS_MASTER = threading.Lock()
_LOCKS = {}

# Name index snapshot: (norm_title, norm_edition) -> [ {path, year, root, edition}, ... ]
MOVIE_INDEX = {}


def get_movie_lock(key):
    with _LOCKS_MASTER:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


# --------------------------------------------------------------------------
# NAME PARSING & MATCH KEYS
# --------------------------------------------------------------------------
def detect_edition_keyword(name):
    for pattern, label in EDITION_PATTERNS:
        if re.search(pattern, name, flags=re.IGNORECASE):
            return label
    return None


def parse_movie_name(raw):
    """
    Parse a folder or file basename into {title, year, edition}.
    year may be None (permissive). edition may be None.
    Returns None if no usable title can be extracted.
    """
    name = raw

    # Drop a trailing known extension if this is a filename
    base, ext = os.path.splitext(name)
    if ext.lower() in KEEP_EXTENSIONS:
        name = base

    # Strip common site prefixes (carried over from original)
    name = re.sub(r'^[\w\s]+[\s.](org|com|net|mx|to|gg|xyz|info)\s*[-–]\s*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'^www\s+[\w\s]+\s*[-–]\s*', '', name, flags=re.IGNORECASE)

    # Edition: prefer an explicit {edition-...} tag, else scan for keywords
    edition = None
    tag = re.search(r'\{edition-([^}]+)\}', name, flags=re.IGNORECASE)
    if tag:
        edition = tag.group(1).strip()
        name = name[:tag.start()] + name[tag.end():]
    else:
        edition = detect_edition_keyword(name)

    # Year (optional). Take the first plausible 19xx/20xx not glued to other digits.
    ym = re.search(r'(?<!\d)(19\d{2}|20\d{2})(?!\d)', name)
    year = ym.group(1) if ym else None

    # Title is everything before the year, or the whole thing if no year
    title = name[:ym.start()] if ym else name
    title = re.sub(r'[._]', ' ', title)

    # Strip release junk from the tail of the title
    title = re.sub(
        r'\b(2160p|1080p|720p|480p|4k|60fps|10bit|WEB[-.]?DL|WEBRip|BluRay|BDRip|BRRip|'
        r'HDTV|DVDRip|x264|x265|H\.?264|H\.?265|HEVC|AAC|MP3|AC3|DTS|DD5\.?1|DDP5\.?1|'
        r'Atmos|REPACK|PROPER|MULTI|DUAL)\b.*$',
        '', title, flags=re.IGNORECASE
    )
    # Remove any leftover bracketed groups
    title = re.sub(r'[\[\(\{].*?[\]\)\}]', ' ', title)
    title = re.sub(r'\s{2,}', ' ', title).strip(' -_.')

    if not title:
        return None
    return {'title': title, 'year': year, 'edition': edition}


def norm_title(title):
    t = title.lower()
    t = re.sub(r'[^a-z0-9]+', ' ', t)
    t = re.sub(r'^(the|a|an)\s+', '', t)   # leading-article insensitive
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def norm_edition(edition):
    if not edition:
        return ""
    return re.sub(r'[^a-z0-9]+', '', edition.lower())


def display_folder_name(title, year):
    """Plex-friendly library folder: 'Title (Year)' or just 'Title' when yearless."""
    return f"{title} ({year})" if year else title


def sanitize_component(name):
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()


# --------------------------------------------------------------------------
# LIBRARY CONFIG / INDEX
# --------------------------------------------------------------------------
def resolve_library_dirs():
    """Parse LIBRARY_DIRS, fold in DEST_DIR, de-dup, drop missing (with a warning)."""
    raw = os.getenv("LIBRARY_DIRS", "")
    candidates = [d.strip() for d in raw.split(';') if d.strip()]
    if DEST_DIR:
        candidates.append(DEST_DIR)

    seen, resolved = set(), []
    for d in candidates:
        nd = os.path.normpath(d)
        key = os.path.normcase(nd)
        if key in seen:
            continue
        seen.add(key)
        if not os.path.isdir(nd):
            logging.warning(f"Library dir not found, skipping: {nd}")
            print(f"WARNING: library dir not found, skipping: {nd}")
            continue
        resolved.append(nd)
    return resolved


def build_movie_index(roots):
    """Name-only index over the top level of every root. No ffprobe here."""
    index = defaultdict(list)
    for root in roots:
        try:
            entries = os.listdir(root)
        except Exception as e:
            logging.warning(f"Could not list {root}: {e}")
            continue

        for entry in entries:
            if entry.startswith('.'):        # skips .deleted and other hidden dirs
                continue
            path = os.path.join(root, entry)

            # Loose video file sitting directly in a root
            if os.path.isfile(path):
                if os.path.splitext(entry)[1].lower() in VIDEO_EXTENSIONS:
                    pf = parse_movie_name(entry)
                    if pf:
                        key = (norm_title(pf['title']), norm_edition(pf['edition']))
                        index[key].append({'path': path, 'year': pf['year'],
                                           'root': root, 'edition': pf['edition']})
                continue

            if not os.path.isdir(path):
                continue

            parsed_folder = parse_movie_name(entry)
            if not parsed_folder:
                continue

            # Index each video file inside the folder (editions can coexist here)
            try:
                inner = os.listdir(path)
            except Exception:
                continue
            for f in inner:
                fp = os.path.join(path, f)
                if not os.path.isfile(fp):
                    continue
                if os.path.splitext(f)[1].lower() not in VIDEO_EXTENSIONS:
                    continue
                pf = parse_movie_name(f)
                file_edition = pf['edition'] if (pf and pf['edition']) else parsed_folder['edition']
                key = (norm_title(parsed_folder['title']), norm_edition(file_edition))
                index[key].append({'path': fp, 'year': parsed_folder['year'],
                                   'root': root, 'edition': file_edition})
    return index


def find_candidates(title, year, edition):
    """
    Return (candidates, ambiguous).
    - Editions must match (different editions are distinct keepers).
    - year present: match same year OR yearless existing copies.
    - year absent (permissive): match all in bucket; ambiguous if they span >1 known year.
    """
    bucket = MOVIE_INDEX.get((norm_title(title), norm_edition(edition)), [])
    if not bucket:
        return [], False

    if year:
        cands = [e for e in bucket if e['year'] == year or e['year'] is None]
        return cands, False
    else:
        distinct_years = {e['year'] for e in bucket if e['year']}
        return list(bucket), (len(distinct_years) > 1)


def scan_target_folder(clean_folder, edition):
    """Live scan of DEST_DIR/clean_folder for same-edition videos (catches same-run additions)."""
    folder = os.path.join(DEST_DIR, clean_folder)
    out = []
    if not os.path.isdir(folder):
        return out
    for f in os.listdir(folder):
        fp = os.path.join(folder, f)
        if os.path.isfile(fp) and os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
            pf = parse_movie_name(f)
            fed = pf['edition'] if (pf and pf['edition']) else None
            if norm_edition(fed) == norm_edition(edition):
                out.append({'path': fp, 'year': None, 'root': DEST_DIR, 'edition': fed})
    return out


# --------------------------------------------------------------------------
# QUALITY (ffprobe)
# --------------------------------------------------------------------------
def get_video_quality(file_path):
    """Extract video quality metrics (incl. duration) using ffprobe, with caching."""
    if file_path in FFPROBE_CACHE:
        return FFPROBE_CACHE[file_path]

    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', file_path],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        video_stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
        if not video_stream:
            return None

        width = int(video_stream.get('width', 0))
        height = int(video_stream.get('height', 0))
        bitrate = int(video_stream.get('bit_rate') or data.get('format', {}).get('bit_rate') or 0)

        # Duration for the safety net: prefer format duration, fall back to the stream's
        try:
            duration = float(data.get('format', {}).get('duration')
                             or video_stream.get('duration') or 0)
        except (TypeError, ValueError):
            duration = 0.0

        quality = {
            'resolution': width * height,
            'bitrate': bitrate,
            'file_size': os.path.getsize(file_path),
            'width': width,
            'height': height,
            'duration': duration,
        }
        FFPROBE_CACHE[file_path] = quality
        return quality
    except Exception as e:
        logging.warning(f"Could not analyze video quality for {file_path}: {e}")
        return None


def compare_quality(new_file, existing_file):
    """Compare two video files. Returns (is_incoming_better_or_equal, reason)."""
    new_q = get_video_quality(new_file)
    existing_q = get_video_quality(existing_file)

    if not new_q and not existing_q: return True, "Could not analyze quality"
    if not existing_q: return True, "Existing file unanalyzable"
    if not new_q: return False, "New file unanalyzable"

    if new_q['resolution'] > existing_q['resolution']:
        return True, f"Higher resolution ({new_q['width']}x{new_q['height']})"
    elif new_q['resolution'] < existing_q['resolution']:
        return False, f"Lower resolution ({existing_q['width']}x{existing_q['height']})"

    if new_q['bitrate'] > 0 and existing_q['bitrate'] > 0:
        diff = (new_q['bitrate'] - existing_q['bitrate']) / existing_q['bitrate'] * 100
        if diff > 10: return True, "Higher bitrate"
        if diff < -10: return False, "Lower bitrate"

    size_diff = (new_q['file_size'] - existing_q['file_size']) / existing_q['file_size'] * 100
    if size_diff > 15: return True, "Larger file size"
    if size_diff < -15: return False, "Smaller file size"

    return True, "Similar quality, using newer"


def duration_conflict(new_file, existing_file):
    """True if both durations are known and differ by more than the tolerance."""
    nq = get_video_quality(new_file)
    eq = get_video_quality(existing_file)
    if not nq or not eq:
        return False
    if nq.get('duration') and eq.get('duration'):
        return abs(nq['duration'] - eq['duration']) > (DURATION_TOLERANCE_MIN * 60)
    return False


# --------------------------------------------------------------------------
# SOFT DELETE (.deleted) + SWEEP
# --------------------------------------------------------------------------
# Recognizes the timestamped subfolders this script creates: <label>_YYYYMMDD_HHMMSS[_hex]
SWEEP_RE = re.compile(r'_(\d{8})_(\d{6})(?:_[0-9a-fA-F]+)?$')


def move_to_deleted(src_path, drive_root, label):
    """Move a file or folder into <drive_root>/.deleted/<label>_<timestamp>_<uid>/ (same drive => fast rename)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:6]
    subfolder = os.path.join(drive_root, DELETED_FOLDER_NAME,
                             f"{sanitize_component(label)}_{ts}_{uid}")
    os.makedirs(subfolder, exist_ok=True)
    dest = os.path.join(subfolder, os.path.basename(src_path.rstrip('/\\')))
    shutil.move(src_path, dest)
    return dest


def sweep_deleted(roots, retention_days):
    """Permanently purge timestamped subfolders older than retention. Only touches folders we recognize."""
    cutoff = datetime.now() - timedelta(days=retention_days)
    purged = 0
    for root in roots:
        ddir = os.path.join(root, DELETED_FOLDER_NAME)
        if not os.path.isdir(ddir):
            continue
        for entry in os.listdir(ddir):
            p = os.path.join(ddir, entry)
            if not os.path.isdir(p):
                continue
            m = SWEEP_RE.search(entry)
            if not m:
                logging.info(f"Sweep: leaving unrecognized item in {ddir}: {entry}")
                continue
            try:
                created = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            except ValueError:
                logging.info(f"Sweep: unparseable timestamp, leaving: {p}")
                continue
            if created < cutoff:
                try:
                    shutil.rmtree(p)
                    purged += 1
                    logging.info(f"Sweep: purged expired {p}")
                except Exception as e:
                    logging.warning(f"Sweep: failed to purge {p}: {e}")
    if purged:
        print(f"Cleanup: purged {purged} expired item(s) from .deleted folders.")


# --------------------------------------------------------------------------
# PROCESSING
# --------------------------------------------------------------------------
def process_single_item(path):
    """Processes a single folder or file from start to finish."""
    item_name = os.path.basename(path)
    parsed = parse_movie_name(item_name)

    if not parsed:
        logging.warning(f"Skipped (no match): {item_name}")
        return item_name, "skipped"

    title, year = parsed['title'], parsed['year']
    clean_folder = display_folder_name(title, year)

    # 1. Gather files and locate the main source video upfront
    source_files = []
    main_source_video = None

    if os.path.isfile(path):
        if os.path.splitext(item_name)[1].lower() in VIDEO_EXTENSIONS:
            main_source_video = path
            source_files.append((path, ""))
    else:
        for root, _, files in os.walk(path):
            rel_path = os.path.relpath(root, path)
            rel_path = "" if rel_path == "." else rel_path
            for file in files:
                f_path = os.path.join(root, file)
                ext = os.path.splitext(file)[1].lower()
                if ext in KEEP_EXTENSIONS:
                    source_files.append((f_path, rel_path))
                    if not main_source_video and not rel_path and ext in VIDEO_EXTENSIONS:
                        main_source_video = f_path

    if not main_source_video and not source_files:
        return clean_folder, "skipped"

    # Edition: prefer the main video's own tag/keywords, fall back to the item name's
    edition = parsed['edition']
    if main_source_video:
        mv = parse_movie_name(os.path.basename(main_source_video))
        if mv and mv['edition']:
            edition = mv['edition']

    # Serialize per destination movie so parallel workers can't race on the same title.
    lock_key = os.path.normcase(clean_folder + "|" + norm_edition(edition))
    with get_movie_lock(lock_key):

        # 2. Find existing copies of THIS edition across all libraries (name-only)
        candidates, ambiguous = find_candidates(title, year, edition)
        # Merge in a live scan of the destination folder (same-run additions)
        seen_paths = {os.path.normcase(c['path']) for c in candidates}
        for extra in scan_target_folder(clean_folder, edition):
            if os.path.normcase(extra['path']) not in seen_paths:
                candidates.append(extra)
                seen_paths.add(os.path.normcase(extra['path']))

        if ambiguous:
            logging.info(f"Ambiguous yearless match for '{title}' — matches multiple years. "
                         f"Skipping for manual review: {item_name}")
            return clean_folder, "skipped_review"

        # 3. Quality decision (only against matched candidates)
        if candidates and main_source_video:
            # Duration safety net first: a same-name copy with a very different runtime
            # is probably a different cut. Bail to manual review rather than guess.
            for cand in candidates:
                if duration_conflict(main_source_video, cand['path']):
                    logging.info(f"Duration mismatch for '{clean_folder}' vs "
                                 f"{os.path.basename(cand['path'])} — likely different cuts. "
                                 f"Skipping for manual review.")
                    return clean_folder, "skipped_review"

            # Incoming is the keeper only if it beats or ties EVERY existing copy
            incoming_wins = True
            lose_reason = ""
            for cand in candidates:
                is_better, reason = compare_quality(main_source_video, cand['path'])
                if not is_better:
                    incoming_wins = False
                    lose_reason = reason
                    break

            if not incoming_wins:
                # Incoming is the duplicate -> soft-delete it on the SOURCE drive
                try:
                    move_to_deleted(path, SOURCE_DIR, clean_folder)
                    logging.info(f"Incoming is lower quality ({lose_reason}); moved to .deleted: {item_name}")
                except Exception as e:
                    logging.warning(f"Failed to move lower-quality incoming to .deleted {item_name}: {e}")
                return clean_folder, "skipped_quality"

            # Incoming wins -> soft-delete every inferior existing copy on its OWN drive
            for cand in candidates:
                try:
                    move_to_deleted(cand['path'], cand['root'], clean_folder)
                    if cand['path'] in FFPROBE_CACHE:
                        del FFPROBE_CACHE[cand['path']]
                    logging.info(f"Superseded existing copy moved to .deleted: {cand['path']}")
                except Exception as e:
                    logging.warning(f"Could not move existing copy {cand['path']} to .deleted: {e}")

        # 4. Batch transfer the incoming files into DEST_DIR/clean_folder
        dest_folder = os.path.join(DEST_DIR, clean_folder)
        os.makedirs(dest_folder, exist_ok=True)
        files_moved = 0

        for f_path, rel_path in source_files:
            filename = os.path.basename(f_path)
            ext = os.path.splitext(filename)[1].lower()

            if rel_path and any(folder in rel_path.lower() for folder in SPECIAL_FOLDERS):
                target_dir = os.path.join(dest_folder, rel_path)
                new_filename = filename
            else:
                target_dir = dest_folder
                if f_path == main_source_video and ext in VIDEO_EXTENSIONS:
                    # Main video gets the clean name, with an {edition-...} tag when applicable
                    if edition:
                        new_filename = f"{clean_folder} {{edition-{edition}}}{ext}"
                    else:
                        new_filename = f"{clean_folder}{ext}"
                else:
                    # Other root-level files keep their name (avoids edition collisions)
                    new_filename = filename

            os.makedirs(target_dir, exist_ok=True)
            dest_file = os.path.join(target_dir, new_filename)

            try:
                if not os.path.exists(dest_file):
                    shutil.move(f_path, dest_file)
                    files_moved += 1
            except Exception as e:
                logging.error(f"Error moving {filename}: {e}")

        # Cleanup the (now-empty) source tracking folder/file
        if os.path.isdir(path):
            try: shutil.rmtree(path)
            except: pass
        elif os.path.isfile(path) and os.path.exists(path):
            try: os.remove(path)
            except: pass

    return clean_folder, "processed" if files_moved > 0 else "skipped"


def process_movies():
    logging.info("Starting optimized movie organization...")
    print("Starting movie organization...")

    if not SOURCE_DIR or not os.path.exists(SOURCE_DIR):
        print(f"ERROR: Source directory not found: {SOURCE_DIR}")
        return
    if not DEST_DIR:
        print("ERROR: DEST_DIR is not set.")
        return

    # Resolve all library locations (DEST_DIR folded in, missing drives skipped)
    library_roots = resolve_library_dirs()
    print(f"Checking {len(library_roots)} library location(s) for existing copies.")

    # Sweep expired soft-deletes first — across every library root AND the source drive
    sweep_roots = list(library_roots)
    if os.path.normcase(os.path.normpath(SOURCE_DIR)) not in {os.path.normcase(r) for r in library_roots}:
        sweep_roots.append(os.path.normpath(SOURCE_DIR))
    print(f"Cleaning up .deleted items older than {DELETED_RETENTION_DAYS} days...")
    sweep_deleted(sweep_roots, DELETED_RETENTION_DAYS)

    # Build the name-only match index once (no ffprobe)
    global MOVIE_INDEX
    MOVIE_INDEX = build_movie_index(library_roots)

    items = [os.path.join(SOURCE_DIR, i) for i in os.listdir(SOURCE_DIR)
             if not i.startswith('.')]   # ignore the source .deleted folder
    if not items:
        print("No items found.")
        return

    print(f"Found {len(items)} item(s) to process via {MAX_WORKERS} parallel workers.\n")

    processed = skipped = q_skipped = review = 0
    total = len(items)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_single_item, item): item for item in items}

        for idx, future in enumerate(as_completed(futures), 1):
            try:
                name, status = future.result()
                if status == "processed": processed += 1
                elif status == "skipped_quality": q_skipped += 1
                elif status == "skipped_review": review += 1
                else: skipped += 1

                sys.stdout.write(f"\rProgress: {idx}/{total} ({int(idx/total*100)}%) | Processing: {name[:30]}")
                sys.stdout.flush()
            except Exception as e:
                logging.error(f"Worker generated an exception: {e}")

    print(f"\n\nComplete! Processed: {processed} | Skipped: {skipped} | "
          f"Lower Quality Dropped: {q_skipped} | Needs Review: {review}")


if __name__ == "__main__":
    process_movies()
    os._exit(0)