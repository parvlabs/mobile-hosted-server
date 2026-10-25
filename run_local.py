"""
Local Runner & Preview Server
--------------------------------------------------------------
Runs the Mobile Hosted Server locally on Windows / macOS / Linux.
- Starts the Flask API on port 5000
- Starts the Dashboard static server on port 8080
- Displays the API access token for File Manager & Controls
- Automatically opens the dashboard in your default browser
- Cleanly stops all services on Ctrl+C
"""

import os
import sys
import time
import socket
import webbrowser
import subprocess
import signal

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(SERVER_DIR, ".token")
PIN_FILE = os.path.join(SERVER_DIR, ".pin")
API_PORT = 5000
DASHBOARD_PORT = 8080


def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def get_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    return "See terminal output when API starts"


def get_pin():
    if os.path.exists(PIN_FILE):
        try:
            with open(PIN_FILE, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    return "See API status"


def main():
    print("=" * 66)
    print("       MOBILE HOSTED SERVER — LOCAL RUNNER & PREVIEW       ")
    print("=" * 66)
    print(f"Working Directory: {SERVER_DIR}")
    print(f"Python Version:    {sys.version.split()[0]}")

    # Check port availability
    if is_port_in_use(API_PORT):
        print(f"[!] Warning: Port {API_PORT} is already in use.")
    if is_port_in_use(DASHBOARD_PORT):
        print(f"[!] Warning: Port {DASHBOARD_PORT} is already in use.")

    # Start Flask API process
    print(f"\n[*] Starting Flask API on http://127.0.0.1:{API_PORT}...")
    api_proc = subprocess.Popen(
        [sys.executable, "api.py"],
        cwd=SERVER_DIR
    )

    # Start Dashboard HTTP server on port 8080 (Termux equivalent)
    print(f"[*] Starting Dashboard on http://127.0.0.1:{DASHBOARD_PORT}...")
    dash_proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(DASHBOARD_PORT)],
        cwd=SERVER_DIR
    )

    # Allow servers a moment to bind
    time.sleep(1.5)

    token = get_token()
    pin = get_pin()

    dashboard_url = f"http://localhost:{DASHBOARD_PORT}"
    flask_url = f"http://localhost:{API_PORT}"

    print("\n" + "=" * 66)
    print(">>> SERVERS RUNNING SUCCESSFULLY! <<<")
    print("=" * 66)
    print(f"Dashboard URL:      {dashboard_url}")
    print(f"Direct API / Web:   {flask_url}")
    print(f"6-Digit Pair PIN:   {pin}")
    print(f"API Access Token:   {token}")
    print("-" * 66)
    print("NOTE: You can now connect from any device by scanning the QR code,")
    print("      or typing the 6-digit PIN in the Settings / Pair dialog.")
    print("=" * 66)
    print("\nPress Ctrl+C to stop both servers...\n")

    # Open default browser
    try:
        webbrowser.open(dashboard_url)
    except Exception:
        pass

    try:
        while True:
            time.sleep(1)
            if api_proc.poll() is not None:
                print("[!] API process terminated unexpectedly.")
                break
            if dash_proc.poll() is not None:
                print("[!] Dashboard process terminated unexpectedly.")
                break
    except KeyboardInterrupt:
        print("\n[*] Stopping servers...")
    finally:
        for proc in (api_proc, dash_proc):
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        print("[OK] All servers stopped cleanly.")


if __name__ == "__main__":
    main()
