"""
Mobile Hosted Server — Flask API
--------------------------------------------------------------
Run with:
    python3 api.py

Listens on 0.0.0.0:5000 (LAN only — never expose this to the
public internet).

Requires:
    pip install flask flask-cors
    pkg install termux-api      (provides termux-battery-status)
    The separate "Termux:API" app must be installed on the device.

SECURITY MODEL
    - File-manager access is confined to ~/server/shared/ only —
      NOT the whole phone storage. safe_join() blocks any path
      that would escape it (../, absolute paths, symlink escapes).
    - .token and the logs/ directory live outside ~/server/shared/,
      so they are structurally unreachable through file endpoints.
    - File-management, server-control, and log-clear endpoints
      require `Authorization: Bearer <token>` (or ?token= for the
      two GET endpoints that need to be opened directly by the
      browser: download and stream).
    - Server controls only ever call one of a small, hardcoded set
      of actions (start/stop/restart web|api|ssh|all). There is no
      endpoint that runs an arbitrary command.
    - CORS is restricted to local-network origins on port 8080
      (10.x, 172.16-31.x, 192.168.x, localhost) rather than "*".
    - debug=False always; unhandled errors return a generic JSON
      message, never a Python traceback.
"""

import getpass
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from functools import wraps
from logging.handlers import RotatingFileHandler

import io
import platform

try:
    import psutil
except ImportError:
    psutil = None

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

import hashlib
import urllib.request
import zipfile

try:
    import qrcode
except ImportError:
    qrcode = None

from flask import Flask, jsonify, request, send_file, send_from_directory, Response
from flask_cors import CORS
from werkzeug.utils import secure_filename

# ================================================================
# CONFIG
# ================================================================
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SERVER_DIR, "logs")
TOKEN_FILE = os.path.join(SERVER_DIR, ".token")
PIN_FILE = os.path.join(SERVER_DIR, ".pin")
FILE_ROOT = os.path.join(SERVER_DIR, "shared")          # File Manager / media root
PUBLIC_HTML_DIR = os.path.join(SERVER_DIR, "public_html")
TRASH_DIR = os.path.join(SERVER_DIR, ".trash")
BACKUP_DIR = os.path.join(SERVER_DIR, "backups")
LINKS_FILE = os.path.join(SERVER_DIR, ".shared_links.json")
TRASH_MANIFEST_FILE = os.path.join(TRASH_DIR, "manifest.json")
DDNS_FILE = os.path.join(SERVER_DIR, ".ddns_config.json")
CRON_FILE = os.path.join(SERVER_DIR, ".cron_tasks.json")
ALERTS_FILE = os.path.join(SERVER_DIR, ".alerts_config.json")
TELEMETRY_HISTORY_FILE = os.path.join(LOG_DIR, "telemetry_24h.json")
DASHBOARD_PID_FILE = os.path.join(LOG_DIR, "dashboard.pid")

API_PORT = 5000
DASHBOARD_PORT = 8080
SSH_DEFAULT_PORT = 8022

os.makedirs(PUBLIC_HTML_DIR, exist_ok=True)
os.makedirs(TRASH_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

_default_site_index = os.path.join(PUBLIC_HTML_DIR, "index.html")
if not os.path.exists(_default_site_index):
    try:
        with open(_default_site_index, "w", encoding="utf-8") as _f:
            _f.write("""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Mobile Hosted Server — Static Site</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0b0d11; color: #f0f4f8; display: flex; align-items: center; justify-content: center; min-height: 90vh; margin: 0; text-align: center; }
    .card { background: #141820; border: 1px solid #232a38; border-radius: 12px; padding: 40px; max-width: 520px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }
    h1 { color: #00dc82; margin-top: 0; }
    code { background: #1b212c; padding: 4px 8px; border-radius: 4px; font-family: monospace; color: #38bdf8; }
  </style>
</head>
<body>
  <div class="card">
    <h1>⚡ Mobile Hosted Site</h1>
    <p>Your personal static website is live!</p>
    <p>Drop your custom HTML, CSS, images, and JavaScript into <code>~/server/public_html</code> to publish here.</p>
  </div>
</body>
</html>""")
    except Exception:
        pass

# State for Cloudflare Tunnel
_tunnel_proc = None
_tunnel_url = None

def get_device_info_meta():
    """Dynamically autodetects device model, brand, OS version, and CPU architecture."""
    env_name = os.environ.get("SERVER_DEVICE_NAME")
    brand = ""
    model = ""
    try:
        p_brand = subprocess.run(["getprop", "ro.product.brand"], capture_output=True, text=True, timeout=1)
        if p_brand.returncode == 0:
            brand = p_brand.stdout.strip().capitalize()
        p_model = subprocess.run(["getprop", "ro.product.model"], capture_output=True, text=True, timeout=1)
        if p_model.returncode == 0:
            model = p_model.stdout.strip()
    except Exception:
        pass

    if env_name:
        device_name = env_name
    elif model:
        device_name = f"{brand} {model}".strip()
    else:
        sys_name = platform.system()
        node_name = platform.node()
        if sys_name == "Windows":
            device_name = f"Windows Host ({node_name})"
        elif sys_name == "Darwin":
            device_name = f"macOS Host ({node_name})"
        elif sys_name == "Linux":
            if "com.termux" in os.environ.get("PREFIX", "") or os.path.exists("/data/data/com.termux"):
                device_name = "Android (Termux Host)"
            else:
                device_name = f"Linux Host ({node_name})"
        else:
            device_name = f"Host ({node_name})"

    os_ver = "Unknown"
    try:
        p_os = subprocess.run(["getprop", "ro.build.version.release"], capture_output=True, text=True, timeout=1)
        if p_os.returncode == 0 and p_os.stdout.strip():
            os_ver = f"Android {p_os.stdout.strip()}"
    except Exception:
        pass
    if os_ver == "Unknown":
        os_ver = f"{platform.system()} {platform.release()}"

    arch = platform.machine() or "unknown"
    return {"device_name": device_name, "os_version": os_ver, "arch": arch}

# Real internal storage (for the Storage Usage stat only — NOT the
# file manager root). Requires `termux-setup-storage` once.
STORAGE_STAT_PATH = "/storage/emulated/0"

MAX_UPLOAD_BYTES = 300 * 1024 * 1024  # 300 MB per upload

ALLOWED_LOG_FILES = {"server.log", "api-stdout.log", "dashboard-stdout.log"}
MEDIA_PREVIEW_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".mp4", ".webm", ".mov",
    ".mp3", ".wav", ".ogg", ".m4a",
}

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(FILE_ROOT, exist_ok=True)

START_TIME = time.time()

# ================================================================
# LOGGING (rotating, size-capped so it can't fill phone storage)
# ================================================================
logger = logging.getLogger("a23server")
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "server.log"), maxBytes=2 * 1024 * 1024, backupCount=3
)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)


def log_event(message, level="info"):
    getattr(logger, level, logger.info)(message)


# ================================================================
# AUTH — per-install token & 6-digit PIN, Authorization: Bearer
# ================================================================
def load_or_create_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                tok = f.read().strip()
                if tok:
                    return tok
        except Exception:
            pass
    tok = secrets.token_hex(16)
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(tok)
        os.chmod(TOKEN_FILE, 0o600)
    except Exception as err:
        print(f"[token] could not persist token file: {err}")
    return tok


def load_or_create_pin():
    if os.path.exists(PIN_FILE):
        try:
            with open(PIN_FILE) as f:
                pin = f.read().strip()
                if pin and len(pin) == 6 and pin.isdigit():
                    return pin
        except Exception:
            pass
    pin = f"{secrets.randbelow(900000) + 100000}"
    try:
        with open(PIN_FILE, "w") as f:
            f.write(pin)
        os.chmod(PIN_FILE, 0o600)
    except Exception as err:
        print(f"[pin] could not persist pin file: {err}")
    return pin


API_TOKEN = load_or_create_token()
API_PIN = load_or_create_pin()

# ================================================================
# SECURITY: Brute-Force Rate Limiter & Constant-Time Auth
# ================================================================
_failed_auth_attempts = {}  # ip -> [timestamps]
_banned_ips = {}            # ip -> ban_until_timestamp
MAX_FAILED_ATTEMPTS = 10
BAN_DURATION_SECONDS = 300  # 5 minutes lockout

def is_ip_banned(ip):
    now = time.time()
    ban_until = _banned_ips.get(ip, 0)
    if now < ban_until:
        return True
    if ip in _banned_ips:
        del _banned_ips[ip]
    return False

def record_failed_attempt(ip):
    now = time.time()
    attempts = [t for t in _failed_auth_attempts.get(ip, []) if now - t < 60]
    attempts.append(now)
    _failed_auth_attempts[ip] = attempts
    if len(attempts) >= MAX_FAILED_ATTEMPTS:
        _banned_ips[ip] = now + BAN_DURATION_SECONDS
        log_event(f"SECURITY ALERT: IP {ip} temporarily locked out for {BAN_DURATION_SECONDS}s due to failed token attempts.", level="warning")

def extract_token():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.args.get("token")


def require_token(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        client_ip = request.remote_addr or "127.0.0.1"
        if is_ip_banned(client_ip):
            return jsonify({"error": "Too many failed attempts. Temporarily locked out for 5 minutes."}), 429

        token = extract_token()
        is_token_ok = bool(token and API_TOKEN and secrets.compare_digest(token, API_TOKEN))
        is_pin_ok = bool(token and API_PIN and secrets.compare_digest(token, API_PIN))

        if not (is_token_ok or is_pin_ok):
            record_failed_attempt(client_ip)
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)

    return wrapper


# ================================================================
# APP SETUP & SECURITY HEADERS
# ================================================================
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

