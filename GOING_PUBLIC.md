# How to Go Public

There are two main ways to make your station listenable for people outside your house.

## Option A: "Instant Public Mode" (Easiest)
We can use a tool called **ngrok** to tunnel your local computer to the internet. 

1. **Sign up** (free) at [ngrok.com](https://ngrok.com).
2. Get your **Authtoken** from the dashboard.
3. Open a terminal in this folder and run:
   ```powershell
   ngrok config add-authtoken YOUR_TOKEN_HERE
   ```
   (You only need to do this once).
4. Run the "Public" script:
   ```powershell
   python go_public.py
   ```
5. It will generate a link like `https://a1b2-c3d4.ngrok-free.app`. **Send this link to your listeners!**

*Note: If you close the terminal or turn off your computer, the station goes offline.*

## Option B: "Permanent Cloud Hosting" (Professional)
For a 24/7 station that runs even when your computer is off, you should host this on a cloud provider.

### Recommended: Render.com (Free Tier available)
1. Push this code to **GitHub**.
2. Sign up at [Render.com](https://render.com).
3. Create a new **Web Service**.
4. Connect your GitHub repo.
5. Use the following settings:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
6. Click Deploy! You will get a permanent URL like `https://grace-radio.onrender.com`.

### Important Note for Cloud Hosting
If you host on the cloud, the "Local File Storage" used for uploads (`static/media`) will be erased every time you deploy/restart on most free tiers (like Render/Heroku).
To fix this for a production app, you would need to connect an **S3 Bucket** (AWS/Cloudflare R2) to store the songs permanently.
