import os
import time
import json
import random
import threading
import mimetypes
import yt_dlp
import tempfile
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory, Response, redirect
from werkzeug.utils import secure_filename
from mutagen import File as MutagenFile

app = Flask(__name__)

# Configuration
# "Cloud Amnesia" Fix: Check for persistent disk mount
STORAGE_DIR = os.environ.get('STORAGE_DIR', '/var/lib/grace_radio')
if not os.path.exists(STORAGE_DIR):
    # Fallback to local 'static/media' if no disk mounted (Development/First Run)
    STORAGE_DIR = 'static/media' # Backward compatibility for local files
    # Actually, we need separation. 
    # Logic:
    # 1. System tries to read from STORAGE_DIR for *Dynamic* content.
    # 2. But we also have "Built-in" content in 'static/media'.
    # We should probably combine them or serve from both.
    # Simpler: Just set UPLOAD_FOLDER to the storage dir.
    
# If on Render and Disk is mounted, STORAGE_DIR will exist.
# But for local dev (Windows), it won't.
if os.name == 'nt': # Windows
    STORAGE_DIR = os.path.join(app.root_path, 'static', 'media')
    print(f"Running on Windows (Local Dev). Using {STORAGE_DIR}")
else:
    # Linux (Render) -> Check if mount exists, else fallback
    # DEBUG: Print what we see
    if os.path.exists('/var/lib/grace_radio'):
        STORAGE_DIR = '/var/lib/grace_radio'
        print(f"USING PERSISTENT DISK: {STORAGE_DIR}")
        # Test write permission
        try:
            with open(os.path.join(STORAGE_DIR, 'write_test.txt'), 'w') as f:
                f.write('ok')
            print("Write test successful.")
        except Exception as e:
            print(f"WRITE TEST FAILED: {e}")
            # Fallback if we can't write, otherwise we crash
            STORAGE_DIR = 'static/media' 
    else:
        print("NO PERSISTENT DISK FOUND. Using static/media (Ephemeral)")
        STORAGE_DIR = 'static/media'

UPLOAD_FOLDER = STORAGE_DIR
DATA_FILE = os.path.join(STORAGE_DIR, 'data.json')
STATE_FILE = os.path.join(STORAGE_DIR, 'state.json')
VOTE_FILE = os.path.join(STORAGE_DIR, 'votes.json')
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'ogg', 'm4a', 'mp4', 'webm'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# Ensure directory exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
print(f"FINAL UPLOAD_FOLDER: {app.config['UPLOAD_FOLDER']}")

# Global State (In-Memory Cache)
state = {
    "library": [],        # List of media objects: {id, title, filename, duration, type, category}
    "queue": [],          # List of media IDs to play next (User manual queue)
    "schedule": [],       # List of {id, run_at_timestamp, media_id}
    "history": [],        # IDs of played songs
    "votes": [],          # List of {track_id, timestamp, vote}
    "deleted_files": [],  # BLOCKLIST: Filenames that have been explicitly deleted
    "current_track": None, # { ...media_obj, start_time: timestamp }
    "playing": False
}

state_lock = threading.Lock()

def extract_metadata(filepath, media_id):
    """
    Extracts duration and Album Art (ID3).
    Returns (duration, art_filename_or_None)
    """
    duration = 0
    art_path = None
    
    try:
        audio = MutagenFile(filepath)
        if audio is not None:
            if audio.info is not None:
                duration = audio.info.length
            
            # ID3 Art Extraction
            # Check for standard ID3 tags (APIC)
            found_art = None
            if hasattr(audio, 'tags') and audio.tags:
                # MP3/ID3
                for tag in audio.tags.values():
                    if tag.FrameID == 'APIC':
                        found_art = tag.data
                        break
                # FLAC/Ogg
                if not found_art and hasattr(audio, 'pictures'):
                     if audio.pictures:
                         found_art = audio.pictures[0].data

            if found_art:
                # Save to Persistent Storage
                art_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'art')
                if not os.path.exists(art_dir):
                    os.makedirs(art_dir)
                
                ext = '.jpg' # Default
                if found_art[0:4] == b'\x89PNG': ext = '.png'
                
                art_filename = f"{media_id}{ext}"
                dest = os.path.join(art_dir, art_filename)
                
                # Write only if doesn't exist (to preserve custom uploads?)
                # Actually, during bootstrap, we might respect existing.
                if not os.path.exists(dest):
                    with open(dest, 'wb') as f:
                        f.write(found_art)
                    print(f"Extracted Art for {media_id}")
                    art_path = f"/static/art/{art_filename}"
                else:
                    art_path = f"/static/art/{art_filename}"

    except Exception as e:
        print(f"Error reading metadata: {e}")
        
    return duration, art_path

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def load_data():
    loaded_from_disk = False
    # 1. Load persistent data (Library, Schedule)
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                state['library'] = data.get('library', [])
                state['schedule'] = data.get('schedule', [])
                state['deleted_files'] = data.get('deleted_files', [])
                loaded_from_disk = True
                
                # Cleanup Duplicates (Root vs Folder)
                clean_lib = []
                folder_basenames = {os.path.basename(x['filename']) for x in state['library'] if '/' in x['filename'].replace('\\', '/')}
                
                removed_dupes = 0
                for item in state['library']:
                    bn = os.path.basename(item['filename'])
                    is_root = '/' not in item['filename'].replace('\\', '/')
                    # If this is a root item, and we have a version in a folder, drop the root one
                    if is_root and bn in folder_basenames:
                        removed_dupes += 1
                        continue
                    clean_lib.append(item)
                
                if removed_dupes > 0:
                    print(f"Auto-cleaned {removed_dupes} duplicate root items.")
                    state['library'] = clean_lib
                    save_data() # Persist cleanup

                # Metadata Debug
                stat = os.stat(DATA_FILE)
                mtime = time.ctime(stat.st_mtime)
                print(f"LOADED {len(state['library'])} items from {DATA_FILE} (Last Mod: {mtime})")
        except Exception as e:
            print(f"ERROR LOADING DATA_FILE {DATA_FILE}: {e}")
            
    # BOOTSTRAP logic...
    # We want to ensure we at least have the bundled music.
    # But checking 'not state["library"]' acts as the trigger.
    
    # Check bundled content
    local_static = os.path.join(app.root_path, 'static', 'media')
    
    # We should ALWAYS check for bundled content to add any "Hardcoded" songs that might be missing from DB
    if os.path.exists(local_static):
        added_count = 0
        for filename in os.listdir(local_static):
            if filename in state.get('deleted_files', []):
                 continue

            if allowed_file(filename):
                # Check if already in library (by filename)
                # IMPORTANT: Use string comparison
                # Check if already in library (by filename or basename)
                if any(os.path.basename(m['filename']) == filename for m in state['library']):
                    continue
                    
                filepath = os.path.join(local_static, filename)
                # Generate ID first so we can use it for art
                mid = str(int(time.time()*1000) + random.randint(1,999))
                duration, art = extract_metadata(filepath, mid)
                
                media_item = {
                    "id": mid,
                    "title": os.path.splitext(filename)[0].replace('_', ' '),
                    "filename": filename,
                    "duration": duration,
                    "art": art,  # New Field
                    "category": "Music", # Default to Music for bootstrap
                    "type": "audio",
                    "added_at": time.time()
                }
                state['library'].append(media_item)
                added_count += 1
        
        if added_count > 0:
            print(f"Bootstrapped/Merged {added_count} items from bundled static/media")
            save_data()
    
    # 2. Load volatile state (Current Track)
    if os.path.exists(STATE_FILE):
        try:
             with open(STATE_FILE, 'r') as f:
                s_data = json.load(f)
                state['current_track'] = s_data.get('current_track')
                state['playing'] = s_data.get('playing', False)
                
                # Restore queue if validity checks pass
                q = s_data.get('queue', [])
                # Filter strict string
                state['queue'] = [str(x) for x in q]
        except: pass

    # 3. Load votes
    if os.path.exists(VOTE_FILE):
        try:
            with open(VOTE_FILE, 'r') as f:
                state['votes'] = json.load(f)
        except Exception as e:
            print(f"Error loading votes: {e}")
            state['votes'] = []

