# AI Call Screener & Smart Auto-Responder 🎙️⚡

An advanced, modular, production-grade personal call management system for Android via ADB (over USB or Wi-Fi). It monitors incoming calls in real time, checks the user's live context (Busy / In Class / Coding Focus / Sleeping / DND / Timetable Schedule), classifies caller priority and intent using an LLM (Local Ollama or Gemini API), and autonomously takes action across three intelligent modes:

1. **Mode 1 (Smart Reject & Contextual Auto-Reply):** Rejects the call automatically and dispatches a personalized, context-aware SMS/WhatsApp reply tailored to who is calling and why the user is occupied.
2. **Mode 2 (Interactive AI Voice Screening):** Answers the call on speakerphone/virtual audio, greets the caller using natural neural speech synthesis (`edge-tts`), transcribes their response via STT (`SpeechRecognition` / Whisper), extracts intent and urgency via LLM, and logs an executive structured summary.
3. **Mode 3 (VIP Bypass):** Lets high-priority contacts (Family, Academic Advisors, Emergency) ring through directly without blocking.

---

## 🏗️ Architecture & Technology Stack

```
                              ┌───────────────────────────────────┐
                              │     Android Device (USB / Wi-Fi)  │
                              │  - dumpsys telephony.registry     │
                              │  - telecom / input keyevents      │
                              │  - SMS Intent / ISMS service      │
                              └─────────────────▲─────────────────┘
                                                │
                                    ADB Subprocess Engine
                                                │
┌────────────────────────┐            ┌─────────▼─────────┐            ┌────────────────────────┐
│   Web Dashboard (SPA)  │◄──WebSocket│   FastAPI Server  │───────────►│    Database Layer      │
│  - Live Status Pills   │───REST API─►│  (Background     │            │  - MongoDB (Primary)   │
│  - Call History & Logs │            │   Listener Loop)  │            │  - Local JSON Fallback │
│  - Test Call Simulator │            └─────────┬─────────┘            └────────────────────────┘
└────────────────────────┘                      │
                                                ▼
                        ┌───────────────────────────────────────────────┐
                        │              AI Decision Engine               │
                        │  - Local Ollama (Llama 3.2 / Qwen 2.5)        │
                        │  - Google Gemini API Fallback                 │
                        │  - Deterministic Contextual Rule Engine       │
                        └───────────────────────┬───────────────────────┘
                                                │
                                                ▼
                        ┌───────────────────────────────────────────────┐
                        │             Voice Agent Pipeline              │
                        │  - Edge-TTS (Indian-English & Hindi Neural)   │
                        │  - SpeechRecognition / Faster-Whisper STT     │
                        │  - Multi-Turn Interactive Screening Session   │
                        └───────────────────────────────────────────────┘
```

- **Backend:** Python 3.10+, FastAPI, WebSockets, Pydantic v2, PyYAML, asyncio.
- **Android Device Bridge:** Android Debug Bridge (`adb`) subprocess controller with telephony registry parser, automated keyevents, and multi-strategy SMS dispatch.
- **AI & NLP:** Modular LLM client supporting local offline **Ollama** (`llama3.2`, `qwen2.5:3b`) and **Gemini API**, with strict JSON schemas and fallback template engines.
- **Speech Pipeline:** `edge-tts` (featuring natural voices such as `en-IN-NeerjaNeural`, `en-IN-PrabhatNeural`, and `hi-IN-SwaraNeural`) + STT with `SpeechRecognition`.
- **Database:** MongoDB with automatic persistent JSON fallback (`data/screener_db.json`) if MongoDB daemon is not running.
- **Frontend Dashboard:** Modern dark cyber-themed dashboard with Tailwind CSS, Lucide icons, real-time WebSocket live updates, and an interactive **Test Call Simulator**.

---

## 📂 Project Structure

```
├── backend/
│   ├── main.py              # FastAPI server, WebSocket hub & background call listener worker
│   ├── adb_controller.py    # ADB device manager, call state parser, reject/answer & SMS dispatcher
│   ├── ai_engine.py         # LLM client (Ollama/Gemini), prompt templates, JSON schema parser & fallback
│   ├── voice_agent.py       # Edge-TTS synthesis, SpeechRecognition STT & interactive screening session
│   ├── context_manager.py   # Timetable schedules, live context status manager & VIP evaluator
│   └── database.py          # MongoDB / JSON dual-mode database engine & Pydantic models
├── frontend/
│   └── index.html           # Modern single-page web dashboard
├── data/
│   ├── audio_logs/          # Synthesized neural voice clips and call recordings
│   └── screener_db.json     # Persistent database storage
├── config.yaml              # System configuration, API keys, models, and default templates
├── requirements.txt         # Project dependencies
└── README.md                # Setup guide and documentation
```

---

