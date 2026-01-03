import os
import sys
from pyngrok import ngrok
from app import app
import threading

# You need an authtoken for ngrok to work properly now
# Ask user to set it or input it
# For now we'll try to connect and see if it purely fails without token (it usually allows limited sessions)

def run_server():
    app.run(port=5000, use_reloader=False)

if __name__ == "__main__":
    print("----------------------------------------------------------------")
    print("INITIALIZING PUBLIC BROADCAST (Powered by ngrok)")
    print("----------------------------------------------------------------")
    
    # Check for token variable
    # os.environ["NGROK_AUTHTOKEN"] = "..." 

    try:
        # Open a HTTP tunnel on the default port 5000
        # <NgrokTunnel: "http://<public_sub>.ngrok.io" -> "http://localhost:5000">
        public_url = ngrok.connect(5000).public_url
        print(f"\n * \033[92m LIVE ON AIR! Public Listener URL: {public_url} \033[0m\n")
        print(f" * Admin Dashboard: http://localhost:5000/admin")
        print(" * Keep this window OPEN to stay live.")
        print("----------------------------------------------------------------")
    except Exception as e:
        print(f"Error starting tunnel: {e}")
        print("Tip: You may need to sign up at ngrok.com and run: `ngrok config add-authtoken <token>`")

    # Run Flask
    run_server()
