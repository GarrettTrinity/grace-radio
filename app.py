import os
import time
import json
import random
import threading
import mimetypes
import yt_dlp
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory, Response
from werkzeug.utils import secure_filename
from mutagen import File as MutagenFile

app = Flask(__name__)

# Configuration
# Use /tmp for ephemeral cloud storage if not configured, or a specific persistent path
# In production on Render/Heroku, local files often wiped on restart unless using a Volume.
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'static/media')
DATA_FILE = os.environ.get('DATA_FILE', 'data.json')
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'ogg', 'm4a', 'mp4', 'webm'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# Ensure directory exists (important for cloud where folders might not exist in repo)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Global State
state = {
    "library": [],        # List of media objects: {id, title, filename, duration, type, category}
    "queue": [],          # List of media IDs to play next (User manual queue)
    "schedule": [],       # List of {id, run_at_timestamp, media_id}
    "history": [],        # IDs of played songs
    "current_track": None, # { ...media_obj, start_time: timestamp }
    "playing": False
}

state_lock = threading.Lock()

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            try:
                data = json.load(f)
                state['library'] = data.get('library', [])
                state['schedule'] = data.get('schedule', [])
            except:
                pass

def save_data():
    with open(DATA_FILE, 'w') as f:
        json.dump({
            "library": state['library'],
            "schedule": state['schedule']
        }, f, indent=2)

load_data()

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

# --- Scheduler / Radio Logic ---
def radio_loop():
    print("--- Radio Loop Started ---")
    while True:
        try:
            with state_lock:
                now = time.time()
                current = state['current_track']
                
                # --- Cleanup Temporary Files ---
                to_remove = []
                for item in state['library']:
                    # Default to removing if > 24h and Temporary
                    if item.get('category') == 'Temporary' and item.get('added_at'):
                        # 24 hours = 86400 seconds
                        if now - item.get('added_at') > 86400:
                            to_remove.append(item)
                
                for item in to_remove:
                    try:
                        print(f"Removing Cleaned Up Temp File: {item['title']}")
                        os.remove(os.path.join(app.config['UPLOAD_FOLDER'], item['filename']))
                    except Exception as e:
                        print(f"Cleanup error: {e}")
                    
                    if item in state['library']:
                        state['library'].remove(item)
                
                if to_remove:
                    save_data()

                # --- Playback Logic ---
                # Check if current song is finished or nothing is playing
                # Safety checks for duration
                duration = current['duration'] if current and isinstance(current.get('duration'), (int, float)) and current['duration'] > 0 else 10
                
                if not current or (now - current['start_time'] >= duration + 2): # +2s buffer
                    
                    # Update 'last_played_at'
                    if current:
                         lib_item = next((m for m in state['library'] if m['id'] == current['id']), None)
                         if lib_item:
                             lib_item['last_played_at'] = now
                             save_data()

                    # Pick next song
                    next_media = None

                    # 1. Check Schedule (Expired/Due events)
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
                            print(f"Playing SCHEDULED: {media['title']}")
                    
                    # 2. Check User Queue
                    if not next_media and state['queue']:
                         media_id = state['queue'].pop(0)
                         media = next((m for m in state['library'] if m['id'] == media_id), None)
                         if media:
                             next_media = media
                             print(f"Playing QUEUED: {media['title']}")

                    # 3. Shuffle (Exclude Temporary)
                    if not next_media and state['library']:
                         candidates = [
                             m for m in state['library'] 
                             if m.get('category') != 'Temporary'
                         ]
                         
                         # Fallback if library only has temporary
                         if not candidates and state['library']:
                             candidates = state['library']

                         if candidates:
                             next_media = random.choice(candidates)
                             print(f"Playing SHUFFLE: {next_media['title']}")

                    if next_media:
                        state['current_track'] = next_media.copy()
                        state['current_track']['start_time'] = time.time()
                        state['playing'] = True
                        state['history'].append(next_media['id'])
                        if len(state['history']) > 20:
                            state['history'].pop(0)
                    else:
                        state['current_track'] = None
                        state['playing'] = False

        except Exception as e:
            print(f"CRITICAL RADIO LOOP ERROR: {e}")
            # Prevent rapid CPU spike on error loop
            time.sleep(5)
            
        time.sleep(1)

# Start Radio Thread
radio_thread = threading.Thread(target=radio_loop, daemon=True)
radio_thread.start()

# --- Routes ---

@app.route('/')
def index():
    return render_template('index.html', is_admin=False)

@app.route('/admin')
def admin_dashboard():
    return render_template('index.html', is_admin=True)

@app.route('/api/status')
def get_status():
    with state_lock:
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
        }
        
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
                os.remove(os.path.join(app.config['UPLOAD_FOLDER'], item['filename']))
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
