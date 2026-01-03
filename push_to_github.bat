@echo off
echo ===================================================
echo Pushing Grace Radio to GitHub
echo ===================================================
echo.
echo Make sure you have created a repository at https://github.com/new
echo AND named it 'grace-radio'.
echo.
set /p REPO_URL="Paste your GitHub Repository URL here: "

"C:\Program Files\Git\cmd\git.exe" remote add origin %REPO_URL%
"C:\Program Files\Git\cmd\git.exe" branch -M main
"C:\Program Files\Git\cmd\git.exe" push -u origin main

echo.
echo Done! Now go to Render.com and connect this repo.
pause