LOCAL_ORIGIN_RE = re.compile(
    r"^https?://(?:10(?:\.\d{1,3}){3}"
    r"|172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2}"
    r"|192\.168(?:\.\d{1,3}){2}"
    r"|127\.0\.0\.1|localhost)(?::\d+)?$"
)
CORS(app, origins=LOCAL_ORIGIN_RE)


@app.after_request
def apply_security_headers_and_log(response):
    # Defense-in-depth security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    # Redact sensitive query parameters (e.g. ?token=) from logs
    try:
        clean_path = request.path
        logger.info(f"{request.remote_addr} {request.method} {clean_path} -> {response.status_code}")
    except Exception:
        pass
    return response


@app.route("/api/auth/pin", methods=["POST"])
def auth_pin():
    """Validates 6-digit PIN and returns full API token for zero-friction client login."""
    client_ip = request.remote_addr or "127.0.0.1"
    if is_ip_banned(client_ip):
        return jsonify({"error": "Too many failed attempts. Temporarily locked out for 5 minutes."}), 429

    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin", "")).strip().replace(" ", "").replace("-", "")

    if pin and API_PIN and secrets.compare_digest(pin, API_PIN):
        log_event(f"Successful 6-digit PIN authentication from {client_ip}")
        return jsonify({"success": True, "token": API_TOKEN})

    record_failed_attempt(client_ip)
    return jsonify({"error": "Invalid 6-digit PIN"}), 401


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "File too large"}), 413