def save_data():
    # Save persistent data
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump({
                "library": state['library'],
                "schedule": state['schedule'],
                "deleted_files": state['deleted_files']
            }, f, indent=2)
            f.flush()
            os.fsync(f.fileno()) # FORCE WRITE TO DISK
            
        print(f"saved data to {DATA_FILE}: {len(state['library'])} items")
    except Exception as e:
        print(f"Error saving data: {e}")

def save_votes():
    try:
        with open(VOTE_FILE, 'w') as f:
            json.dump(state['votes'], f)
    except Exception as e:
        print(f"Error saving votes: {e}")

def save_state():
    # Save volatile state separate for fast writes
    with open(STATE_FILE, 'w') as f:
        json.dump({
            "current_track": state['current_track'],
            "playing": state['playing'],
            "queue": state['queue']
        }, f)

load_data()



# --- Singleton Management ---
LOCK_FILE = os.path.join(tempfile.gettempdir(), 'radio_heartbeat.lock')

def acquire_lock():
    try:
        # Check if lock exists
        if os.path.exists(LOCK_FILE):
             mtime = os.path.getmtime(LOCK_FILE)
             is_fresh = (time.time() - mtime < 10)
             
             if is_fresh:
                 # It is fresh. Is it ME?
                 try:
                     with open(LOCK_FILE, 'r') as f:
                         pid = f.read().strip()
                     if pid == str(os.getpid()):
                         # It is ME. I own it.
                         # Update timestamp
                         os.utime(LOCK_FILE, None)
                         return True
                     else:
                         # It is someone else. Back off.
                         return False
                 except: 
                     # Read failed? Assume active.
                     return False
        
        # Lock is old or missing. Take it.
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
        return True
    except:
        return False

def update_heartbeat():
    # Deprecated/Redundant given acquire_lock updates it, 
    # but kept for safety if used elsewhere
    try:
        if os.path.exists(LOCK_FILE):
             os.utime(LOCK_FILE, None)
    except: pass

# --- Thread Management ---
radio_thread = None

# Listener Tracking
listeners = {} # {ip: timestamp}
listener_lock = threading.Lock()

def update_listeners(ip):
    with listener_lock:
        listeners[ip] = time.time()

def get_active_listeners():
    with listener_lock:
        cutoff = time.time() - 30 # Active in last 30s
        # Prune
        bad = [k for k,v in listeners.items() if v < cutoff]
        for k in bad:
            listeners.pop(k, None)
        return len(listeners)

