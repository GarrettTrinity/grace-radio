@echo off
echo Auto-Deploying...
"C:\Program Files\Git\cmd\git.exe" add .
"C:\Program Files\Git\cmd\git.exe" commit -m "Auto-deploy via Agent"
"C:\Program Files\Git\cmd\git.exe" push origin main
echo Deployed.