## 🚀 Step-by-Step Setup Guide

### 1. Prerequisites
- Python 3.10 or higher
- Android Device (Android 9+) OR run in built-in **Virtual Simulator Mode**
- Optional: [Ollama](https://ollama.com/) for local offline privacy

### 2. Install Python Dependencies
```bash
pip install -r requirements.txt
```

### 3. Android Device Setup (USB & Wireless ADB)

#### A. Enable Developer Options & USB Debugging
1. On your Android phone, go to **Settings > About Phone**.
2. Tap **Build Number** 7 times until Developer Mode is unlocked.
3. Go to **Settings > System > Developer Options**.
4. Enable **USB Debugging** and **Disable ADB authorization timeout**.
5. Enable **Verify apps over USB** or **USB debugging (Security settings)** if on Xiaomi/MIUI to permit input and SMS.

#### B. Connect via USB
Plug your phone into your PC with a USB cable. Run:
```bash
adb devices
```
Accept the RSA fingerprint prompt on your phone screen ("Always allow from this computer").

#### C. Enable Wireless ADB (Untethered)
1. While plugged into USB on the same Wi-Fi network, run:
   ```bash
   adb tcpip 5555
   ```
2. Unplug the USB cable.
3. Find your phone's local IP address (e.g., `192.168.1.100` under **Settings > About Phone > Status**).
4. Connect wirelessly:
   ```bash
   adb connect 192.168.1.100:5555
   ```
5. You can now control calls completely untethered!

---

## ⚙️ Configuration (`config.yaml`)

Edit `config.yaml` to configure your preferences:

```yaml
app:
  user_name: "Aditya"
  active_mode: "smart_reject" # smart_reject | voice_screen | vip_bypass

adb:
  enabled: true
  adb_path: "adb" # Auto-detected from PATH or Android SDK
  connect_wireless: false
  device_ip: "192.168.1.100"
  device_port: 5555
  poll_interval: 1.0
  simulation_mode: false # Set true to test without physical phone

llm:
  provider: "ollama" # 'ollama' or 'gemini'
  ollama:
    base_url: "http://localhost:11434"
    model: "llama3.2" # or "qwen2.5:3b"
  gemini:
    api_key: "YOUR_GEMINI_API_KEY" # Or set GEMINI_API_KEY env var
    model: "gemini-2.5-flash"
  fallback_to_template: true

voice:
  tts_voice: "en-IN-NeerjaNeural" # en-IN-PrabhatNeural, hi-IN-SwaraNeural, en-US-JennyNeural
  language: "en-IN"
  max_turns: 3
```

---

## 🖥️ Running the Application

Start the backend server:

```bash
python -m backend.main
```

Or using uvicorn directly:
```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

Open your browser and navigate to:
```
http://localhost:8000
```

---

## 🎮 Interactive Features Walkthrough

### 1. Live Context Switching
Click any status button on the dashboard to toggle your live context:
- 🟢 **Available:** Direct calls ring through.
- 🎓 **In Class:** Prompt allows entering course name & end time (e.g. *Operating Systems until 16:30*).
- 💻 **Coding Focus:** Auto-replies that you are in a deep focus sprint.
- 🌙 **Sleeping:** Enforces DND; auto-rejects and alerts callers to reply 'EMERGENCY' to bypass.
- ⛔ **DND:** Strict Do Not Disturb.

### 2. Testing with Built-in Call Simulator
You don't need an active phone call to test the system!
1. Click the **Test Call** button in the top navigation.
2. Select a caller preset:
   - **Mom (Family VIP)**: Tests VIP Bypass or high-priority routing.
   - **Dr. R. Sharma (Professor)**: Tests academic auto-reply and urgent task logging.
   - **Amazon Delivery**: Tests parcel drop-off instructions.
   - **Spam Robocall**: Tests immediate call blocking.
3. Click **Trigger Incoming Call** and watch the live banner react in real time!

### 3. Mode 2: Interactive Voice Screening Demo
1. Switch the Mode in the top header to **AI Voice Screening 🎙️**.
2. Trigger a test call (e.g., from *Amazon Delivery* or *Dr. Sharma*).
3. The system automatically answers the call, turns on the speakerphone, and speaks an introduction via `edge-tts`.
4. In the live screening box on the dashboard, type a simulated caller reply (e.g. *"I am outside with your parcel, please give the OTP"*).
5. Watch the AI respond conversationally, log the turns, and generate an executive call summary!

---

## 🔒 Privacy & Local Execution
- **Offline Privacy:** When using Ollama with models like `llama3.2` or `qwen2.5`, all call classifications and transcripts remain 100% on your local machine.
- **Fail-Safe Operation:** If LLMs or network connectivity are unavailable, the system automatically falls back to deterministic context rules so no call is ever dropped or left hanging.