def radio_loop():
    print(f"--- Radio Loop Started (PID: {os.getpid()}) ---")
    
    # Simple file logger function
    def log_loop(msg):
        try:
            with open("loop_debug.log", "a") as f:
                f.write(f"{time.ctime()}: {msg}\n")
        except: pass

    log_loop("Loop initialized.")
    with state_lock:
        log_loop(f"Library size: {len(state.get('library', []))}")

    while True:
        # Ghost Thread Check: Am I the official thread?
        # Note: We need a small delay on startup to allow the global var to be set
        if radio_thread and radio_thread != threading.current_thread():
             log_loop("I am a GHOST thread (replaced). Exiting.")
             break

        try:
            time.sleep(1.0) # Tick
            now = time.time()
            
            with state_lock:
                # --- Queue Maintenance (Inline to ensure execution) ---
                target_len = 10
                q_len = len(state['queue'])
                if q_len < target_len:
                    needed = target_len - q_len
                    # Candidates
                    music = [m for m in state['library'] if m.get('category') == 'Music']
                    if music:
                        hist = set(state['history'])
                        q_set = set(state['queue'])
                        added_count = 0
                        for _ in range(needed):
                            # Try unplayed
                            cands = [m for m in music if m['id'] not in hist and str(m['id']) not in q_set]
                            if not cands: cands = [m for m in music if str(m['id']) not in q_set]
                            if not cands: break
                            
                            pick = random.choice(cands)
                            state['queue'].append(str(pick['id']))
                            q_set.add(str(pick['id']))
                            added_count += 1
                        
                        if added_count > 0:
                            save_data()
                            # log_loop(f"Refilled queue with {added_count} items")
                
                # Check for Hot Reload
                try:
                    if os.path.exists(DATA_FILE):
                        stat = os.stat(DATA_FILE)
                        if stat.st_mtime > state.get('last_disk_read', 0):
                            print(f"DISK CHANGE DETECTED. Reloading library... (Old: {len(state['library'])})")
                            with open(DATA_FILE, 'r') as f:
                                data = json.load(f)
                                # Only update if valid
                                if 'library' in data:
                                    state['library'] = data.get('library', [])
                                    state['schedule'] = data.get('schedule', [])
                                    state['last_disk_read'] = stat.st_mtime
                                    print(f"RELOAD COMPLETE. New size: {len(state['library'])}")
                                    # Fix Queue Type Mismatches immediately after reload
                                    state['queue'] = [str(x) for x in state['queue']]
                except Exception as e:
                    print(f"HOT RELOAD FAILED: {e}")
                # ------------------------------------------

                current = state['current_track']
                
                # --- Cleanup ---
                to_remove = []
                for item in state['library']:
                    if item.get('category') == 'Temporary' and item.get('added_at'):
                        if now - item.get('added_at') > 86400:
                            to_remove.append(item)
                
                for item in to_remove:
                    try:
                        os.remove(os.path.join(app.config['UPLOAD_FOLDER'], item['filename']))
                    except: pass
                    if item in state['library']:
                        state['library'].remove(item)
                
                if to_remove:
                    save_data()

                # --- Playback Decision ---
                should_pick = False
                
                if not current:
                    should_pick = True
                    log_loop("Picking: Current is None")
                else:
                    dur = current.get('duration', 1)
                    if not isinstance(dur, (int, float)) or dur <= 0: dur = 10
                    
                    elapsed = now - current['start_time']
                    
                    # Normal Finish
                    if elapsed >= dur + 1: # 1s buffer
                        should_pick = True
                        log_loop(f"Picking: Track Finished ({elapsed:.1f}s / {dur}s)")
                    
                    # Overdue Failsafe (Safety Net)
                    elif elapsed > (dur + 10):
                        should_pick = True
                        log_loop(f"Picking: Track OVERDUE Force Skip ({elapsed:.1f}s / {dur}s)")

                    if should_pick:
                        # Update Last Played
                        lib_item = next((m for m in state['library'] if m['id'] == current['id']), None)
                        if lib_item:
                            lib_item['last_played_at'] = now
                            save_data()

                if should_pick:
                    # SYNC: Read fresh queue from disk before deciding
                    # We need to be careful not to overwrite 'current_track' if playing
                    # Just read 'queue' from state.json
                    try:
                        if os.path.exists(STATE_FILE):
                             with open(STATE_FILE, 'r') as f:
                                 s_data = json.load(f)
                                 # Merge external queue additions
                                 disk_queue = s_data.get('queue', [])
                                 # If disk queue has items and local doesn't, or different, take disk
                                 # Simplest: Trust disk fully for queue
                                 state['queue'] = disk_queue
                    except: pass

                    next_media = None

                    # 1. Schedule
                    state['schedule'].sort(key=lambda x: x['run_at'])
                    due_idx = -1
                    for i, item in enumerate(state['schedule']):
                        if item['run_at'] <= now:
                            due_idx = i
                            break
                    if due_idx != -1:
                        item = state['schedule'].pop(due_idx)
                        log_loop(f"Processing SCHEDULED Item: {item['media_id']} (Due: {item['run_at']})")
                        
                        media = next((m for m in state['library'] if str(m['id']) == str(item['media_id'])), None)
                        if media:
                            next_media = media
                            log_loop(f"Selected SCHEDULED: {media['title']}")
                        else:
                            log_loop(f"ERROR: Scheduled media {item['media_id']} NOT found in library. Skipped.")


                    # 2. Queue
                    log_loop(f"Checking Queue: {len(state['queue'])} items")
                    while not next_media and state['queue']:
                         media_id = state['queue'][0] # Peek first
                         log_loop(f"Peeking Queue ID: {media_id}")
                         
                         # Type safe check
                         media = next((m for m in state['library'] if str(m['id']) == str(media_id)), None)
                         
                         if media:
                             # Valid item found, consume it
                             state['queue'].pop(0)
                             next_media = media
                             log_loop(f"Selected QUEUED: {media['title']}")
                         else:
                             log_loop(f"Queue ID {media_id} NOT found in Library ({len(state['library'])} items).")
                             # Try reloading
                             # ... (omitted for brevity, assume disk reload if critical, or just drop)
                             # Let's just drop it to unblock
                             state['queue'].pop(0)
                             log_loop("Dropped invalid queue item.")


                     # 3. Shuffle
                     # 3. Shuffle
                    if not next_media:
                        # Filters
                        # User Request: Exclude 'Sermon' from auto-shuffle.
                        # Blocklist: 'Sermon', 'Temporary'
                        
                        blocklist = ['Sermon', 'Temporary']
                        candidates = [m for m in state['library'] if m.get('category') not in blocklist]
                        
                        # Priority 1: Unplayed Candidates (History)
                        history_set = set(state['history'])
                        final_cands = [m for m in candidates if m['id'] not in history_set]
                        
                        # Priority 2: Reset (Recycle all candidates)
                        if not final_cands:
                             final_cands = candidates
                        
                        # Priority 3: Fallback (Anything not temporary - e.g. if only Sermons exist)
                        if not final_cands:
                             final_cands = [m for m in state['library'] if m.get('category') != 'Temporary']

                        if final_cands:
                             next_media = random.choice(final_cands)
                             log_loop(f"Selected SHUFFLE: {next_media['title']}")
                        else:
                             log_loop("No candidates found in library!")

                    if next_media:
                        state['current_track'] = next_media.copy()
                        state['current_track']['start_time'] = time.time()
                        state['playing'] = True
                        
                        # Add to history
                        state['history'].append(next_media['id'])
                        
                        # Keep history large enough to cover most of the library
                        # e.g. 75% of library size, or max 50
                        max_hist = max(10, len(state['library']) - 5) 
                        if len(state['history']) > max_hist:
                             state['history'].pop(0)
                    else:
                        state['current_track'] = None
                        state['playing'] = False
                    
                    # Maintain Queue Depth (Run auto-fill)
                    ensure_queue_filled()

                    # Sync state to disk immediately
                    save_state()
                    save_data() # Save queue/schedule changes too

        except Exception as e:
            print(f"CRITICAL RADIO LOOP ERROR: {e}")
            try:
                with open("loop_debug.log", "a") as f:
                    f.write(f"CRASH: {e}\n")
            except: pass
            time.sleep(5)
            
        time.sleep(1)

