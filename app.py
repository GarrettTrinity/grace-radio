import os
import time
import json
import random
import threading
import mimetypes
import yt_dlp
import tempfile
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory, Response
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
    STORAGE_DIR = 'static/media'
else:
    # Linux (Render) -> Check if mount exists, else fallback
    if not os.path.exists('/var/lib/grace_radio'):
        STORAGE_DIR = 'static/media'
    else:
        STORAGE_DIR = '/var/lib/grace_radio'

UPLOAD_FOLDER = STORAGE_DIR
DATA_FILE = os.path.join(STORAGE_DIR, 'data.json')
STATE_FILE = os.path.join(STORAGE_DIR, 'state.json')
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'ogg', 'm4a', 'mp4', 'webm'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# Ensure directory exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Global State (In-Memory Cache)
state = {
    "library": [],        # List of media objects: {id, title, filename, duration, type, category}
    "queue": [],          # List of media IDs to play next (User manual queue)
    "schedule": [],       # List of {id, run_at_timestamp, media_id}
    "history": [],        # IDs of played songs
    "current_track": None, # { ...media_obj, start_time: timestamp }
    "playing": False
}

state_lock = threading.Lock()

def get_media_duration(filepath):
    try:
        audio = MutagenFile(filepath)
        if audio is not None and audio.info is not None:
            return audio.info.length
    except Exception as e:
        print(f"Error reading duration: {e}")
    return 0

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def load_data():
    # 1. Load persistent data (Library, Schedule)
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            try:
                data = json.load(f)
                state['library'] = data.get('library', [])
                state['schedule'] = data.get('schedule', [])
            except: pass
            
    # BOOTSTRAP: If library is empty, scan local static/media (The "Original 40")
    if not state['library']:
        print("Library is empty. Bootstrapping from bundled content...")
        
        # Path to bundled content in git repo
        local_static = os.path.join(app.root_path, 'static', 'media')
        
        if os.path.exists(local_static):
            for filename in os.listdir(local_static):
                if allowed_file(filename):
                    # Check if already in library (redundant if empty, but safe)
                    if any(m['filename'] == filename for m in state['library']):
                        continue
                        
                    filepath = os.path.join(local_static, filename)
                    duration = get_media_duration(filepath)
                    
                    media_item = {
                        "id": str(int(time.time()*1000) + random.randint(1,999)),
                        "title": os.path.splitext(filename)[0].replace('_', ' '),
                        "filename": filename,
                        "duration": duration,
                        "category": "Music", # Default to Music for bootstrap
                        "type": "audio",
                        "added_at": time.time()
                    }
                    state['library'].append(media_item)
            
            # Save initialized state to the new disk
            save_data()
    
    # 2. Load volatile state (Current Track)
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try:
                s_data = json.load(f)
                state['current_track'] = s_data.get('current_track')
                state['playing'] = s_data.get('playing', False)
                state['queue'] = s_data.get('queue', state['queue'])
            except: pass

def save_data():
    # Save persistent data
    with open(DATA_FILE, 'w') as f:
        json.dump({
            "library": state['library'],
            "schedule": state['schedule']
        }, f, indent=2)

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
        # Check if lock exists and is valid (fresh < 10s)
        if os.path.exists(LOCK_FILE):
             mtime = os.path.getmtime(LOCK_FILE)
             if time.time() - mtime < 10:
                 return False # Active lock exists
        
        # Take lock
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
        return True
    except:
        return False

def update_heartbeat():
    try:
        # Touch file
        os.utime(LOCK_FILE, None)
    except: pass

# --- Thread Management ---
radio_thread = None

