# Grace Radio

A modern, single-station radio application.

## Features
- **Live Broadcasting**: Continuous play with "Smart Client" synchronization.
- **Media Management**: Organize "Music", "Sermons", and "Announcements".
- **Scheduling**: Schedule broadcasts at specific times.
- **Queueing**: Manually queue up tracks to play next.
- **Modern UI**: Dark mode, glassmorphism design.

## How to Run

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Run Server**:
   ```bash
   python app.py
   ```

3. **Access Dashboard/Player**:
   Open [http://localhost:5000](http://localhost:5000) in your browser.

## Listener vs Admin Mode

- **Listeners**: Give them the URL `http://192.168.0.21:5000` (or your server's IP).
  - They see the "Live Player" and current queue.
  - They *cannot* modify the library or schedule.

- **Admin Dashboard**: Go to `http://localhost:5000/admin`
  - You have full access to Upload, Schedule, Queue, and Delete.

## Hosting
To host on a server:
1. Copy this folder to your server.
2. Run `app.py`.
3. Ensure port 5000 is open in your firewall.


## Usage
- **Upload**: Click "+ Upload Media" in the Library tab. You can select multiple files at once.
- **Queue**: Click "Queue Next" on any item.
- **Schedule**: Click "Schedule" on any item and pick a time.
- **Listen**: Click "Sync Stream" on the player to start listening.