# Thread management lock
thread_start_lock = threading.Lock()

def start_radio_thread():
    global radio_thread
    with thread_start_lock:
        if radio_thread is None or not radio_thread.is_alive():
            print("Starting Radio Loop Thread...")
            radio_thread = threading.Thread(target=radio_loop, daemon=True)
            radio_thread.start()

# Start initially
start_radio_thread()

# Watchdog: Check thread on every request
@app.before_request
def watchdog():
    start_radio_thread()

@app.route('/api/logs')
def get_logs():
    # Helper to view what the loop is doing
    try:
        with open("loop_debug.log", "r") as f:
            lines = f.readlines()
            return "<br>".join(lines[-50:]) # last 50 lines
    except:
        return "No logs"

@app.route('/api/debug/state')
def debug_state():
    with state_lock:
        return jsonify({
            "playing": state.get('playing'),
            "current_track": state.get('current_track'),
            "queue_len": len(state.get('queue', [])),
            "library_len": len(state.get('library', [])),
            "history_len": len(state.get('history', [])),
            "library_sample": [m['title'] for m in state.get('library', [])[:5]],
            "queue_dump": state.get('queue'),
            "thread_alive": radio_thread.is_alive() if radio_thread else False
        })

@app.route('/api/debug/logs')
def debug_logs():
    try:
        if os.path.exists("loop_debug.log"):
            with open("loop_debug.log", "r") as f:
                return "<pre>" + f.read() + "</pre>"
        return "No logs"
    except Exception as e:
        return str(e)

@app.route('/api/stream/current')
def stream_current():
    with state_lock:
        current = state.get('current_track')
        if current and state.get('playing'):
            # specific file URL
            return redirect(f"/static/media/{current['filename']}")
        else:
            # Fallback or silent mp3
            return "Radio Offline", 404

# --- Routes ---

@app.route('/')
def index():
    return render_template('index.html', is_admin=False)

@app.route('/admin')
def admin_dashboard():
    return render_template('index.html', is_admin=True)

@app.route('/api/status')
def get_status():
    # Update listener heartbeat
    # Update listener heartbeat
    # Only count valid clients with Listener ID (Filters bots)
    lid = request.headers.get('X-Listener-ID')
    if lid:
        update_listeners(lid)

    with state_lock:
        now = time.time()
        current = state['current_track']
        
        # Calculate elapsed
        elapsed = 0
        if current and state['playing']:
            elapsed = now - current['start_time']
            if elapsed < 0: elapsed = 0
        # Calculate elapsed
        elapsed = 0
        if current and state['playing']:
            elapsed = now - current['start_time']
            if elapsed < 0: elapsed = 0
            
        # Check if user voted on this track
        user_vote = None
        if current and lid:
            # Find vote by this listener for this track
            # Performance: Search list (OK for now, optimize with dict later if needed)
            v = next((x for x in state['votes'] if x['track_id'] == str(current['id']) and x.get('listener_id') == lid), None)
            if v:
                user_vote = v.get('rating')
                # Compat
                if user_vote is None:
                    if v.get('vote') == 'like': user_vote = 5
                    elif v.get('vote') == 'dislike': user_vote = 1
        queue_preview = []
        for q_id in state['queue'][:10]:
            # Find in library
            m = next((m for m in state['library'] if str(m['id']) == str(q_id)), None)
            if m:
                queue_preview.append({"id": m['id'], "title": m['title'], "category": m.get('category', 'Unknown')})
            else:
                queue_preview.append({"id": q_id, "title": "Loading...", "category": "Unknown"})

        return jsonify({
            "playing": state['playing'],
            "current_track": current,
            "elapsed": elapsed,
            "listeners": get_active_listeners(),
            "queue": queue_preview,
            "user_vote": user_vote,
            "server_time": now
        })

@app.route('/api/library', methods=['GET', 'POST'])
def library():
    if request.method == 'GET':
        return jsonify(state['library'])
    
    # POST - Add items is handled by upload mostly, but maybe editing metadata?
    return jsonify({"error": "Use upload"}), 400