def radio_loop():
    print(f"--- Radio Loop Started (PID: {os.getpid()}) ---")
    
    # Simple file logger function
    def log_loop(msg):
        try:
            with open("loop_debug.log", "a") as f:
                f.write(f"[{time.ctime()}][PID {os.getpid()}] {msg}\n")
        except: pass

    log_loop("Loop initialized.")

    while True:
        # 0. Singleton Check
        if not acquire_lock():
            # Another worker is active, I should back off
            # But wait, acquire_lock updates the lock if I own it? 
            # No, simplistic check.
            # Let's verify if I own it first?
            # Actually, the simplest check:
            # If valid lock exists and it's NOT ME, sleep.
            # If I own it, touch it.
            
            # Better logic inside loop:
            try:
                if os.path.exists(LOCK_FILE):
                    mtime = os.path.getmtime(LOCK_FILE)
                    if time.time() - mtime < 5:
                        # Active. Is it me?
                        with open(LOCK_FILE, 'r') as f:
                            pid = f.read().strip()
                        if pid != str(os.getpid()):
                            # It's someone else. I sleep.
                            time.sleep(5)
                            continue
            except: pass
            
            # If I got here, I'm taking over (or renewing)
            try:
                with open(LOCK_FILE, 'w') as f:
                    f.write(str(os.getpid()))
            except: pass

        update_heartbeat()

        try:
            with state_lock:
                now = time.time()
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
                    if elapsed >= dur + 1: # 1s buffer
                        should_pick = True
                        log_loop(f"Picking: Track Finished ({elapsed:.1f}s / {dur}s)")
                        
                        # Update Last Played
                        lib_item = next((m for m in state['library'] if m['id'] == current['id']), None)
                        if lib_item:
                            lib_item['last_played_at'] = now
                            save_data()

                if should_pick:
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
                        media = next((m for m in state['library'] if m['id'] == item['media_id']), None)
                        if media:
                            next_media = media
                            log_loop(f"Selected SCHEDULED: {media['title']}")

                    # 2. Queue
                    if not next_media and state['queue']:
                         media_id = state['queue'].pop(0)
                         media = next((m for m in state['library'] if m['id'] == media_id), None)
                         if media:
                             next_media = media
                             log_loop(f"Selected QUEUED: {media['title']}")

                    # 3. Shuffle
                    if not next_media:
                        # Filters
                        music_cands = [m for m in state['library'] if m.get('category') == 'Music']
                        other_cands = [m for m in state['library'] if m.get('category') != 'Temporary']
                        
                        # Priority 1: Unplayed Music (not in history)
                        history_set = set(state['history'])
                        
                        # Try to find Music not in history
                        final_cands = [m for m in music_cands if m['id'] not in history_set]
                        
                        if not final_cands:
                            # Priority 2: Unplayed Non-Temp (Sermons etc)
                            final_cands = [m for m in other_cands if m['id'] not in history_set]
                        
                        if not final_cands:
                            # Priority 3: Reset! All Music (Recycle)
                            final_cands = music_cands
                            # Optional: clear history early? 
                            # No, just pick from full list, ensuring we don't get stuck.
                            # We will keep history filtering for the next turn though.
                        
                        if not final_cands:
                             # Panic: Anything in library
                             final_cands = state['library']

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

def start_radio_thread():
    global radio_thread
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

# --- Routes ---

@app.route('/')
def index():
    return render_template('index.html', is_admin=False)

@app.route('/admin')
def admin_dashboard():
    return render_template('index.html', is_admin=True)

@app.route('/api/status')
def get_status():
    # Force reload of state to ensure we see what the background worker is doing
    with state_lock:
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r') as f:
                    s_data = json.load(f)
                    # We only update playback info, not library (expensive)
                    state['current_track'] = s_data.get('current_track')
                    state['playing'] = s_data.get('playing')
        except: pass

        now = time.time()
        current = state['current_track']
        
        # Calculate plays
        if current:
            elapsed = now - current['start_time']
            current_copy = current.copy()
            current_copy['elapsed'] = elapsed
        else:
            current_copy = None

        return jsonify({
            "current": current_copy,
            "queue_len": len(state['queue']),
            "schedule_len": len(state['schedule']),
            "server_time": now
        })

@app.route('/api/library', methods=['GET', 'POST'])
def library():
    if request.method == 'GET':
        return jsonify(state['library'])
    
    # POST - Add items is handled by upload mostly, but maybe editing metadata?
    return jsonify({"error": "Use upload"}), 400

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
            file.save(path)
            
            duration = get_media_duration(path)
            
            # Small sleep to ensure unique ID if multiple files uploaded instantly
            time.sleep(0.01)
            media_item = {
                "id": str(int(time.time()*1000)) + str(random.randint(0,1000)),
                "title": filename, 
                "filename": filename,
                "duration": duration,
                "category": category,
                "type": "audio"
            }
            uploaded_items.append(media_item)

    if uploaded_items:
        with state_lock:
            state['library'].extend(uploaded_items)
            save_data()
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

@app.route('/api/upload/youtube', methods=['POST'])
def upload_youtube():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(app.config['UPLOAD_FOLDER'], '%(title)s.%(ext)s'),
            'postprocessors': [], 
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
            basename = os.path.basename(filename)
            duration = info.get('duration', 0)
            
            media_item = {
                "id": str(int(time.time()*1000)),
                "title": info.get('title', basename),
                "filename": basename,
                "duration": duration,
                "category": "Temporary",
                "type": "audio",
                "added_at": time.time()
            }
            
            with state_lock:
                state['library'].append(media_item)
                save_data()
            
            return jsonify(media_item)
            
    except Exception as e:
        print(f"DL Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/queue/add', methods=['POST'])
def add_to_queue():
    data = request.json
    media_id = data.get('id')
    with state_lock:
        # verify exists
        if any(m['id'] == media_id for m in state['library']):
            state['queue'].append(media_id)
            return jsonify({"status": "added"})
    return jsonify({"error": "not found"}), 404

@app.route('/api/schedule/add', methods=['POST'])
def add_to_schedule():
    data = request.json
    media_id = data.get('id')
    run_at = data.get('run_at') # Timestamp or ISO string
    
    # Convert ISO to timestamp if needed
    # Assuming input is timestamp for simplicity or handle ISO
    try:
        if isinstance(run_at, str):
             # Try parse ISO
             dt = datetime.fromisoformat(run_at.replace('Z', '+00:00'))
             run_at = dt.timestamp()
    except:
        pass

    with state_lock:
        state['schedule'].append({
            "id": str(random.randint(0, 100000)),
            "media_id": media_id,
            "run_at": float(run_at)
        })
        save_data()
    return jsonify({"status": "scheduled"})

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


if __name__ == '__main__':
    # Local development
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG', 'True')=='True')