@app.errorhandler(404)
def not_found(_e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    # Never leak a traceback or internal detail to the browser.
    logger.error(f"Unhandled error: {e}")
    return jsonify({"error": "Internal server error"}), 500


# ================================================================
# SECURITY: confine all file access to FILE_ROOT (~/server/shared)
# ================================================================
def safe_join(root, rel_path):
    root_real = os.path.realpath(root)
    rel_path = (rel_path or "").replace("\\", "/").lstrip("/")
    if "\x00" in rel_path:
        raise ValueError("Invalid path")
    candidate = os.path.realpath(os.path.join(root_real, rel_path))
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        raise ValueError("Path traversal attempt blocked")
    return candidate


def to_relative(abs_path):
    rel = os.path.relpath(abs_path, os.path.realpath(FILE_ROOT))
    return "" if rel == "." else rel.replace(os.sep, "/")


# ================================================================
# DEVICE / SYSTEM INFO
# ================================================================
def get_uptime_seconds():
    return int(time.time() - START_TIME)


def get_storage_info():
    """Real usage of actual phone storage, with fallback to local drive when previewing."""
    stat_path = STORAGE_STAT_PATH if os.path.isdir(STORAGE_STAT_PATH) else SERVER_DIR
    try:
        usage = shutil.disk_usage(stat_path)
        gb = 1024 ** 3
        return {
            "total_gb": round(usage.total / gb, 2),
            "used_gb": round(usage.used / gb, 2),
            "free_gb": round(usage.free / gb, 2),
        }
    except Exception as err:
        log_event(f"Storage read failed: {err}", level="error")
        return {"total_gb": None, "used_gb": None, "free_gb": None}


def get_battery_info():
    fields = {
        "percentage": None, "level": None, "health": None,
        "temperature": None, "voltage": None, "current": None,
        "current_average": None, "plugged": None, "status": None,
        "technology": None, "charge_counter": None, "cycle": None,
    }
    # 1. Termux API (Android)
    try:
        result = subprocess.run(
            ["termux-battery-status"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            raw = json.loads(result.stdout)
            for key in ("percentage", "health", "temperature", "voltage", "current",
                        "current_average", "plugged", "status", "technology",
                        "charge_counter", "cycle"):
                fields[key] = raw.get(key)
            if fields["percentage"] is not None:
                fields["level"] = round(fields["percentage"] / 100, 2)
            return fields
    except Exception:
        pass

    # 2. psutil fallback (Laptop / Desktop / Local Preview)
    if psutil and hasattr(psutil, "sensors_battery"):
        try:
            bat = psutil.sensors_battery()
            if bat:
                fields["percentage"] = round(bat.percent, 1)
                fields["level"] = round(bat.percent / 100, 2)
                fields["plugged"] = "PLUGGED_AC" if bat.power_plugged else "UNPLUGGED"
                fields["status"] = "CHARGING" if bat.power_plugged else "DISCHARGING"
                fields["health"] = "GOOD"
                return fields
        except Exception:
            pass

    return fields


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as err:
        log_event(f"Could not determine local IP: {err}", level="warning")
        return None


def get_network_interface():
    try:
        result = subprocess.run(
            ["ip", "route", "get", "8.8.8.8"], capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            parts = result.stdout.split()
            if "dev" in parts:
                return parts[parts.index("dev") + 1]
    except Exception:
        pass
    return None


_net_sample_cache = {}


def read_net_bytes(interface):
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if ":" not in line:
                    continue
                name, data = line.split(":", 1)
                if name.strip() == interface:
                    fields = data.split()
                    return int(fields[0]), int(fields[8])  # rx_bytes, tx_bytes
    except Exception:
        pass
    return None, None


def get_network_traffic(interface):
    empty = {"rx_bytes": None, "tx_bytes": None, "rx_rate_bps": None, "tx_rate_bps": None}
    now = time.time()
    if interface:
        rx, tx = read_net_bytes(interface)
        if rx is not None:
            prev = _net_sample_cache.get(interface)
            rx_rate = tx_rate = None
            if prev:
                prev_rx, prev_tx, prev_time = prev
                dt = now - prev_time
                if 0 < dt < 30:
                    rx_rate = round(max(0, (rx - prev_rx) / dt), 1)
                    tx_rate = round(max(0, (tx - prev_tx) / dt), 1)
            _net_sample_cache[interface] = (rx, tx, now)
            return {"rx_bytes": rx, "tx_bytes": tx, "rx_rate_bps": rx_rate, "tx_rate_bps": tx_rate}

    # psutil fallback for Windows / local preview
    if psutil:
        try:
            net_io = psutil.net_io_counters()
            rx, tx = net_io.bytes_recv, net_io.bytes_sent
            prev = _net_sample_cache.get("__psutil__")
            rx_rate = tx_rate = None
            if prev:
                prev_rx, prev_tx, prev_time = prev
                dt = now - prev_time
                if 0 < dt < 30:
                    rx_rate = round(max(0, (rx - prev_rx) / dt), 1)
                    tx_rate = round(max(0, (tx - prev_tx) / dt), 1)
            _net_sample_cache["__psutil__"] = (rx, tx, now)
            return {"rx_bytes": rx, "tx_bytes": tx, "rx_rate_bps": rx_rate, "tx_rate_bps": tx_rate}
        except Exception:
            pass

    return empty


def check_port_open(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def find_pid(pattern):
    """Best-effort PID lookup by command-line pattern. Never raises."""
    try:
        result = subprocess.run(["pgrep", "-af", pattern], capture_output=True, text=True, timeout=3)
        if result.returncode == 0 and result.stdout.strip():
            first_line = result.stdout.strip().splitlines()[0]
            return int(first_line.split()[0])
    except Exception:
        pass

    # psutil fallback (cross-platform)
    if psutil:
        try:
            my_pid = os.getpid()
            for proc in psutil.process_iter(["pid", "cmdline"]):
                if proc.info["pid"] == my_pid:
                    continue
                cmd = " ".join(proc.info.get("cmdline") or [])
                if pattern in cmd:
                    return proc.info["pid"]
        except Exception:
            pass
    return None


def read_pid_file(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # raises if the process is gone
        return pid
    except Exception:
        return None


def get_ssh_status():
    """Tri-state: online / offline / unknown — never guessed."""
    for p in (SSH_DEFAULT_PORT, 22):
        if check_port_open("127.0.0.1", p):
            return {"status": "online", "port": p}
    try:
        result = subprocess.run(["pgrep", "-f", "sshd"], capture_output=True, text=True, timeout=3)
    except Exception:
        return {"status": "unknown", "port": None}
    if result.returncode == 0 and result.stdout.strip():
        return {"status": "online", "port": None}
    if result.returncode == 1:
        return {"status": "offline", "port": None}
    return {"status": "unknown", "port": None}


def get_termux_username():
    try:
        return getpass.getuser()
    except Exception:
        return None


# ---- CPU / RAM: Android/Termux-compatible, no psutil, no root ----
def _read_cpu_snapshot():
    with open("/proc/stat") as f:
        line = f.readline()
    nums = list(map(int, line.split()[1:8]))  # user nice system idle iowait irq softirq
    idle = nums[3] + nums[4]
    return idle, sum(nums)


def get_cpu_usage():
    try:
        idle1, total1 = _read_cpu_snapshot()
        time.sleep(0.15)
        idle2, total2 = _read_cpu_snapshot()
        dtotal = total2 - total1
        if dtotal <= 0:
            return None
        usage = (1 - (idle2 - idle1) / dtotal) * 100
        return round(max(0, min(100, usage)), 1)
    except Exception:
        pass

    # psutil fallback
    if psutil:
        try:
            return round(psutil.cpu_percent(interval=0.1), 1)
        except Exception:
            pass
    return None


def get_ram_usage():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    info[parts[0].strip()] = int(parts[1].strip().split()[0])
        total = info.get("MemTotal")
        avail = info.get("MemAvailable")
        if not total or avail is None:
            return None
        return round(max(0, min(100, (total - avail) / total * 100)), 1)
    except Exception:
        pass

    # psutil fallback
    if psutil:
        try:
            return round(psutil.virtual_memory().percent, 1)
        except Exception:
            pass
    return None


# ================================================================
# HELPERS: EXTENDED SERVER & CLOUD SERVICES
# ================================================================

# 1. Shared Download Links
def _load_links():
    if not os.path.exists(LINKS_FILE):
        return {}
    try:
        with open(LINKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_links(links):
    try:
        with open(LINKS_FILE, "w", encoding="utf-8") as f:
            json.dump(links, f, indent=2)
    except Exception as e:
        log_event(f"Failed to save shared links: {e}", level="error")

def _prune_expired_links():
    links = _load_links()
    now = int(time.time())
    changed = False
    active = {}
    for lid, data in links.items():
        exp = data.get("expires_at", 0)
        if exp > 0 and exp < now:
            changed = True
            continue
        max_d = data.get("max_downloads", 0)
        cnt = data.get("downloads", 0)
        if max_d > 0 and cnt >= max_d:
            changed = True
            continue
        active[lid] = data
    if changed:
        _save_links(active)
    return active

# 2. Trash Manifest
def _load_trash():
    if not os.path.exists(TRASH_MANIFEST_FILE):
        return []
    try:
        with open(TRASH_MANIFEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def _save_trash(items):
    try:
        with open(TRASH_MANIFEST_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
    except Exception as e:
        log_event(f"Failed to save trash manifest: {e}", level="error")

# 3. Dynamic DNS (DuckDNS / Custom)
def _load_ddns_config():
    default_cfg = {
        "enabled": False,
        "service": "duckdns",
        "domain": "",
        "token": "",
        "interval_minutes": 30,
        "last_update": 0,
        "last_status": "Not configured",
        "last_ip": ""
    }
    if not os.path.exists(DDNS_FILE):
        return default_cfg
    try:
        with open(DDNS_FILE, "r", encoding="utf-8") as f:
            c = json.load(f)
            return {**default_cfg, **c}
    except Exception:
        return default_cfg

def _save_ddns_config(cfg):
    try:
        with open(DDNS_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log_event(f"Failed to save DDNS config: {e}", level="error")

def _trigger_ddns_update():
    cfg = _load_ddns_config()
    domain = cfg.get("domain", "").strip()
    tok = cfg.get("token", "").strip()
    if not domain or not tok:
        cfg["last_status"] = "Missing domain or token"
        _save_ddns_config(cfg)
        return "Missing domain or token"

    try:
        url = f"https://www.duckdns.org/update?domains={domain}&token={tok}&ip="
        req = urllib.request.Request(url, headers={"User-Agent": "MobileHostedServer/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode().strip()
            if body == "OK":
                cfg["last_status"] = "Success (OK)"
                cfg["last_update"] = int(time.time())
                log_event(f"DDNS DuckDNS sync OK: {domain}.duckdns.org")
            else:
                cfg["last_status"] = f"Failed ({body})"
                log_event(f"DDNS sync error: {body}", level="warning")
    except Exception as err:
        cfg["last_status"] = f"Network error: {err}"
        log_event(f"DDNS sync network error: {err}", level="error")

    _save_ddns_config(cfg)
    return cfg["last_status"]

# 4. Webhook Alerts (Telegram & Discord)
def _load_alerts_config():
    default_cfg = {
        "enabled": False,
        "discord_webhook": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "triggers": {
            "high_temp": True,
            "max_temp_c": 42.0,
            "low_battery": True,
            "low_storage": True,
            "storage_threshold_pct": 90.0
        },
        "last_alerts": {}
    }
    if not os.path.exists(ALERTS_FILE):
        return default_cfg
    try:
        with open(ALERTS_FILE, "r", encoding="utf-8") as f:
            return {**default_cfg, **json.load(f)}
    except Exception:
        return default_cfg

def _save_alerts_config(cfg):
    try:
        with open(ALERTS_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log_event(f"Failed to save alerts config: {e}", level="error")

def _send_notification(title, message):
    cfg = _load_alerts_config()
    discord_url = cfg.get("discord_webhook", "").strip()
    tg_token = cfg.get("telegram_bot_token", "").strip()
    tg_chat_id = cfg.get("telegram_chat_id", "").strip()

    sent = []
    # 1. Discord
    if discord_url.startswith("https://discord.com/api/webhooks/"):
        try:
            payload = json.dumps({
                "content": f"🚨 **{title}**\n{message}\n*Server: {get_device_info_meta()['device_name']}*"
            }).encode("utf-8")
            req = urllib.request.Request(discord_url, data=payload, headers={"Content-Type": "application/json", "User-Agent": "MobileHostedServer/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status in (200, 204):
                    sent.append("Discord")
        except Exception as e:
            log_event(f"Discord alert failed: {e}", level="warning")

    # 2. Telegram
    if tg_token and tg_chat_id:
        try:
            tg_url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
            payload = json.dumps({
                "chat_id": tg_chat_id,
                "text": f"🚨 *{title}*\n{message}\n\n_Server: {get_device_info_meta()['device_name']}_",
                "parse_mode": "Markdown"
            }).encode("utf-8")
            req = urllib.request.Request(tg_url, data=payload, headers={"Content-Type": "application/json", "User-Agent": "MobileHostedServer/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    sent.append("Telegram")
        except Exception as e:
            log_event(f"Telegram alert failed: {e}", level="warning")

    log_event(f"Alert dispatched: {title} via {', '.join(sent) if sent else 'none'}")
    return sent

# 5. Storage Breakdown & Cache Cleaning
def _get_storage_breakdown():
    categories = {
        "media": {"bytes": 0, "count": 0, "exts": {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".ogg", ".jpg", ".jpeg", ".png", ".gif", ".webp"}},
        "documents": {"bytes": 0, "count": 0, "exts": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xls", ".xlsx", ".ppt", ".pptx", ".epub", ".md"}},
        "archives": {"bytes": 0, "count": 0, "exts": {".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz", ".iso"}},
        "code": {"bytes": 0, "count": 0, "exts": {".py", ".js", ".html", ".css", ".json", ".sh", ".c", ".cpp", ".java", ".sql", ".xml", ".yaml", ".yml"}},
        "logs_system": {"bytes": 0, "count": 0, "exts": set()},
        "other": {"bytes": 0, "count": 0, "exts": set()}
    }

    # Scan FILE_ROOT
    for root, _, files in os.walk(FILE_ROOT):
        for f in files:
            p = os.path.join(root, f)
            try:
                sz = os.path.getsize(p)
                ext = os.path.splitext(f)[1].lower()
                matched = False
                for cat in ("media", "documents", "archives", "code"):
                    if ext in categories[cat]["exts"]:
                        categories[cat]["bytes"] += sz
                        categories[cat]["count"] += 1
                        matched = True
                        break
                if not matched:
                    categories["other"]["bytes"] += sz
                    categories["other"]["count"] += 1
            except Exception:
                pass

    # Scan LOG_DIR and TRASH_DIR
    for d in (LOG_DIR, TRASH_DIR):
        for root, _, files in os.walk(d):
            for f in files:
                try:
                    sz = os.path.getsize(os.path.join(root, f))
                    categories["logs_system"]["bytes"] += sz
                    categories["logs_system"]["count"] += 1
                except Exception:
                    pass

    total_shared_bytes = sum(c["bytes"] for c in categories.values())
    return {
        "categories": {k: {"bytes": v["bytes"], "mb": round(v["bytes"] / 1048576, 2), "count": v["count"]} for k, v in categories.items()},
        "total_managed_bytes": total_shared_bytes,
        "total_managed_mb": round(total_shared_bytes / 1048576, 2)
    }

def _clean_temp_cache():
    freed = 0
    # Clean trash
    manifest = _load_trash()
    for item in manifest:
        tp = os.path.join(TRASH_DIR, item.get("trash_name", ""))
        try:
            if os.path.isdir(tp):
                freed += sum(os.path.getsize(os.path.join(dr, fl)) for dr, _, fls in os.walk(tp) for fl in fls)
                shutil.rmtree(tp, ignore_errors=True)
            elif os.path.isfile(tp):
                freed += os.path.getsize(tp)
                os.remove(tp)
        except Exception:
            pass
    _save_trash([])

    # Clean rotated logs and pycache
    for root, dirs, files in os.walk(SERVER_DIR):
        if "__pycache__" in dirs:
            pyc_p = os.path.join(root, "__pycache__")
            try:
                freed += sum(os.path.getsize(os.path.join(dr, fl)) for dr, _, fls in os.walk(pyc_p) for fl in fls)
                shutil.rmtree(pyc_p, ignore_errors=True)
            except Exception:
                pass
        for f in files:
            if re.search(r"\.log\.\d+$", f) or f.endswith(".tmp"):
                p = os.path.join(root, f)
                try:
                    freed += os.path.getsize(p)
                    os.remove(p)
                except Exception:
                    pass
    return freed

# 6. Android Wake-Lock & Governor
def _get_wake_lock_status():
    try:
        if os.path.exists("/sys/power/wake_lock"):
            with open("/sys/power/wake_lock", "r") as f:
                content = f.read().strip()
                if content:
                    return {"active": True, "details": content}
    except Exception:
        pass
    if find_pid("termux-wake-lock"):
        return {"active": True, "details": "termux-wake-lock held"}
    return {"active": False, "details": "No active wake-lock held"}

def _get_cpu_governor():
    for path in ("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"):
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    return f.read().strip()
        except Exception:
            pass
    return "balanced"

# 7. Tailscale VPN
def _get_tailscale_info():
    ts_bin = shutil.which("tailscale")
    installed = ts_bin is not None
    running = False
    ip = None
    magic_dns = None
    online_peers = 0

    if installed:
        try:
            p = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=2)
            if p.returncode == 0:
                data = json.loads(p.stdout)
                running = bool(data.get("BackendState") == "Running")
                self_node = data.get("Self", {})
                ips = self_node.get("TailscaleIPs", [])
                if ips:
                    ip = ips[0]
                magic_dns = self_node.get("DNSName", "").rstrip(".")
                peers = data.get("Peer", {})
                online_peers = sum(1 for p in peers.values() if p.get("Online"))
        except Exception:
            pass

    if not ip and psutil:
        try:
            for iface, addrs in psutil.net_if_addrs().items():
                if "tailscale" in iface.lower() or "tun" in iface.lower():
                    for a in addrs:
                        if a.family == socket.AF_INET and a.address.startswith("100."):
                            ip = a.address
                            running = True
                            break
        except Exception:
            pass

    return {
        "installed": installed,
        "running": running,
        "ip": ip,
        "magic_dns": magic_dns,
        "online_peers": online_peers
    }

# 8. Scheduled Tasks (Cron)
DEFAULT_CRON_TASKS = [
    {
        "id": "task_auto_backup",
        "name": "Daily Shared Storage Backup",
        "action": "backup_shared",
        "interval_hours": 24,
        "enabled": False,
        "last_run": 0
    },
    {
        "id": "task_clean_cache",
        "name": "Clean Cache & Old Trash",
        "action": "clean_cache",
        "interval_hours": 12,
        "enabled": True,
        "last_run": 0
    },
    {
        "id": "task_rotate_logs",
        "name": "Prune Server Logs",
        "action": "clear_logs",
        "interval_hours": 48,
        "enabled": False,
        "last_run": 0
    },
    {
        "id": "task_ddns_sync",
        "name": "Auto Sync Dynamic DNS",
        "action": "ddns_update",
        "interval_hours": 1,
        "enabled": False,
        "last_run": 0
    }
]

def _load_cron_tasks():
    if not os.path.exists(CRON_FILE):
        return DEFAULT_CRON_TASKS
    try:
        with open(CRON_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_CRON_TASKS

def _save_cron_tasks(tasks):
    try:
        with open(CRON_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, indent=2)
    except Exception as e:
        log_event(f"Failed to save cron tasks: {e}", level="error")

def _execute_cron_action(action):
    if action == "backup_shared":
        stamp = time.strftime("%Y%m%d_%H%M%S")
        out_name = f"shared_backup_{stamp}.zip"
        dest_zip = os.path.join(BACKUP_DIR, out_name)
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(FILE_ROOT):
                for file in files:
                    full_p = os.path.join(root, file)
                    rel_p = os.path.relpath(full_p, FILE_ROOT)
                    zf.write(full_p, rel_p)
        log_event(f"Cron: Created backup {out_name}")
        return f"Backup created: {out_name}"
    elif action == "clean_cache":
        freed = _clean_temp_cache()
        log_event(f"Cron: Cleaned cache ({freed} bytes freed)")
        return f"Cache cleaned ({round(freed/1048576, 2)} MB freed)"
    elif action == "clear_logs":
        srv_log = os.path.join(LOG_DIR, "server.log")
        if os.path.exists(srv_log):
            with open(srv_log, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[-200:]
            with open(srv_log, "w", encoding="utf-8") as f:
                f.writelines(lines)
        log_event("Cron: Rotated and truncated server log")
        return "Logs rotated"
    elif action == "ddns_update":
        st = _trigger_ddns_update()
        return f"DDNS update: {st}"
    return "Unknown action"

# 9. 24-Hour Telemetry History
_telemetry_history = []

def _init_telemetry_history():
    global _telemetry_history
    if os.path.exists(TELEMETRY_HISTORY_FILE):
        try:
            with open(TELEMETRY_HISTORY_FILE, "r", encoding="utf-8") as f:
                _telemetry_history = json.load(f)
        except Exception:
            _telemetry_history = []

def _record_telemetry_snapshot():
    global _telemetry_history
    now = int(time.time())
    try:
        cpu = get_cpu_usage() or 0
        ram = get_ram_usage() or 0
        stor = get_storage_info()
        stor_pct = 0
        if stor and stor.get("total_gb") and stor.get("used_gb"):
            stor_pct = round((stor["used_gb"] / stor["total_gb"]) * 100, 1)
        bat = get_battery_info()
        bat_level = bat.get("percentage", 0) or 0
        bat_temp = bat.get("temperature", 0) or 0
        snap = {
            "ts": now,
            "cpu": cpu,
            "ram": ram,
            "storage_pct": stor_pct,
            "battery": bat_level,
            "temp_c": bat_temp,
        }
        _telemetry_history.append(snap)
        if len(_telemetry_history) > 1440:
            _telemetry_history = _telemetry_history[-1440:]
    except Exception:
        pass

# 10. Background Supervisor Thread
def _background_supervisor():
    _init_telemetry_history()
    last_60s_run = 0
    last_5m_save = 0

    while True:
        try:
            now = time.time()
            if now - last_60s_run >= 58:
                last_60s_run = now
                _record_telemetry_snapshot()

                if now - last_5m_save >= 300:
                    last_5m_save = now
                    try:
                        with open(TELEMETRY_HISTORY_FILE, "w", encoding="utf-8") as f:
                            json.dump(_telemetry_history, f)
                    except Exception:
                        pass

                tasks = _load_cron_tasks()
                tasks_changed = False
                for t in tasks:
                    if not t.get("enabled"):
                        continue
                    int_sec = t.get("interval_hours", 24) * 3600
                    last_r = t.get("last_run", 0)
                    if now - last_r >= int_sec:
                        try:
                            _execute_cron_action(t.get("action"))
                            t["last_run"] = int(now)
                            tasks_changed = True
                        except Exception as e:
                            log_event(f"Cron task {t.get('name')} failed: {e}", level="error")
                if tasks_changed:
                    _save_cron_tasks(tasks)

                ddns_cfg = _load_ddns_config()
                if ddns_cfg.get("enabled"):
                    int_sec = max(5, ddns_cfg.get("interval_minutes", 30)) * 60
                    if now - ddns_cfg.get("last_update", 0) >= int_sec:
                        _trigger_ddns_update()

                alerts_cfg = _load_alerts_config()
                if alerts_cfg.get("enabled"):
                    triggers = alerts_cfg.get("triggers", {})
                    last_alerts = alerts_cfg.get("last_alerts", {})
                    bat = get_battery_info()
                    temp = bat.get("temperature", 0) or 0
                    pct = bat.get("percentage", 100) or 100
                    plugged = bat.get("plugged")

                    if triggers.get("high_temp") and temp >= triggers.get("max_temp_c", 42):
                        if now - last_alerts.get("temp", 0) > 3600:
                            _send_notification("High Battery Temperature Alert", f"Battery temperature reached {temp}°C! Unplug charger or cool device to prevent swelling.")
                            last_alerts["temp"] = int(now)
                            alerts_cfg["last_alerts"] = last_alerts
                            _save_alerts_config(alerts_cfg)

                    if triggers.get("low_battery") and pct <= 20 and plugged == "UNPLUGGED":
                        if now - last_alerts.get("battery", 0) > 3600:
                            _send_notification("Low Battery Alert", f"Battery is at {pct}% and running on UPS power. Reconnect charger to prevent shutdown.")
                            last_alerts["battery"] = int(now)
                            alerts_cfg["last_alerts"] = last_alerts
                            _save_alerts_config(alerts_cfg)

                    if triggers.get("low_storage"):
                        stor = get_storage_info()
                        if stor and stor.get("total_gb") and stor.get("used_gb"):
                            used_pct = (stor["used_gb"] / stor["total_gb"]) * 100
                            thresh = triggers.get("storage_threshold_pct", 90)
                            if used_pct >= thresh and (now - last_alerts.get("storage", 0) > 7200):
                                _send_notification("Low Disk Storage Alert", f"Server disk usage is at {round(used_pct, 1)}%! Clean storage cache or remove old files.")
                                last_alerts["storage"] = int(now)
                                alerts_cfg["last_alerts"] = last_alerts
                                _save_alerts_config(alerts_cfg)

        except Exception as err:
            log_event(f"Background supervisor error: {err}", level="error")

        time.sleep(30)

# Start supervisor daemon thread
threading.Thread(target=_background_supervisor, daemon=True).start()


# ================================================================
# ROUTES: dashboard / status / services / network / server-info
# ================================================================
@app.route("/", methods=["GET"])
@app.route("/dashboard", methods=["GET"])
def dashboard_home():
    index_path = os.path.join(SERVER_DIR, "index.html")
    if os.path.isfile(index_path):
        return send_file(index_path)
    return jsonify({"message": "Mobile Hosted Server API is running"}), 200


@app.route("/api/status", methods=["GET"])
def status():
    dev_meta = get_device_info_meta()
    try:
        storage = get_storage_info()
    except Exception as err:
        log_event(f"Status: storage section failed: {err}", level="error")
        storage = {"total_gb": None, "used_gb": None, "free_gb": None}
    try:
        battery = get_battery_info()
    except Exception as err:
        log_event(f"Status: battery section failed: {err}", level="error")
        battery = {}
    try:
        network_ip = get_local_ip()
    except Exception:
        network_ip = None
    try:
        network_interface = get_network_interface()
    except Exception:
        network_interface = None
    try:
        ssh = get_ssh_status()
    except Exception:
        ssh = {"status": "unknown", "port": None}
    try:
        cpu = get_cpu_usage()
    except Exception:
        cpu = None
    try:
        ram = get_ram_usage()
    except Exception:
        ram = None

    # Battery Health Guard calculation
    swelling_risk = False
    guard_msg = "Battery state normal."
    bat_pct = battery.get("percentage")
    bat_plugged = battery.get("plugged")
    if bat_pct is not None and bat_pct >= 95 and bat_plugged in ("PLUGGED_AC", "PLUGGED_USB"):
        swelling_risk = True
        guard_msg = "Battery is at 100% and continuously charging. For 24/7 server longevity, consider unplugging or using a smart plug to avoid battery swelling."
    elif bat_pct is not None and bat_pct < 20 and bat_plugged == "UNPLUGGED":
        guard_msg = "Battery low (<20%). Connect charger to avoid unexpected server shutdown."

    battery_guard = {
        "swelling_risk": swelling_risk,
        "message": guard_msg,
    }

    tunnel_running = _tunnel_proc is not None and _tunnel_proc.poll() is None

    return jsonify({
        "device": dev_meta["device_name"],
        "os_version": dev_meta["os_version"],
        "arch": dev_meta["arch"],
        "status": "online",
        "uptime_seconds": get_uptime_seconds(),
        "storage": storage,
        "battery": battery,
        "battery_guard": battery_guard,
        "cpu": cpu,
        "ram": ram,
        "network": network_ip,
        "network_interface": network_interface,
        "ssh": ssh,
        "tunnel": {
            "running": tunnel_running,
            "url": _tunnel_url if tunnel_running else None,
        },
        "tailscale": _get_tailscale_info(),
        "ddns": _load_ddns_config(),
        "trash_count": len(_load_trash()),
        "links_count": len(_prune_expired_links()),
        "wake_lock": _get_wake_lock_status(),
        "cpu_governor": _get_cpu_governor(),
        "public_site": {"url": "/site/", "active": os.path.exists(_default_site_index)},
        "pin": API_PIN,
    })


@app.route("/api/services", methods=["GET"])
def services():
    dashboard_pid = read_pid_file(DASHBOARD_PID_FILE) or find_pid("http.server")
    dashboard_up = check_port_open("127.0.0.1", DASHBOARD_PORT)
    ssh_info = get_ssh_status()
    ssh_pid = find_pid("sshd") if ssh_info["status"] == "online" else None
    file_manager_ok = os.path.isdir(FILE_ROOT)
    tunnel_running = _tunnel_proc is not None and _tunnel_proc.poll() is None

    return jsonify([
        {
            "name": "Web Server", "process": "python http.server",
            "port": DASHBOARD_PORT, "pid": dashboard_pid if dashboard_up else None,
            "status": "online" if dashboard_up else "offline",
        },
        {
            "name": "Flask API", "process": "api.py",
            "port": API_PORT, "pid": os.getpid(),
            "status": "online",
        },
        {
            "name": "SSH", "process": "sshd",
            "port": ssh_info["port"], "pid": ssh_pid,
            "status": ssh_info["status"],
        },
        {
            "name": "File Manager", "process": "n/a",
            "port": None, "pid": None,
            "status": "online" if file_manager_ok else "unknown",
        },
        {
            "name": "Cloudflare Tunnel", "process": "cloudflared",
            "port": None, "pid": _tunnel_proc.pid if tunnel_running else None,
            "status": "online" if tunnel_running else "offline",
        },
    ])


@app.route("/api/network", methods=["GET"])
def network_info():
    interface = get_network_interface()
    ssh_info = get_ssh_status()
    return jsonify({
        "local_ip": get_local_ip(),
        "interface": interface,
        "dashboard_port": DASHBOARD_PORT,
        "api_port": API_PORT,
        "ssh_port": ssh_info["port"] or SSH_DEFAULT_PORT,
        "dashboard_reachable": check_port_open("127.0.0.1", DASHBOARD_PORT),
        "traffic": get_network_traffic(interface),
    })


@app.route("/api/server/info", methods=["GET"])
def server_info():
    dev_meta = get_device_info_meta()
    ip = get_local_ip()
    ssh_info = get_ssh_status()
    username = get_termux_username()
    connect_cmd = (
        f"ssh {username}@{ip} -p {ssh_info['port'] or SSH_DEFAULT_PORT}"
        if ip and username else None
    )
    return jsonify({
        "device": dev_meta["device_name"],
        "os_version": dev_meta["os_version"],
        "arch": dev_meta["arch"],
        "local_ip": ip,
        "dashboard_port": DASHBOARD_PORT,
        "api_port": API_PORT,
        "ssh": {**ssh_info, "connect_command": connect_cmd},
        "uptime_seconds": get_uptime_seconds(),
    })


# ================================================================
# ROUTES: CLOUDFLARE REMOTE TUNNEL
# ================================================================


@app.route("/api/tunnel/status", methods=["GET"])
def tunnel_status():
    global _tunnel_proc, _tunnel_url
    running = _tunnel_proc is not None and _tunnel_proc.poll() is None
    if not running:
        _tunnel_url = None
    installed = shutil.which("cloudflared") is not None
    return jsonify({
        "installed": installed,
        "running": running,
        "url": _tunnel_url,
    })


@app.route("/api/tunnel/start", methods=["POST"])
@require_token
def tunnel_start():
    global _tunnel_proc, _tunnel_url
    if not shutil.which("cloudflared"):
        return jsonify({
            "error": "cloudflared not found",
            "instructions": "Install via 'pkg install cloudflared' on Termux or 'winget install cloudflare.cloudflared' on Windows.",
        }), 400

    if _tunnel_proc is not None and _tunnel_proc.poll() is None:
        return jsonify({"status": "already_running", "url": _tunnel_url})

    _tunnel_url = None
    try:
        _tunnel_proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{DASHBOARD_PORT}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

        def monitor_tunnel(proc):
            global _tunnel_url
            for line in iter(proc.stdout.readline, ""):
                m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if m:
                    _tunnel_url = m.group(0)
                    log_event(f"Cloudflare Tunnel active at: {_tunnel_url}")
                    break
                if proc.poll() is not None:
                    break

        threading.Thread(target=monitor_tunnel, args=(_tunnel_proc,), daemon=True).start()
        time.sleep(1.5)
    except Exception as err:
        log_event(f"Failed to start tunnel: {err}", level="error")
        return jsonify({"error": f"Failed to start cloudflared: {err}"}), 500

    return jsonify({"status": "starting", "url": _tunnel_url})


@app.route("/api/tunnel/stop", methods=["POST"])
@require_token
def tunnel_stop():
    global _tunnel_proc, _tunnel_url
    if _tunnel_proc is not None:
        try:
            _tunnel_proc.terminate()
        except Exception:
            pass
        _tunnel_proc = None
    _tunnel_url = None
    log_event("Cloudflare Tunnel stopped")
    return jsonify({"status": "stopped"})


@app.route("/api/qrcode", methods=["GET"])
def qr_code_endpoint():
    """Generates a QR code PNG or SVG for easy mobile/Wi-Fi connection."""
    raw_data = request.args.get("data")
    if not raw_data:
        ip = get_local_ip() or "127.0.0.1"
        raw_data = f"http://{ip}:{DASHBOARD_PORT}"

    if qrcode:
        try:
            qr = qrcode.QRCode(box_size=8, border=2)
            qr.add_data(raw_data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#000000", back_color="#ffffff")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return send_file(buf, mimetype="image/png")
        except Exception:
            pass

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="220" height="220" viewBox="0 0 220 220">
      <rect width="220" height="220" fill="#ffffff" rx="8"/>
      <text x="110" y="100" fill="#111" font-family="sans-serif" font-size="12" text-anchor="middle">Scan URL:</text>
      <text x="110" y="125" fill="#00dc82" font-family="sans-serif" font-size="11" font-weight="bold" text-anchor="middle">{raw_data[:30]}</text>
    </svg>"""
    return Response(svg, mimetype="image/svg+xml")


# ================================================================
# ROUTES: file manager (confined to FILE_ROOT = ~/server/shared)
# ================================================================
@app.route("/api/files", methods=["GET"])
@require_token
def list_files():
    rel_path = request.args.get("path", "")
    try:
        target = safe_join(FILE_ROOT, rel_path)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isdir(target):
        return jsonify({"error": "Not a directory"}), 404

    entries = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    st = entry.stat()
                    ext = os.path.splitext(entry.name)[1].lower()
                    entries.append({
                        "name": entry.name,
                        "is_dir": entry.is_dir(),
                        "size_bytes": None if entry.is_dir() else st.st_size,
                        "modified": int(st.st_mtime),
                        "previewable": (not entry.is_dir()) and ext in MEDIA_PREVIEW_EXTS,
                    })
                except Exception:
                    continue
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403
    except Exception as err:
        log_event(f"List files failed: {err}", level="error")
        return jsonify({"error": "Could not list directory"}), 500

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    rel_display = to_relative(target)
    log_event(f"Listed directory: /{rel_display}")
    return jsonify({"path": rel_display, "entries": entries})


@app.route("/api/files/download", methods=["GET"])
@require_token
def download_file():
    rel_path = request.args.get("path", "")
    try:
        target = safe_join(FILE_ROOT, rel_path)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isfile(target):
        return jsonify({"error": "File not found"}), 404
    log_event(f"Downloaded file: {rel_path}")
    try:
        return send_file(target, as_attachment=True)
    except Exception as err:
        log_event(f"Download failed: {err}", level="error")
        return jsonify({"error": "Download failed"}), 500


@app.route("/api/files/stream", methods=["GET"])
@require_token
def stream_file():
    """Inline view for images/video/audio (Media Sharing preview)."""
    rel_path = request.args.get("path", "")
    try:
        target = safe_join(FILE_ROOT, rel_path)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isfile(target):
        return jsonify({"error": "File not found"}), 404
    log_event(f"Streamed file: {rel_path}")
    try:
        return send_file(target, as_attachment=False)
    except Exception as err:
        log_event(f"Stream failed: {err}", level="error")
        return jsonify({"error": "Could not stream file"}), 500


@app.route("/api/files/upload", methods=["POST"])
@require_token
def upload_file():
    """
    Handles both:
      - a single plain file upload (existing behavior, unchanged), and
      - one file of a folder upload, identified by an optional
        `relative_path` form field (e.g. "MyProject/css/style.css"),
        sent by the dashboard's Upload Folder feature — one request
        per file, so large folders never block the dashboard.

    Every path segment of `relative_path` is sanitized individually
    and the final destination is re-verified with safe_join(), so a
    crafted relative_path (e.g. "../../x") can never escape FILE_ROOT.
    """
    rel_path = request.form.get("path", "")
    try:
        target_dir = safe_join(FILE_ROOT, rel_path)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isdir(target_dir):
        return jsonify({"error": "Target directory not found"}), 404
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    relative_path = (request.form.get("relative_path") or "").strip()

    if relative_path:
        # Folder upload: rebuild the sub-path under the current
        # directory, sanitizing every segment (drops any "..", ".",
        # empty segments, or unsafe characters).
        relative_path = relative_path.replace("\\", "/")
        segments = [s for s in relative_path.split("/") if s not in ("", ".", "..")]
        safe_segments = [secure_filename(s) for s in segments]
        safe_segments = [s for s in safe_segments if s]
        if not safe_segments:
            return jsonify({"error": "Invalid relative path"}), 400

        sub_rel = "/".join(safe_segments)
        try:
            dest = safe_join(FILE_ROOT, f"{rel_path}/{sub_rel}" if rel_path else sub_rel)
        except ValueError:
            return jsonify({"error": "Invalid path"}), 400

        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
        except Exception as err:
            log_event(f"Could not create folder for upload: {err}", level="error")
            return jsonify({"error": "Could not create destination folder"}), 500

        filename = safe_segments[-1]
    else:
        # Plain single-file upload
        filename = secure_filename(file.filename or "")
        if not filename:
            return jsonify({"error": "Invalid filename"}), 400
        dest = os.path.join(target_dir, filename)

    # Security: Prohibit uploading executable scripts and binaries
    ext = os.path.splitext(filename)[1].lower()
    DANGEROUS_EXTS = {".exe", ".bat", ".cmd", ".sh", ".bash", ".ps1", ".vbs", ".pif", ".scr", ".com", ".apk", ".php", ".py", ".pl"}
    if ext in DANGEROUS_EXTS:
        log_event(f"Blocked dangerous upload attempt: {filename}", level="warning")
        return jsonify({"error": f"Upload blocked: '{ext}' executable/script files are prohibited for security."}), 400

    try:
        file.save(dest)
        try:
            os.chmod(dest, 0o644)
        except Exception:
            pass
    except Exception as err:
        log_event(f"Upload failed: {err}", level="error")
        return jsonify({"error": "Upload failed"}), 500

    uploaded_rel = to_relative(dest)
    log_event(f"Uploaded file: {uploaded_rel}")
    return jsonify({"message": "uploaded", "name": filename, "path": uploaded_rel})


@app.route("/api/files/upload-zip", methods=["POST"])
@require_token
def upload_zip():
    """Uploads a .zip archive, with an option to automatically extract its contents."""
    rel_path = request.form.get("path", "")
    extract = request.form.get("extract", "true").lower() in ("true", "1", "yes")
    try:
        target_dir = safe_join(FILE_ROOT, rel_path)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isdir(target_dir):
        return jsonify({"error": "Target directory not found"}), 404
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    filename = secure_filename(file.filename or "")
    if not filename.lower().endswith(".zip"):
        return jsonify({"error": "Only .zip archive files are accepted"}), 400

    zip_dest = os.path.join(target_dir, filename)
    try:
        file.save(zip_dest)
    except Exception as err:
        log_event(f"Failed to save zip upload: {err}", level="error")
        return jsonify({"error": f"Failed to save zip: {err}"}), 500

    extracted_count = 0
    if extract:
        try:
            with zipfile.ZipFile(zip_dest, "r") as zf:
                for member in zf.infolist():
                    member_path = member.filename.replace("\\", "/")
                    parts = [p for p in member_path.split("/") if p not in ("", ".", "..")]
                    if not parts or ".." in parts:
                        continue
                    dest_path = safe_join(target_dir, "/".join(parts))
                    if member.is_dir():
                        os.makedirs(dest_path, exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                        with zf.open(member) as source, open(dest_path, "wb") as target:
                            target.write(source.read())
                        extracted_count += 1
            log_event(f"Uploaded and extracted ZIP '{filename}' ({extracted_count} items) into {rel_path or 'root'}")
            return jsonify({
                "message": "extracted",
                "name": filename,
                "extracted_count": extracted_count,
                "path": to_relative(zip_dest)
            })
        except Exception as err:
            log_event(f"ZIP extraction error: {err}", level="error")
            return jsonify({
                "message": "uploaded_unextracted",
                "name": filename,
                "warning": f"Saved ZIP file, but extraction encountered an issue: {err}",
                "path": to_relative(zip_dest)
            })

    log_event(f"Uploaded ZIP archive '{filename}' into {rel_path or 'root'}")
    return jsonify({"message": "uploaded", "name": filename, "path": to_relative(zip_dest)})


@app.route("/api/files/create-folder", methods=["POST"])
@require_token
def create_folder():
    data = request.get_json(silent=True) or {}
    rel_path = data.get("path", "")
    name = secure_filename(data.get("name", ""))
    if not name:
        return jsonify({"error": "Invalid folder name"}), 400
    try:
        new_dir = safe_join(FILE_ROOT, f"{rel_path}/{name}" if rel_path else name)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if os.path.exists(new_dir):
        return jsonify({"error": "Already exists"}), 409
    try:
        os.makedirs(new_dir)
    except Exception as err:
        log_event(f"Create-folder failed: {err}", level="error")
        return jsonify({"error": "Could not create folder"}), 500
    log_event(f"Created folder: {rel_path}/{name}")
    return jsonify({"message": "created"})


@app.route("/api/files/rename", methods=["POST"])
@require_token
def rename_item():
    data = request.get_json(silent=True) or {}
    rel_path = data.get("path", "")
    new_name = secure_filename(data.get("new_name", ""))
    if not new_name:
        return jsonify({"error": "Invalid new name"}), 400
    try:
        source = safe_join(FILE_ROOT, rel_path)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.exists(source):
        return jsonify({"error": "Not found"}), 404

    parent_rel = os.path.dirname(rel_path.rstrip("/"))
    try:
        dest = safe_join(FILE_ROOT, f"{parent_rel}/{new_name}" if parent_rel else new_name)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if os.path.exists(dest):
        return jsonify({"error": "Target already exists"}), 409
    try:
        os.rename(source, dest)
    except Exception as err:
        log_event(f"Rename failed: {err}", level="error")
        return jsonify({"error": "Rename failed"}), 500
    log_event(f"Renamed: {rel_path} -> {new_name}")
    return jsonify({"message": "renamed"})


@app.route("/api/files", methods=["DELETE"])
@require_token
def delete_item():
    data = request.get_json(silent=True) or {}
    rel_path = data.get("path", "")
    permanent = bool(data.get("permanent") or request.args.get("permanent") == "true")
    if not rel_path:
        return jsonify({"error": "Path required"}), 400
    try:
        target = safe_join(FILE_ROOT, rel_path)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if target == os.path.realpath(FILE_ROOT):
        return jsonify({"error": "Cannot delete the shared root directory"}), 400
    if not os.path.exists(target):
        return jsonify({"error": "Not found"}), 404
    try:
        if permanent:
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            log_event(f"Permanently deleted: {rel_path}")
            return jsonify({"message": "permanently_deleted"})
        else:
            trash_id = secrets.token_hex(6)
            base_name = os.path.basename(target)
            trash_name = f"{trash_id}_{base_name}"
            dest = os.path.join(TRASH_DIR, trash_name)
            is_dir = os.path.isdir(target)
            sz = 0
            if is_dir:
                sz = sum(os.path.getsize(os.path.join(dr, fl)) for dr, _, fls in os.walk(target) for fl in fls)
            else:
                sz = os.path.getsize(target)
            shutil.move(target, dest)
            items = _load_trash()
            items.append({
                "id": trash_id,
                "name": base_name,
                "trash_name": trash_name,
                "original_path": rel_path,
                "is_dir": is_dir,
                "size": sz,
                "deleted_at": int(time.time())
            })
            _save_trash(items)
            log_event(f"Moved to trash: {rel_path} (ID: {trash_id})")
            return jsonify({"message": "moved_to_trash", "trash_id": trash_id})
    except Exception as err:
        log_event(f"Delete failed: {err}", level="error")
        return jsonify({"error": "Delete failed"}), 500


# ================================================================
# ROUTES: TEXT EDITOR & ZIP DOWNLOAD
# ================================================================
@app.route("/api/files/content", methods=["GET"])
@require_token
def get_file_content():
    rel_path = request.args.get("path", "")
    try:
        target = safe_join(FILE_ROOT, rel_path)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isfile(target):
        return jsonify({"error": "File not found"}), 404
    sz = os.path.getsize(target)
    if sz > 4 * 1024 * 1024:
        return jsonify({"error": "File too large to edit in browser (max 4 MB)"}), 400
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as err:
        return jsonify({"error": f"Failed to read file: {err}"}), 500
    filename = os.path.basename(target)
    ext = os.path.splitext(filename)[1].lower()
    return jsonify({"path": rel_path, "name": filename, "ext": ext, "size": sz, "content": content})


@app.route("/api/files/save", methods=["POST"])
@require_token
def save_file_content():
    data = request.get_json(silent=True) or {}
    rel_path = data.get("path", "")
    content = data.get("content", "")
    try:
        target = safe_join(FILE_ROOT, rel_path)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isfile(target):
        return jsonify({"error": "File not found"}), 404
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        log_event(f"Saved file edit: {rel_path} ({len(content)} chars)")
    except Exception as err:
        log_event(f"Failed to save file {rel_path}: {err}", level="error")
        return jsonify({"error": f"Could not save file: {err}"}), 500
    return jsonify({"status": "saved", "size": len(content.encode("utf-8"))})


@app.route("/api/files/zip", methods=["GET"])
@require_token
def download_folder_zip():
    rel_path = request.args.get("path", "")
    try:
        target = safe_join(FILE_ROOT, rel_path)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.exists(target):
        return jsonify({"error": "Not found"}), 404

    base_name = os.path.basename(target.rstrip("/\\")) or "shared_files"
    mem_buf = io.BytesIO()
    with zipfile.ZipFile(mem_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isdir(target):
            for root, _, files in os.walk(target):
                for f in files:
                    full_p = os.path.join(root, f)
                    arc_p = os.path.relpath(full_p, target)
                    zf.write(full_p, arc_p)
        else:
            zf.write(target, os.path.basename(target))

    mem_buf.seek(0)
    log_event(f"Downloaded ZIP archive for: {rel_path or 'root'}")
    return send_file(
        mem_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{base_name}.zip"
    )


# ================================================================
# ROUTES: SHAREABLE DOWNLOAD LINKS
# ================================================================
@app.route("/api/links/create", methods=["POST"])
@require_token
def create_share_link():
    data = request.get_json(silent=True) or {}
    rel_path = data.get("path", "")
    expires_in_hours = int(data.get("expires_in_hours", 24))
    pwd = (data.get("password") or "").strip()
    max_d = int(data.get("max_downloads", 0))

    try:
        target = safe_join(FILE_ROOT, rel_path)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isfile(target):
        return jsonify({"error": "File not found"}), 404

    link_id = secrets.token_urlsafe(12)
    now = int(time.time())
    expires_at = (now + expires_in_hours * 3600) if expires_in_hours > 0 else 0
    pwd_hash = hashlib.sha256(pwd.encode("utf-8")).hexdigest() if pwd else None

    links = _load_links()
    links[link_id] = {
        "id": link_id,
        "path": rel_path,
        "filename": os.path.basename(target),
        "created_at": now,
        "expires_at": expires_at,
        "has_password": bool(pwd),
        "password_hash": pwd_hash,
        "downloads": 0,
        "max_downloads": max_d
    }
    _save_links(links)
    log_event(f"Created shared link {link_id} for {rel_path}")
    return jsonify({
        "link_id": link_id,
        "share_url": f"/api/share/{link_id}",
        "expires_at": expires_at,
        "has_password": bool(pwd)
    })


@app.route("/api/links", methods=["GET"])
@require_token
def list_share_links():
    active = _prune_expired_links()
    result = []
    for lid, d in active.items():
        result.append({
            "id": lid,
            "path": d["path"],
            "filename": d.get("filename", os.path.basename(d["path"])),
            "created_at": d.get("created_at"),
            "expires_at": d.get("expires_at"),
            "has_password": d.get("has_password", False),
            "downloads": d.get("downloads", 0),
            "max_downloads": d.get("max_downloads", 0),
            "share_url": f"/api/share/{lid}"
        })
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return jsonify({"links": result})


@app.route("/api/links/<link_id>", methods=["DELETE"])
@require_token
def delete_share_link(link_id):
    links = _load_links()
    if link_id in links:
        del links[link_id]
        _save_links(links)
        log_event(f"Revoked shared link: {link_id}")
        return jsonify({"message": "Link revoked"})
    return jsonify({"error": "Link not found"}), 404


@app.route("/api/share/<link_id>", methods=["GET"])
def public_share_access(link_id):
    links = _prune_expired_links()
    link = links.get(link_id)
    if not link:
        return "<h3>Shared link expired or does not exist.</h3>", 404

    if link.get("has_password"):
        client_pwd = request.headers.get("X-Share-Password") or request.args.get("pwd", "")
        client_hash = hashlib.sha256(client_pwd.encode("utf-8")).hexdigest()
        if client_hash != link.get("password_hash"):
            return jsonify({"password_required": True, "error": "Password required or incorrect"}), 401

    try:
        target = safe_join(FILE_ROOT, link["path"])
    except ValueError:
        return "Invalid path", 400
    if not os.path.isfile(target):
        return "File not found", 404

    link["downloads"] = link.get("downloads", 0) + 1
    _save_links(links)
    log_event(f"Shared link download: {link_id} -> {link['path']}")
    return send_file(target, as_attachment=True, download_name=link.get("filename", os.path.basename(target)))


# ================================================================
# ROUTES: TRASH BIN (SAFE DELETE & RESTORE)
# ================================================================
@app.route("/api/trash", methods=["GET"])
@require_token
def list_trash():
    items = _load_trash()
    valid_items = []
    total_size = 0
    for it in items:
        tp = os.path.join(TRASH_DIR, it.get("trash_name", ""))
        if os.path.exists(tp):
            it["exists"] = True
            total_size += it.get("size", 0)
            valid_items.append(it)
    return jsonify({"items": valid_items, "total_size_bytes": total_size, "total_size_mb": round(total_size / 1048576, 2)})


@app.route("/api/trash/restore", methods=["POST"])
@require_token
def restore_trash_item():
    data = request.get_json(silent=True) or {}
    item_id = data.get("id")
    items = _load_trash()
    target_item = None
    remaining = []
    for it in items:
        if it.get("id") == item_id:
            target_item = it
        else:
            remaining.append(it)

    if not target_item:
        return jsonify({"error": "Trash item not found"}), 404

    src = os.path.join(TRASH_DIR, target_item["trash_name"])
    if not os.path.exists(src):
        _save_trash(remaining)
        return jsonify({"error": "Trash file missing"}), 404

    orig_rel = target_item.get("original_path", "")
    try:
        dest = safe_join(FILE_ROOT, orig_rel)
    except ValueError:
        dest = os.path.join(FILE_ROOT, os.path.basename(orig_rel))

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        base, ext = os.path.splitext(dest)
        dest = f"{base}_restored_{int(time.time())}{ext}"

    try:
        shutil.move(src, dest)
        _save_trash(remaining)
        log_event(f"Restored trash item: {orig_rel}")
        return jsonify({"status": "restored", "path": to_relative(dest)})
    except Exception as err:
        return jsonify({"error": f"Failed to restore: {err}"}), 500


@app.route("/api/trash/empty", methods=["DELETE"])
@require_token
def empty_trash():
    freed = _clean_temp_cache()
    log_event(f"Trash emptied ({round(freed/1048576, 2)} MB freed)")
    return jsonify({"status": "emptied", "freed_bytes": freed, "freed_mb": round(freed / 1048576, 2)})


# ================================================================
# ROUTES: STORAGE ANALYZER & CLEAN CACHE
# ================================================================
@app.route("/api/storage/breakdown", methods=["GET"])
@require_token
def storage_breakdown_route():
    data = _get_storage_breakdown()
    return jsonify(data)


@app.route("/api/storage/clean-cache", methods=["POST"])
@require_token
def clean_cache_route():
    freed = _clean_temp_cache()
    return jsonify({"status": "cleaned", "freed_bytes": freed, "freed_mb": round(freed / 1048576, 2)})


# ================================================================
# ROUTES: TAILSCALE, DDNS, POWER & TELEMETRY EXPORT
# ================================================================
@app.route("/api/tailscale/status", methods=["GET"])
def tailscale_status_route():
    return jsonify(_get_tailscale_info())


@app.route("/api/ddns/config", methods=["GET", "POST"])
@require_token
def ddns_config_route():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        cfg = _load_ddns_config()
        cfg["enabled"] = bool(data.get("enabled", cfg.get("enabled")))
        if "domain" in data:
            cfg["domain"] = data["domain"].strip()
        if "token" in data:
            cfg["token"] = data["token"].strip()
        if "interval_minutes" in data:
            cfg["interval_minutes"] = max(5, int(data["interval_minutes"]))
        _save_ddns_config(cfg)
        log_event("Updated DDNS configuration")
        return jsonify({"status": "saved", "config": cfg})
    return jsonify(_load_ddns_config())


@app.route("/api/ddns/update", methods=["POST"])
@require_token
def ddns_update_manual_route():
    res = _trigger_ddns_update()
    return jsonify({"status": res})


@app.route("/api/power/status", methods=["GET"])
def power_status_route():
    wl = _get_wake_lock_status()
    gov = _get_cpu_governor()
    bat = get_battery_info()
    return jsonify({
        "wake_lock": wl,
        "cpu_governor": gov,
        "battery": bat,
        "thermal_c": bat.get("temperature", 0)
    })


@app.route("/api/power/wakelock", methods=["POST"])
@require_token
def power_wakelock_route():
    data = request.get_json(silent=True) or {}
    action = data.get("action", "acquire")
    if action == "acquire":
        try:
            subprocess.run(["termux-wake-lock"], timeout=2)
            log_event("Acquired Android wake-lock via Termux API")
        except Exception:
            pass
        return jsonify({"status": "acquired", "active": True})
    else:
        try:
            subprocess.run(["termux-wake-unlock"], timeout=2)
            log_event("Released Android wake-lock via Termux API")
        except Exception:
            pass
        return jsonify({"status": "released", "active": False})


@app.route("/api/telemetry/export", methods=["GET"])
@require_token
def telemetry_export_route():
    fmt = request.args.get("format", "csv").lower()
    global _telemetry_history
    if fmt == "json":
        buf = io.BytesIO(json.dumps(_telemetry_history, indent=2).encode("utf-8"))
        buf.seek(0)
        return send_file(buf, mimetype="application/json", as_attachment=True, download_name="telemetry_24h.json")

    lines = ["timestamp,datetime,cpu_pct,ram_pct,storage_pct,battery_pct,temp_c"]
    for s in _telemetry_history:
        ts = s.get("ts", 0)
        dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        lines.append(f"{ts},{dt},{s.get('cpu', 0)},{s.get('ram', 0)},{s.get('storage_pct', 0)},{s.get('battery', 0)},{s.get('temp_c', 0)}")
    csv_bytes = "\n".join(lines).encode("utf-8")
    buf = io.BytesIO(csv_bytes)
    buf.seek(0)
    return send_file(buf, mimetype="text/csv", as_attachment=True, download_name="telemetry_24h.csv")


# ================================================================
# ROUTES: CRON SCHEDULER & WEBHOOK ALERTS
# ================================================================
@app.route("/api/cron", methods=["GET"])
@require_token
def cron_list_route():
    tasks = _load_cron_tasks()
    return jsonify({"tasks": tasks})


@app.route("/api/cron/save", methods=["POST"])
@require_token
def cron_save_route():
    data = request.get_json(silent=True) or {}
    task_id = data.get("id")
    tasks = _load_cron_tasks()
    found = False
    for t in tasks:
        if t["id"] == task_id:
            if "enabled" in data:
                t["enabled"] = bool(data["enabled"])
            if "interval_hours" in data:
                t["interval_hours"] = max(1, int(data["interval_hours"]))
            found = True
            break
    if not found and task_id:
        tasks.append(data)
    _save_cron_tasks(tasks)
    return jsonify({"status": "saved", "tasks": tasks})


@app.route("/api/cron/run/<task_id>", methods=["POST"])
@require_token
def cron_run_route(task_id):
    tasks = _load_cron_tasks()
    for t in tasks:
        if t["id"] == task_id:
            res = _execute_cron_action(t.get("action"))
            t["last_run"] = int(time.time())
            _save_cron_tasks(tasks)
            return jsonify({"status": "executed", "result": res})
    return jsonify({"error": "Task not found"}), 404


@app.route("/api/alerts/config", methods=["GET", "POST"])
@require_token
def alerts_config_route():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        cfg = _load_alerts_config()
        if "enabled" in data:
            cfg["enabled"] = bool(data["enabled"])
        if "discord_webhook" in data:
            cfg["discord_webhook"] = data["discord_webhook"].strip()
        if "telegram_bot_token" in data:
            cfg["telegram_bot_token"] = data["telegram_bot_token"].strip()
        if "telegram_chat_id" in data:
            cfg["telegram_chat_id"] = data["telegram_chat_id"].strip()
        if "triggers" in data and isinstance(data["triggers"], dict):
            cfg["triggers"] = {**cfg.get("triggers", {}), **data["triggers"]}
        _save_alerts_config(cfg)
        log_event("Alerts configuration updated")
        return jsonify({"status": "saved", "config": cfg})
    return jsonify(_load_alerts_config())


@app.route("/api/alerts/test", methods=["POST"])
@require_token
def alerts_test_route():
    sent = _send_notification("Test Alert Notification", "This is a test notification from your Mobile Hosted Server dashboard. Webhook alerts are working correctly!")
    if not sent:
        return jsonify({"error": "Failed to send test alert. Check webhook URL or bot credentials."}), 400
    return jsonify({"status": "sent", "channels": sent})


# ================================================================
# ROUTES: STATIC SITE HOSTING (~/server/public_html)
# ================================================================
@app.route("/site/", defaults={"subpath": "index.html"}, methods=["GET"])
@app.route("/site/<path:subpath>", methods=["GET"])
def serve_static_site(subpath):
    try:
        full_path = safe_join(PUBLIC_HTML_DIR, subpath)
    except ValueError:
        return "Invalid path", 400
    if os.path.isdir(full_path):
        subpath = os.path.join(subpath, "index.html").replace("\\", "/")
    return send_from_directory(PUBLIC_HTML_DIR, subpath)


# ================================================================
# ROUTES: logs (whitelisted filenames only — .token is never reachable)
# ================================================================
@app.route("/api/logs", methods=["GET"])
def get_logs():
    filename = request.args.get("file", "server.log")
    if filename not in ALLOWED_LOG_FILES:
        return jsonify({"error": "Invalid log file"}), 400
    lines = request.args.get("lines", default=100, type=int) or 100
    lines = max(1, min(lines, 1000))
    path = os.path.join(LOG_DIR, filename)
    if not os.path.exists(path):
        return jsonify({"file": filename, "lines": []})
    try:
        with open(path, "r", errors="replace") as f:
            content = f.readlines()
    except Exception as err:
        log_event(f"Could not read log file {filename}: {err}", level="error")
        return jsonify({"error": "Could not read log file"}), 500
    return jsonify({"file": filename, "lines": [l.rstrip("\n") for l in content[-lines:]]})


@app.route("/api/logs/clear", methods=["POST"])
@require_token
def clear_logs():
    data = request.get_json(silent=True) or {}
    filename = data.get("file")
    if filename not in ALLOWED_LOG_FILES:
        return jsonify({"error": "Invalid log file"}), 400
    path = os.path.join(LOG_DIR, filename)
    try:
        open(path, "w").close()
    except Exception as err:
        log_event(f"Failed to clear log {filename}: {err}", level="error")
        return jsonify({"error": "Could not clear log"}), 500
    log_event(f"Cleared log file: {filename}")
    return jsonify({"message": "cleared", "file": filename})


# ================================================================
# ROUTES: server controls — predefined actions only, no shell exec
# ================================================================
def _start_web():
    if check_port_open("127.0.0.1", DASHBOARD_PORT):
        return {"service": "web", "action": "start", "status": "already_running"}
    try:
        log_file = open(os.path.join(LOG_DIR, "dashboard-stdout.log"), "a")
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(DASHBOARD_PORT)],
            cwd=SERVER_DIR, stdout=log_file, stderr=subprocess.STDOUT,
        )
        with open(DASHBOARD_PID_FILE, "w") as f:
            f.write(str(proc.pid))
    except Exception as err:
        log_event(f"Failed to start web server: {err}", level="error")
        return {"service": "web", "action": "start", "status": "error"}
    log_event("Web server started via control API")
    return {"service": "web", "action": "start", "status": "started", "pid": proc.pid}


def _stop_web():
    pid = read_pid_file(DASHBOARD_PID_FILE) or find_pid("http.server")
    if not pid:
        return {"service": "web", "action": "stop", "status": "not_running"}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception as err:
        log_event(f"Failed to stop web server: {err}", level="error")
        return {"service": "web", "action": "stop", "status": "error"}
    if os.path.exists(DASHBOARD_PID_FILE):
        os.remove(DASHBOARD_PID_FILE)
    log_event("Web server stopped via control API")
    return {"service": "web", "action": "stop", "status": "stopped"}


def _restart_web():
    _stop_web()
    time.sleep(0.5)
    return _start_web()


def _start_ssh():
    if get_ssh_status()["status"] == "online":
        return {"service": "ssh", "action": "start", "status": "already_running"}
    try:
        subprocess.run(["sshd"], timeout=5)
    except Exception as err:
        log_event(f"Failed to start sshd: {err}", level="error")
        return {"service": "ssh", "action": "start", "status": "error"}
    log_event("SSH started via control API")
    return {"service": "ssh", "action": "start", "status": "started"}


def _stop_ssh():
    try:
        subprocess.run(["pkill", "-f", "sshd"], timeout=5)
    except Exception as err:
        log_event(f"Failed to stop sshd: {err}", level="error")
        return {"service": "ssh", "action": "stop", "status": "error"}
    log_event("SSH stopped via control API")
    return {"service": "ssh", "action": "stop", "status": "stopped"}


def _restart_ssh():
    _stop_ssh()
    time.sleep(0.5)
    return _start_ssh()


def _start_api():
    return {
        "service": "api", "action": "start", "status": "already_running",
        "note": "This API is already running — it just answered this request.",
    }


def _stop_api():
    log_event("API stop requested via control API")

    def do_stop():
        time.sleep(1)
        os._exit(0)

    threading.Thread(target=do_stop, daemon=True).start()
    return {"service": "api", "action": "stop", "status": "stopping"}


def _restart_api():
    log_event("API restart requested via control API")

    def do_restart():
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=do_restart, daemon=True).start()
    return {"service": "api", "action": "restart", "status": "restarting"}


ACTION_TABLE = {
    ("web", "start"): _start_web, ("web", "stop"): _stop_web, ("web", "restart"): _restart_web,
    ("ssh", "start"): _start_ssh, ("ssh", "stop"): _stop_ssh, ("ssh", "restart"): _restart_ssh,
    ("api", "start"): _start_api, ("api", "stop"): _stop_api, ("api", "restart"): _restart_api,
}


def dispatch_control(action, service):
    if service == "all":
        order = ["ssh", "web", "api"]  # api last since "stop" ends this process
        return [ACTION_TABLE[(svc, action)]() for svc in order if (svc, action) in ACTION_TABLE]
    fn = ACTION_TABLE.get((service, action))
    return fn() if fn else None


@app.route("/api/control/<action>", methods=["POST"])
@require_token
def control(action):
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "Unknown action"}), 400
    data = request.get_json(silent=True) or {}
    service = data.get("service")
    if service not in ("web", "api", "ssh", "all"):
        return jsonify({"error": "Unknown service. Use web, api, ssh, or all."}), 400
    try:
        result = dispatch_control(action, service)
    except Exception as err:
        log_event(f"Control action failed ({action}/{service}): {err}", level="error")
        return jsonify({"error": "Action failed"}), 500
    if result is None:
        return jsonify({"error": "Unsupported action for this service"}), 400
    return jsonify({"result": result})


# ================================================================
# MAIN
# ================================================================
if __name__ == "__main__":
    log_event("Server API starting up")
    print("=" * 64)
    print("Mobile Hosted Server API")
    print(f"Listening on 0.0.0.0:{API_PORT}")
    print()
    print("API access token (needed for File Manager & Server Controls):")
    print(API_TOKEN)
    print("Enter this once in the dashboard's Settings action.")
    print("=" * 64)
    app.run(host="0.0.0.0", port=API_PORT, debug=False)