@app.route('/api/library/folders')
def library_folders():
    """Returns list of unique folder paths used in library"""
    folders = set()
    with state_lock:
        for item in state['library']:
            fname = item.get('filename', '').replace('\\', '/')
            if '/' in fname:
                # Extract dir
                d = os.path.dirname(fname)
                if d and d != '.':
                    folders.add(d)
    return jsonify(sorted(list(folders)))

@app.route('/api/library/batch_move', methods=['POST'])
def batch_move():
    data = request.json
    ids = data.get('ids', [])
    target_folder = data.get('folder', '').strip()
    target_folder = secure_filename(target_folder)
    
    count = 0
    with state_lock:
        for mid in ids:
            item = next((m for m in state['library'] if str(m['id']) == str(mid)), None)
            if not item: continue
            
            old_filename = item['filename']
            base_name = os.path.basename(old_filename)
            
            if target_folder:
                new_filename = os.path.join(target_folder, base_name)
            else:
                new_filename = base_name # Root
            
            new_filename = new_filename.replace('\\', '/')
            
            if new_filename != old_filename:
                src_path = os.path.join(app.config['UPLOAD_FOLDER'], old_filename)
                dst_path = os.path.join(app.config['UPLOAD_FOLDER'], new_filename)
                
                # Check bundled fallback if not in persistent
                is_bundled = False
                if not os.path.exists(src_path):
                    bundled_path = os.path.join(app.root_path, 'static', 'media', old_filename)
                    if os.path.exists(bundled_path):
                         src_path = bundled_path
                         is_bundled = True
                
                try:
                    # Create dst dir
                    dst_dir = os.path.dirname(dst_path)
                    if dst_dir and not os.path.exists(dst_dir):
                        os.makedirs(dst_dir)
                    
                    if os.path.exists(src_path):
                        if is_bundled:
                             import shutil
                             shutil.copy2(src_path, dst_path) # Copy to persistent
                             # We can't delete bundled file, but DB now points to new path which is in UPLOAD
                        else:
                             os.rename(src_path, dst_path)
                             
                        item['filename'] = new_filename
                        count += 1
                    else:
                        print(f"Batch Move: Source not found for {old_filename}")
                        
                except Exception as e:
                    print(f"Batch Move Error {mid}: {e}")
        
        if count > 0:
            save_data()
            
    return jsonify({"status": "ok", "moved": count})
@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    files = request.files.getlist('file')
    category = request.form.get('category', 'Music') # Music, Sermon, Announcement
    uploaded_items = []

    for file in files:
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            print(f"DEBUG: Saving file to {path}") # LOGGING
            file.save(path)
            
            # Small sleep to ensure unique ID if multiple files uploaded instantly
            time.sleep(0.01)
            mid = str(int(time.time()*1000)) + str(random.randint(0,1000))
            
            duration, art = extract_metadata(path, mid)
            
            media_item = {
                "id": mid,
                "title": filename, 
                "filename": filename,
                "duration": duration,
                "art": art,
                "category": category,
                "type": "audio"
            }
            uploaded_items.append(media_item)

    if uploaded_items:
        with state_lock:
            state['library'].extend(uploaded_items)
            save_data()
            
            # VERIFY WRITE
            try:
                with open(DATA_FILE, 'r') as f:
                    verify_data = json.load(f)
                    verify_os = verify_data.get('library', [])
                    # Check if our new IDs are in there
                    for item in uploaded_items:
                        if not any(str(v['id']) == str(item['id']) for v in verify_os):
                            print(f"CRITICAL: Uploaded item {item['id']} NOT FOUND in disk after save!")
                            return jsonify({"error": "Disk Write Failed"}), 500
                    print(f"VERIFICATION SUCCESS: {len(verify_os)} items on disk.")
            except Exception as e:
                print(f"VERIFICATION ERROR: {e}")
                
        return jsonify(uploaded_items)
    
    return jsonify({'error': 'No valid files allowed'}), 400

@app.route('/api/debug')
def debug_info():
    with state_lock:
        s_copy = {k:v for k,v in state.items()}
    return jsonify({
        "pid": os.getpid(),
        "thread_alive": radio_thread.is_alive() if 'radio_thread' in globals() else False,
        "state": str(s_copy)[:500] # truncate
    })

@app.route('/api/danger/force_next', methods=['POST'])
def force_next_track():
    with state_lock:
        state['current_track'] = None
        state['playing'] = False
    # Explicitly wake loop? No need if sleep(1)
    return jsonify({"status": "forced_reset"})

import tempfile

# ...

@app.route('/api/admin/repair_library')
def repair_library():
    """Fixes missing or incorrect durations using ffprobe"""
    count = 0
    fixed = []
    
    with state_lock:
        for item in state['library']:
            # Check if duration is suspicious (0, or <60s for non-music?)
            # Or just check ALL to be safe.
            # Let's check files that exist.
            fpath = os.path.join(app.config['UPLOAD_FOLDER'], item['filename'])
            if os.path.exists(fpath):
                old_dur = item.get('duration', 0)
                
                # Get actual duration
                try:
                    # Use ffprobe via shell
                    # ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 input.mp3
                    cmd = [
                        'ffprobe', '-v', 'error', 
                        '-show_entries', 'format=duration', 
                        '-of', 'default=noprint_wrappers=1:nokey=1', 
                        fpath
                    ]
                    # We need to run this subprocess
                    import subprocess
                    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    real_dur = float(result.stdout.strip())
                    
                    if abs(real_dur - old_dur) > 5: # If variance > 5s
                        item['duration'] = real_dur
                        fixed.append(f"{item['title']}: {old_dur} -> {real_dur}")
                        count += 1
                except Exception as e:
                    print(f"Repair failed for {item['filename']}: {e}")
        
    if count > 0:
        save_data()
        
    return jsonify({"processed": len(state['library']), "fixed": fixed})



