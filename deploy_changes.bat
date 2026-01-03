@echo off
echo ===================================================
echo Deploying Changes to Grace Radio
echo ===================================================
echo.
set /p MSG="Enter a description of your changes: "

"C:\Program Files\Git\cmd\git.exe" add .
"C:\Program Files\Git\cmd\git.exe" commit -m "%MSG%"
"C:\Program Files\Git\cmd\git.exe" push origin main

echo.
echo Changes pushed! Render should automatically redeploy now.
echo It may take 1-2 minutes for the new version to go live.
pause
