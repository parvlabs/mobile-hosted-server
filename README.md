# ⚡ Mobile Hosted Server

> **Ultra low-power personal home server & private cloud for Android (Termux) and PC. Features real-time hardware telemetry, built-in battery UPS monitoring, web file manager with folder uploads, zero-config Cloudflare tunnels, and hardened security.**

[![Python](https://img.shields.io/badge/Python-3.8+-3776AB?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.x-000000?style=flat&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Termux](https://img.shields.io/badge/Termux-Supported-10b981?style=flat&logo=android&logoColor=white)](https://termux.dev/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

An ultra-lightweight, self-hosted home server and dashboard designed to run seamlessly on Android devices (via [Termux](https://termux.dev)) or on any desktop/laptop (Windows, Linux, macOS).

---

## 🌟 Why an Android Phone as a Server?

* 🔋 **Built-in Battery UPS**: Immune to sudden power outages — the phone's internal battery keeps the server online 24/7.
* ⚡ **Ultra Low Power**: Consumes only ~2–4W compared to 60–100W+ for traditional desktop or rack servers.
* 📶 **Always Connected**: Built-in Wi-Fi and cellular failover capabilities in a compact, silent footprint.
* ♻️ **Upcycle Old Hardware**: Turn spare smartphones into dependable personal cloud storage and compute servers.

---

## ✨ Features

### 🎨 Claymorphism UI, Themes & Fast Connect
* 🏺 **Pure Claymorphism Design System**: Strictly crafted with soft pastel clay surfaces (`#B8A6FF` Lavender, `#9EC9F0` Sky, `#7DD4A8` Mint, `#FFB59E` Peach, `#F5D77A` Lemon), multi-layer colored inset/outset lighting, chunky 28px card radii, and tactile squish-action buttons.
* 🌓 **Dual Claymorphism Themes (Light & Dark)**: 1-click sidebar switcher between **Clay Light** (`#F7F5FB` pastel clay) and **Midnight Obsidian Clay** (`#13101C` sculpted dark clay with luminous glow), with persistent theme preference.
* 📱 **Left Sidebar Navigation**: Dedicated left sidebar organizing all server functions, with **Cloud Storage & Files** as the clean landing experience, and 1-click switching to Telemetry, Remote Access, Automations, and Live Logs.
* 📷 **1-Tap QR Connect**: Point any phone or tablet camera at the QR code to open the dashboard and authenticate instantly with zero typing.
* 🔢 **Human-Friendly 6-Digit PIN Pairing**: Pair new devices using a simple 6-digit PIN code (e.g. `199 593`) instead of memorizing or copying long 32-character tokens.

### 📊 Real-Time Telemetry & Visuals
* 📈 **60-Second Real-Time Telemetry Graph**: Smooth canvas chart displaying real-time CPU %, RAM %, and network traffic over the last minute.
* 📥 **24-Hour Telemetry History & Export**: Automatic background metrics capture with 1-click export to CSV or JSON for deep performance analysis.
* 💾 **Disk Space Visual Breakdown & Cache Cleaner**: Interactive multi-color storage bar categorizing Media, Documents, Archives, Code, and Logs with a 1-click temporary cache cleaner.
* 🔋 **Battery UPS & Health Guard**: Continuous battery level and temperature monitoring with smart advisory alerts to prevent battery swelling during 24/7 wall power.
* 🛡️ **Android Wake-Lock & Governor Guard**: Real-time CPU governor indicator and 1-click Termux wake-lock toggle to prevent the mobile OS from sleeping or killing server threads.
* 📱 **Instant Wi-Fi QR Connect**: Built-in QR code modal to connect mobile devices or laptops on your local network instantly without typing IP addresses.

### 🌐 Remote Access & Public Hosting
* 🌐 **Zero-Config Cloudflare Tunnel**: 1-click Cloudflare Tunnel (`cloudflared`) to access your server globally over HTTPS without port forwarding or exposing your home IP.
* 🦎 **Tailscale Mesh VPN Integration**: Node indicator showing peer IP and MagicDNS address for encrypted zero-trust private mesh networking across all your devices.
* 🔄 **DuckDNS Dynamic DNS (DDNS) Updater**: Built-in background dynamic DNS auto-updater with configurable sync intervals, domain token support, and manual sync triggers.
* 🌍 **Static Website & Webhook Hosting**: Built-in static website directory (`public_html/`) automatically served at `/site/` for lightweight websites, documentation, or webhook receivers.

### 📁 Cloud Storage, File Manager & Sharing
* 📁 **Web File Manager**: Browse, create folders, download, rename, and delete files safely.
* 📦 **ZIP Archive Upload & Auto-Extraction**: Upload `.zip` archives with an option to automatically unpack and extract directories into the current folder or save as-is.
* 📂 **Folder Upload & 1-Click ZIP Download**: Upload nested folder structures directly from your browser, or download entire directories bundled as `.zip` archives.
* 📝 **In-Browser Text & Code Editor**: View and edit `.txt`, `.py`, `.sh`, `.json`, `.md`, `.html`, `.css`, and `.yaml` files directly from the browser with `Ctrl+S` quick save.
* 🔗 **Shareable Expiring Download Links**: Generate public download links with custom expiration times (1 hour to 7 days), download counters, and optional password protection.
* 🗑️ **Safe Trash Bin**: Accidental deletion protection; files are safely moved to `.trash/` with 1-click restoration and storage reclamation.
* 🎬 **In-line Media Preview**: Stream videos, music, and view photos directly inside the browser.
* 🛡️ **Sandboxed Storage**: Access is strictly isolated to `~/server/shared/`, preventing directory traversal escapes (`../`).

### ⚙️ Automation, Alerts & Security
* ⏰ **Web Scheduled Tasks (Cron Jobs)**: Background supervisor executing recurring maintenance, backup, and health checks with interval controls, enable/disable toggles, and manual execution triggers.
* 🔔 **Discord & Telegram Webhook Alerts**: Automatic push notifications delivered to your Discord channel or Telegram chat on critical hardware triggers (high battery temperature, low battery, low storage).
* ⚡ **Services Grid**: Real-time status monitoring and PID tracking for Web, API, and SSH services.
* 🔄 **Server Controls**: Safe 1-click start, stop, and restart controls for core services.
* 📜 **Live Audit Log Viewer**: Real-time server and access logs with auto-refresh and secure log-clearing.
* 🔒 **Hardened Security**: Timing-safe token authentication (`secrets.compare_digest`), IP rate-limiting with lockout protection, upload extension verification, and HTTP security headers.

---

## 🚀 Quick Start

### 📱 Running on Android (Termux)

1. **Install Termux and Termux:API** from [F-Droid](https://f-droid.org/en/packages/com.termux/).
2. **Open Termux** and install required packages:
   ```bash
   pkg update -y
   pkg install -y python git termux-api
   ```
3. **Clone the repository**:
   ```bash
   git clone https://github.com/parvlabs/mobile-hosted-server.git ~/server
   cd ~/server
   ```
4. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
5. **Start all services**:
   ```bash
   bash start-all.sh
   ```
6. **Open the Dashboard**:
   The terminal will display your local IP. Open your browser on any device on the same Wi-Fi:
   ```text
   http://<PHONE_IP>:8080
   ```

---

### 💻 Running Locally on Windows / macOS / Linux

1. **Clone the repository**:
   ```bash
   git clone https://github.com/parvlabs/mobile-hosted-server.git
   cd mobile-hosted-server
   ```
2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Launch the server**:
   * **Windows (One-Click)**: Double-click `start-local.bat`
   * **Any OS (Terminal)**:
     ```bash
     python run_local.py
     ```
   The runner will start both services, output your access token, and automatically open the dashboard in your default browser.

---

## 📂 Project Structure

```text
mobile-hosted-server/
├── api.py              # Flask backend REST API (telemetry, storage, services, logs, cron, DDNS)
├── index.html          # Modern frontend dashboard (telemetry chart, file manager, editor, modals)
├── public_html/        # Hosted static website served at /site/ (starter template included)
├── run_local.py        # Cross-platform runner for Windows / macOS / Linux
├── start-local.bat     # One-click Windows batch launcher
├── start-all.sh        # Background launcher for Termux / Linux
├── start-api.sh        # Foreground API debug launcher
├── start-dashboard.sh  # Foreground HTTP server debug launcher
├── start-server.sh     # Boot script for Termux:Boot app
├── requirements.txt    # Python dependencies
├── .gitignore          # Security rules (ignores tokens, logs, uploaded files)
└── README.md           # Documentation
```

---

## 🔒 Security Model

* **Sandboxed Storage**: File Manager access is strictly confined to `~/server/shared/`. Path traversal (`../`, absolute paths, symlinks) is blocked via sanitized path resolution.
* **Token Authentication**: Modifying files, server controls, and logs require the `Authorization: Bearer <token>` header. A cryptographically random token is generated on initial run in `.token`.
* **Private Network First**: CORS is constrained to private network subnets (10.x, 172.16-31.x, 192.168.x, localhost).
* **Safe Error Handling**: Unhandled errors return sanitized JSON responses without leaking Python tracebacks.

---

## 📜 License

This project is licensed under the [MIT License](LICENSE).