@app.route('/api/vote', methods=['POST'])
def vote_track():
    data = request.json
    track_id = str(data.get('id'))
    rating = data.get('rating') # 1-5 integer
    
    # Backward compat: if 'vote' is sent, map it
    if 'vote' in data and rating is None:
        v = data.get('vote')
        if v == 'like': rating = 5
        elif v == 'dislike': rating = 1

    listener_id = request.headers.get('X-Listener-ID')
    
    if not listener_id:
        return jsonify({"error": "No Listener ID"}), 400
    
    try:
        rating = int(rating)
        if rating < 1 or rating > 5: raise ValueError()
    except:
        return jsonify({"error": "Invalid rating (1-5)"}), 400

    now = time.time()
    
    with state_lock:
        # Retention Policy: Clean old votes (older than 90 days)
        cutoff = now - (90 * 24 * 60 * 60)
        state['votes'] = [v for v in state['votes'] if v['timestamp'] > cutoff]
        
        # Check for existing vote
        existing = next((v for v in state['votes'] if v['track_id'] == track_id and v.get('listener_id') == listener_id), None)
        
        if existing:
            existing['rating'] = rating
            existing['timestamp'] = now
            # Clear legacy field if exists
            if 'vote' in existing: del existing['vote']
        else:
            # Add new vote
            state['votes'].append({
                "track_id": track_id,
                "listener_id": listener_id,
                "rating": rating,
                "timestamp": now
            })
            
        save_votes()
        
    return jsonify({"status": "ok"})

@app.route('/api/stats/votes')
def get_vote_stats():
    # Admin only
    
    # Aggregate
    # {track_id: {total_score: 0, count: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0}}
    stats = {} 
    
    with state_lock:
        library_map = {str(item['id']): item for item in state['library']}
        
        for v in state['votes']:
            tid = v['track_id']
            
            # Normalize rating
            r = v.get('rating')
            if r is None:
                # Legacy fallback
                legacy = v.get('vote')
                if legacy == 'like': r = 5
                elif legacy == 'dislike': r = 1
                else: continue # Skip invalid
            
            if tid not in stats:
                stats[tid] = {"total": 0, "count": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
            
            stats[tid]['total'] += r
            stats[tid]['count'] += 1
            if str(r) in stats[tid]:
                stats[tid][str(r)] += 1
        
        # Format for UI
        result = []
        for tid, data in stats.items():
            item = library_map.get(tid)
            title = item['title'] if item else "Unknown Track"
            category = item['category'] if item else "Unknown"
            
            avg = data['total'] / data['count'] if data['count'] > 0 else 0
            
            result.append({
                "id": tid,
                "title": title,
                "category": category,
                "average": round(avg, 1),
                "votes": data['count'],
                "stars_1": data['1'],
                "stars_2": data['2'],
                "stars_3": data['3'],
                "stars_4": data['4'],
                "stars_5": data['5']
            })
            
        # Sort by Average Descending
        result.sort(key=lambda x: x['average'], reverse=True)
        
    return jsonify(result)

@app.route('/api/stats/clear', methods=['POST'])
def clear_vote_stats():
    # Admin only (but no auth check for this demo)
    with state_lock:
        state['votes'] = []
        save_votes()
    return jsonify({"status": "cleared"})

@app.route('/api/upload/cookies', methods=['POST'])
def upload_cookies():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file part'}), 400
        file = request.files['file']
        if file:
            # Save to /tmp/cookies.txt (safer specific path)
            cookie_path = os.path.join(tempfile.gettempdir(), 'grace_radio_cookies.txt')
            file.save(cookie_path)
            return jsonify({"status": "cookies_updated"})
        return jsonify({'error': 'No file selected'}), 400
    except Exception as e:
        print(f"Cookie Upload Error: {e}")
        return jsonify({"error": str(e)}), 500


def run_youtube_download(url, category='Music'):
    """Background task to handle the heavy download"""
    print(f"BACKGROUND: Starting download for {url} (Category: {category})")
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(app.config['UPLOAD_FOLDER'], '%(title)s.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }], 
            'restrictfilenames': True,
            'nocheckcertificate': True,
        }
        
        # Check for cookies in tmp
        cookie_path = os.path.join(tempfile.gettempdir(), 'grace_radio_cookies.txt')
        if os.path.exists(cookie_path):
            ydl_opts['cookiefile'] = cookie_path
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # Fix extension shuffle (webm -> mp3)
            final_filename = os.path.splitext(os.path.basename(filename))[0] + ".mp3"
            
            duration = info.get('duration', 0)
            
            media_item = {
                "id": str(int(time.time()*1000)),
                "title": info.get('title', "Unknown Title"),
                "filename": final_filename, # Ensure we point to the MP3
                "duration": duration,
                "category": category,
                "type": "audio",
                "added_at": time.time()
            }
            
            with state_lock:
                state['library'].append(media_item)
                save_data()
                print(f"BACKGROUND: Success! Added {media_item['title']}")

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"BACKGROUND ERROR: {e}")

# --- Helpers ---
def ensure_queue_filled(exclude_ids=None):
    """Auto-fills queue with random music to maintain 10 items"""
    if exclude_ids is None: exclude_ids = []
    
    # Strict Shuffle: Only Music
    music_cands = [m for m in state['library'] if m.get('category') == 'Music']
    if not music_cands: return # No music to pick from
    
    # Avoid recent repeats (History)
    history_set = set(state['history'])
    
    changes = False
    attempts = 0
    while len(state['queue']) < 10 and attempts < 20:
        attempts += 1
        # Filter candidates
        cands = [m for m in music_cands if m['id'] not in history_set and str(m['id']) not in state['queue'] and str(m['id']) not in exclude_ids]
        
        if not cands:
             # Relax history if strictly needed, or just pick any music
             cands = music_cands
        
        if cands:
            pick = random.choice(cands)
            state['queue'].append(pick['id'])
            changes = True
    
    if changes:
        save_state()

