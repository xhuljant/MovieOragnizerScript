import os
import re
import shutil
import logging
import sys
import subprocess
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# --- CONFIG ---
load_dotenv()  # reads the .env file in the current directory

SOURCE_DIR = os.getenv("SOURCE_DIR")
DEST_DIR = os.getenv("DEST_DIR")
LOG_DIR = os.getenv("LOG_DIR", os.path.dirname(__file__))
LOG_FILE = os.path.join(LOG_DIR, "movie_organizer.log")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "1"))  # Adjust based on drive speeds (keep low for HDDs, can raise to 8+ for NVMe/SSDs)

# --- LOGGING ---
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.m2ts'}
KEEP_EXTENSIONS = VIDEO_EXTENSIONS | {'.srt', '.sub', '.idx', '.ass', '.ssa', '.vtt'}
SPECIAL_FOLDERS = {'extra', 'extras', 'special', 'specials', 'feature', 'features', 'bonus', 'deleted', 'behind', 'interview', 'trailer', 'trailers'}

# Cache ffprobe results in-memory during execution to prevent analyzing the same destination file repeatedly
FFPROBE_CACHE = {}

def get_video_quality(file_path):
    """Extract video quality metrics using ffprobe with caching."""
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
        
        quality = {
            'resolution': width * height,
            'bitrate': bitrate,
            'file_size': os.path.getsize(file_path),
            'width': width,
            'height': height
        }
        FFPROBE_CACHE[file_path] = quality
        return quality
    except Exception as e:
        logging.warning(f"Could not analyze video quality for {file_path}: {e}")
        return None

def compare_quality(new_file, existing_file):
    """Compare two video files. Returns: (is_better, reason)"""
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

def find_existing_movie(dest_folder):
    """Find existing movie file inside target folder."""
    if not os.path.exists(dest_folder):
        return None
    for file in os.listdir(dest_folder):
        file_path = os.path.join(dest_folder, file)
        if os.path.isfile(file_path) and os.path.splitext(file)[1].lower() in VIDEO_EXTENSIONS:
            return file_path
    return None

def extract_title_year(name):
    """Clean and match titles."""
    name = re.sub(r'^[\w\s]+[\s.](org|com|net|mx|to|gg|xyz|info)\s*[-–]\s*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'^www\s+[\w\s]+\s*[-–]\s*', '', name, flags=re.IGNORECASE)
    match = re.search(r"(.+?)[\s._-]*\(?(\d{4})\)?", name)
    if not match: return None
    
    title = match.group(1)
    year = match.group(2)
    title = re.sub(r"[._]", " ", title).strip()
    title = re.sub(r"\b(1080p|720p|480p|60fps|10bit|WEB-?DL|BluRay|HDTV|x264|x265|HEVC|AAC|MP3|AC3|DTS|DD5\.?1|REPACK|PROPER).*$", "", title, flags=re.IGNORECASE)
    return f"{re.sub(r'\s{2,}', ' ', title).strip().title()} ({year})"

def process_single_item(path):
    """Processes a single folder or file from start to finish."""
    item_name = os.path.basename(path)
    clean_name = extract_title_year(item_name)
    
    if not clean_name:
        logging.warning(f"Skipped (no match): {item_name}")
        return item_name, "skipped"
        
    dest_folder = os.path.join(DEST_DIR, clean_name)
    existing_movie = find_existing_movie(dest_folder)
    
    # 1. Gather files and locate source video upfront
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
        return clean_name, "skipped"

    # 2. Perform ONE quality check for the entire item
    if existing_movie and main_source_video:
        is_better, reason = compare_quality(main_source_video, existing_movie)
        if not is_better:
            try:
                if os.path.isfile(path): os.remove(path)
                else: shutil.rmtree(path)
                logging.info(f"Deleted lower quality source: {item_name} - {reason}")
            except Exception as e:
                logging.warning(f"Failed to delete lower quality source {item_name}: {e}")
            return clean_name, "skipped_quality"
        else:
            try:
                os.remove(existing_movie)
                if existing_movie in FFPROBE_CACHE: del FFPROBE_CACHE[existing_movie]
                logging.info(f"Deleted old lower quality file: {os.path.basename(existing_movie)}")
            except Exception as e:
                logging.warning(f"Could not delete old file {existing_movie}: {e}")

    # 3. Batch transfer files
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
            new_filename = f"{clean_name}{ext}" if ext in VIDEO_EXTENSIONS and not rel_path else filename
            
        os.makedirs(target_dir, exist_ok=True)
        dest_file = os.path.join(target_dir, new_filename)
        
        try:
            if not os.path.exists(dest_file):
                shutil.move(f_path, dest_file)
                files_moved += 1
        except Exception as e:
            logging.error(f"Error moving {filename}: {e}")

    # Cleanup source tracking folder
    if os.path.isdir(path):
        try: shutil.rmtree(path)
        except: pass
    elif os.path.isfile(path) and os.path.exists(path):
        try: os.remove(path)
        except: pass

    return clean_name, "processed" if files_moved > 0 else "skipped"

def process_movies():
    logging.info("Starting optimized movie organization...")
    print("Starting movie organization...")
    
    if not os.path.exists(SOURCE_DIR):
        print(f"ERROR: Source directory not found: {SOURCE_DIR}")
        return
        
    items = [os.path.join(SOURCE_DIR, i) for i in os.listdir(SOURCE_DIR)]
    if not items:
        print("No items found.")
        return
        
    print(f"Found {len(items)} item(s) to process via {MAX_WORKERS} parallel workers.\n")
    
    processed = skipped = q_skipped = 0
    total = len(items)
    
    # Use ThreadPoolExecutor to crunch through network/disk tasks concurrently
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_single_item, item): item for item in items}
        
        for idx, future in enumerate(as_completed(futures), 1):
            try:
                name, status = future.result()
                if status == "processed": processed += 1
                elif status == "skipped_quality": q_skipped += 1
                else: skipped += 1
                
                # Simplified tracking prints nicely across parallel tasks
                sys.stdout.write(f"\rProgress: {idx}/{total} ({int(idx/total*100)}%) | Processing: {name[:30]}")
                sys.stdout.flush()
            except Exception as e:
                logging.error(f"Worker generated an exception: {e}")

    print(f"\n\nComplete! Processed: {processed} | Skipped: {skipped} | Lower Quality Dropped: {q_skipped}")

if __name__ == "__main__":
    process_movies()
    os._exit(0)