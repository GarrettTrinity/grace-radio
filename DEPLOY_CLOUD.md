# Deploying Grace Radio to the Cloud

The "safest" and most reliable way to host your station is using a Cloud Provider like **Render.com**.

## Step 1: Get the Code on GitHub
1. Create an account at [GitHub.com](https://github.com).
2. Create a **New Repository** named `grace-radio`.
3. Open a terminal in this `Grace Radio` folder and run:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   # Replace URL with your new repo URL
   git remote add origin https://github.com/YOUR_USERNAME/grace-radio.git
   git push -u origin main
   ```

## Step 2: Deploy on Render (Free Tier)
1. Sign up at [Render.com](https://render.com).
2. Click **New +** -> **Web Service**.
3. Select "Build and deploy from a Git repository".
4. Connect your GitHub account and select `grace-radio`.
5. **Settings**:
   - **Name**: `grace-radio`
   - **Region**: (Choose closest to you)
   - **Branch**: `main`
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
6. Click **Create Web Service**.

## Step 3: IMPORTANT - Data Persistence
**Warning**: On the free tier of most cloud providers (Render, Heroku), **files you upload will disappear if the server restarts**.
To prevent this, you have two options:

### Option A: Use Render "Disk" (Paid)
If you upgrade to a paid Render plan (~$7/mo), you can add a **Disk**.
1. Go to your Web Service settings -> **Disks**.
2. Create a disk:
   - **Mount Path**: `/opt/render/project/src/static/media`
   - **Size**: 1GB (or more)
3. Redeploy. Now your music will stay safe!

### Option B: Keep it Local (Free)
If you don't want to pay, stick to the **ngrok** method described in `GOING_PUBLIC.md`. It runs on YOUR computer, so your files are always safe on your hard drive.

## Step 4: Updating the App
To make changes (like adding features or fixing bugs):
1. Ask the AI to make the code changes.
2. Run `deploy_changes.bat` in this folder.
3. Enter a short description.
4. Render will detect the change and auto-update your site in ~1 minute.