@app.route('/api/library/update', methods=['POST'])
def update_library_item():
    # Support both JSON and FormData
    if request.is_json:
        data = request.json
    else:
        data = request.form

    mid = data.get('id')
    with state_lock:
        item = next((m for m in state['library'] if str(m['id']) == str(mid)), None)
        if item:
            if 'title' in data: item['title'] = data['title']
            if 'category' in data: item['category'] = data['category']
            
            # Art Upload
            if 'art' in request.files:
                file = request.files['art']
                if file and file.filename != '':
                    try:
                        # Ensure persistent dir exists
                        art_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'art')
                        if not os.path.exists(art_dir):
                            os.makedirs(art_dir)

                        # Save art with ID
                        ext = os.path.splitext(file.filename)[1].lower()
                        if not ext: ext = '.jpg'
                        art_filename = f"{mid}{ext}"
                        dest = os.path.join(art_dir, art_filename)
                        file.save(dest)
                        item['art'] = f"/static/art/{art_filename}?t={int(time.time())}" # cache bust
                        
                        # Propagate to Current Track
                        if state.get('current_track') and str(state['current_track']['id']) == str(mid):
                            state['current_track']['art'] = item['art']
                    except Exception as e:
                        print(f"ART UPLOAD ERROR: {e}")
                        # Don't fail the whole request, just log it? 
                        # Or return error?
                        # Let's log and continue, maybe warnings?

            if 'eq' in data: 
                item['eq'] = data['eq'] # Store EQ settings {low, mid, high}
                # Propagate to Current Track if active (Immediate Listener Update)
                if state.get('current_track') and str(state['current_track']['id']) == str(mid):
                    state['current_track']['eq'] = data['eq']
            
            # Folder Support
            new_folder = data.get('folder') # e.g. "Newsboys" or "" (root)
            if new_folder is not None:
                # Sanitize folder name
                new_folder = secure_filename(new_folder)
                
                # Current location
                old_filename = item['filename']
                src_path = os.path.join(app.config['UPLOAD_FOLDER'], old_filename)
                
                # Determine basic filename (without path)
                base_name = os.path.basename(old_filename)
                
                # New Filename
                if new_folder:
                     new_filename = os.path.join(new_folder, base_name)
                else:
                     new_filename = base_name
                
                # Normalize slashes
                new_filename = new_filename.replace('\\', '/')

                if new_filename != old_filename:
                    # Move File
                    src_path = os.path.join(app.config['UPLOAD_FOLDER'], old_filename)
                    dst_path = os.path.join(app.config['UPLOAD_FOLDER'], new_filename)
                    
                    # Check bundled fallback
                    is_bundled = False
                    if not os.path.exists(src_path):
                        bundled_path = os.path.join(app.root_path, 'static', 'media', old_filename)
                        if os.path.exists(bundled_path):
                             src_path = bundled_path
                             is_bundled = True

                    dst_dir = os.path.dirname(dst_path)
                    
                    try:
                        # Create dir if not exists
                        if dst_dir and not os.path.exists(dst_dir):
                            os.makedirs(dst_dir)
                            
                        # Move
                        if os.path.exists(src_path):
                             if is_bundled:
                                 import shutil
                                 shutil.copy2(src_path, dst_path)
                             else:
                                 os.rename(src_path, dst_path)

                             item['filename'] = new_filename
                             print(f"MOVED: {old_filename} -> {new_filename}")

                        else:
                            # If file missing, just update DB path? No, risky. 
                            print(f"MOVE FAILED: Source {src_path} not found.")
                            # Fallback: maybe it's in local static? We can't move logical static files easily.
                            # Only move if in UPLOAD_FOLDER
                            pass
                    except Exception as e:
                        print(f"MOVE ERROR: {e}")
                        return jsonify({"error": f"Failed to move file: {str(e)}"}), 500

            save_data()
            return jsonify({"status": "updated", "item": item})
    return jsonify({"error": "not found"}), 404

@app.route('/api/queue/reorder', methods=['POST'])
def reorder_queue():
    """Expects [id1, id2, id3...] representing new order"""
    new_order = request.json.get('order', [])
    with state_lock:
        valid_ids = []
        for qid in new_order:
             if any(str(m['id']) == str(qid) for m in state['library']):
                 valid_ids.append(str(qid))
        state['queue'] = valid_ids
        ensure_queue_filled()
        save_state()
    return jsonify({"status": "ok", "queue": state['queue']})

@app.route('/api/queue/remove', methods=['POST'])
def remove_from_queue():
    target_id = request.json.get('id')
    with state_lock:
        state['queue'] = [q for q in state['queue'] if str(q) != str(target_id)]
        ensure_queue_filled(exclude_ids=[str(target_id)])
        save_state()
    return jsonify({"status": "removed"})
    
@app.route('/api/upload/youtube', methods=['POST'])
def upload_youtube():
    try:
        data = request.json
        url = data.get('url')
        category = data.get('category', 'Music') 
        if not url:
            return jsonify({"error": "No URL provided"}), 400
        thread = threading.Thread(target=run_youtube_download, args=(url, category), daemon=True)
        thread.start()
        return jsonify({"status": "accepted", "message": "Download started in background."}), 202
    except Exception as e:
        print(f"DL Launch Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/queue/add', methods=['POST'])
def add_to_queue():
    data = request.json
    media_id = data.get('id')
    with state_lock:
        # verify exists
        # Fix: Ensure strict string comparison here too, in case library has int but we received str
        if any(str(m['id']) == str(media_id) for m in state['library']):
            # Priority: Insert at 0 so it plays NEXT
            # Duplicate check handled?
            # User might want to queue same song multiple times?
            # If we auto-fill, duplicates are disallowed.
            # Manual queues allow duplicates? Let's allow.
            state['queue'].insert(0, str(media_id))
            save_state() 
            return jsonify({"status": "added"})
    return jsonify({"error": "not found"}), 404

def log_sched(msg):
    try:
        with open("schedule_debug.txt", "a") as f:
            f.write(f"{datetime.now()}: {msg}\n")
    except: pass

@app.route('/api/schedule/add', methods=['POST'])
def add_to_schedule():
    data = request.json
    media_id = data.get('id')
    run_at = data.get('run_at') # Timestamp expected
    
    log_sched(f"ADD REQUEST: media={media_id}, run_at={run_at} (type {type(run_at)})")

    # Handle numeric/string conversion
    try:
        if isinstance(run_at, str):
             # Try parse ISO
             dt = datetime.fromisoformat(run_at.replace('Z', '+00:00'))
             run_at = dt.timestamp()
    except:
        pass

    run_at = float(run_at)
    log_sched(f"ADD NORMALIZED: {run_at} (Now: {time.time()})")

    with state_lock:
        state['schedule'].append({
            "id": str(random.randint(0, 100000)),
            "media_id": media_id,
            "run_at": run_at
        })
        save_data()
        log_sched(f"Schedule Saved. Count: {len(state['schedule'])}")
    return jsonify({"status": "scheduled"})

@app.route('/api/schedule/list', methods=['GET'])
def list_schedule():
    res = []
    # Sort by time
    sorted_sched = sorted(state['schedule'], key=lambda x: x['run_at'])
    
    for s in sorted_sched:
        media = next((m for m in state['library'] if m['id'] == s['media_id']), None)
        item = s.copy()
        if media:
            item['title'] = media['title']
            item['category'] = media.get('category', 'Unknown')
            item['duration'] = media.get('duration', 0)
        else:
             item['title'] = "Unknown ID: " + str(s['media_id'])
        res.append(item)
    return jsonify(res)

@app.route('/api/schedule/remove', methods=['POST'])
def remove_schedule_item():
    item_id = request.json.get('id')
    with state_lock:
        original_len = len(state['schedule'])
        state['schedule'] = [s for s in state['schedule'] if str(s['id']) != str(item_id)]
        if len(state['schedule']) < original_len:
            save_data()
            return jsonify({"status": "removed"})
    return jsonify({"error": "not found"}), 404

@app.route('/api/schedule/update', methods=['POST'])
def update_schedule_item():
    data = request.json
    item_id = data.get('id')
    new_run_at = data.get('run_at')
    
    # Parse timestamp
    try:
        if isinstance(new_run_at, str):
             dt = datetime.fromisoformat(new_run_at.replace('Z', '+00:00'))
             new_run_at = dt.timestamp()
    except:
        return jsonify({"error": "invalid timestamp"}), 400

    with state_lock:
        item = next((s for s in state['schedule'] if str(s['id']) == str(item_id)), None)
        if item:
            item['run_at'] = float(new_run_at)
            save_data()
            return jsonify({"status": "updated"})
    return jsonify({"error": "not found"}), 404

@app.route('/api/delete/<media_id>', methods=['DELETE'])
def delete_media(media_id):
    with state_lock:
        # Remove from library, queue, schedule
        item = next((m for m in state['library'] if m['id'] == media_id), None)
        if item:
            try:
                # Try delete from UPLOAD_FOLDER (Persistent)
                path_p = os.path.join(app.config['UPLOAD_FOLDER'], item['filename'])
                if os.path.exists(path_p):
                    os.remove(path_p)
                else:
                    # Try delete from Local Static (Fallback)
                    path_l = os.path.join(app.root_path, 'static', 'media', item['filename'])
                    if os.path.exists(path_l):
                         os.remove(path_l)
            except:
                pass

            # Tombstone: Prevent bundled files from reappearing
            # Store the basename of the file
            bn = os.path.basename(item['filename'])
            if bn not in state['deleted_files']:
                state['deleted_files'].append(bn)

            state['library'] = [m for m in state['library'] if m['id'] != media_id]
            state['queue'] = [q for q in state['queue'] if q != media_id]
            state['schedule'] = [s for s in state['schedule'] if s['media_id'] != media_id]
            save_data()
            return jsonify({"status": "deleted"})
    return jsonify({"error": "not found"}), 404


@app.route('/api/skip', methods=['POST'])
def skip_track():
    with state_lock:
        state['current_track'] = None
        state['playing'] = False
        # loop runs every 1s, will pick up next immediately
    return jsonify({"status": "skipped"})


@app.route('/static/media/<path:filename>')
def custom_static(filename):
    # 1. Check Permanent Disk (Uploads)
    upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if os.path.exists(upload_path):
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)
    
    # 2. Check Local Static Folder (Built-in)
    local_path = os.path.join(app.root_path, 'static', 'media')
    if os.path.exists(os.path.join(local_path, filename)):
        return send_from_directory(local_path, filename)
        
    print(f"404: Could not find {filename} in {app.config['UPLOAD_FOLDER']} or {local_path}")
    return "File not found", 404

@app.route('/static/art/<path:filename>')
def custom_art(filename):
    # 1. Check Permanent Disk (Uploads)
    # Be careful with subdirectory 'art'
    art_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'art')
    upload_path = os.path.join(art_dir, filename)
    
    if os.path.exists(upload_path):
        return send_from_directory(art_dir, filename)
    
    # 2. Check content-type param in filename? No.
    
    print(f"404 Art: {filename} not found in {art_dir}")
    return "Not Found", 404

if __name__ == '__main__':
    # Local development
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG', 'True')=='True')
