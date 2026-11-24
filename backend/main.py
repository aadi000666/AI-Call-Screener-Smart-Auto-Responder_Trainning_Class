"""
AI Call Screener & Smart Auto-Responder - FastAPI Main Server
Coordinates ADB call monitoring, AI classification, voice screening,
WebSocket events, and REST APIs.
"""

from __future__ import annotations
import os
import yaml
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# Load environment variables from .env file (with override=True)
load_dotenv(override=True)

# Sync Gemini API key to GOOGLE_API_KEY for google-genai SDK
if os.environ.get("GEMINI_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.database import db, CallLog, VipContact, ScheduleRule, SystemState
from backend.adb_controller import ADBController, TelephonyState
from backend.context_manager import ContextManager, UserStatus
from backend.ai_engine import AIEngine
from backend.voice_agent import VoicePipeline, VoiceScreeningSession

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("CallScreener.Server")

# ============================================================
# Configuration Loading
# ============================================================
CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")

def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"Error loading {CONFIG_PATH}: {e}")
    return {}

config = load_config()

# ============================================================
# Core Services Initialization
# ============================================================
adb_cfg = config.get("adb", {})
adb = ADBController(
    adb_path=adb_cfg.get("adb_path", "adb"),
    default_serial=adb_cfg.get("device_serial", ""),
    simulation_mode=adb_cfg.get("simulation_mode", False)
)

context_manager = ContextManager(
    user_name=config.get("app", {}).get("user_name", "Aditya"),
    default_mode=config.get("app", {}).get("active_mode", "smart_reject")
)

ai_engine = AIEngine(config)

voice_cfg = config.get("voice", {})
voice_pipeline = VoicePipeline(
    voice=voice_cfg.get("tts_voice", "en-IN-NeerjaNeural"),
    rate=voice_cfg.get("tts_rate", "+0%"),
    pitch=voice_cfg.get("tts_pitch", "+0Hz"),
    output_dir=voice_cfg.get("output_audio_dir", "data/audio_logs")
)


# ============================================================
# WebSocket Connection Hub
# ============================================================
class ConnectionManager:
    """Maintains active WebSocket browser clients for real-time live events."""
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket client connected. Total clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"WebSocket client disconnected. Total clients: {len(self.active_connections)}")

    async def broadcast(self, event: str, data: Any):
        payload = {"event": event, "data": data}
        to_remove = []
        for connection in self.active_connections:
            try:
                await connection.send_json(payload)
            except Exception:
                to_remove.append(connection)
        for conn in to_remove:
            self.disconnect(conn)

ws_manager = ConnectionManager()


# ============================================================
# Active Call State Tracker & Worker
# ============================================================
class ActiveCallState:
    def __init__(self):
        self.current_call_id: Optional[str] = None
        self.current_number: str = ""
        self.current_name: str = ""
        self.state: str = TelephonyState.IDLE
        self.screening_session: Optional[VoiceScreeningSession] = None
        self.lock = asyncio.Lock()

active_call = ActiveCallState()


async def handle_incoming_call(number: str):
    """
    Core business logic when a new incoming call is detected.
    Evaluates context, triggers Mode action (Reject+SMS, Voice Screen, VIP Bypass).
    """
    async with active_call.lock:
        if active_call.state == TelephonyState.RINGING and active_call.current_number == number:
            return # Already being processed

        active_call.current_number = number
        active_call.state = TelephonyState.RINGING

        # Resolve contact & evaluate policy
        rec_action, contact, user_ctx = context_manager.evaluate_incoming_caller(number)
        caller_name = contact.name if contact else "Unknown Caller"
        category = contact.category if contact else "Unknown"
        active_call.current_name = caller_name

        logger.info(f"Processing Incoming Call from '{caller_name}' ({number}) | Category: {category} | Action: {rec_action}")

        # Create initial CallLog
        call_log = CallLog(
            caller_number=number,
            caller_name=caller_name,
            category=category,
            mode_used=context_manager.active_mode,
            action_taken=rec_action,
            user_context=user_ctx,
            status="RINGING"
        )
        active_call.current_call_id = db.add_call(call_log)

        # Broadcast live ringing event to frontend
        await ws_manager.broadcast("INCOMING_CALL", {
            "call_id": active_call.current_call_id,
            "caller_number": number,
            "caller_name": caller_name,
            "category": category,
            "user_context": user_ctx,
            "recommended_action": rec_action,
            "active_mode": context_manager.active_mode
        })

    # Execute action asynchronously outside lock
    call_id = active_call.current_call_id
    if rec_action == "BLOCK_SPAM":
        # Instantly reject
        adb.reject_call()
        db.update_call(call_id, {
            "status": "REJECTED",
            "action_taken": "BLOCK_SPAM",
            "urgency_score": 1,
            "urgency_label": "LOW",
            "ai_summary": "Robocall / Telemarketer dropped immediately."
        })
        await ws_manager.broadcast("CALL_ACTION", {
            "call_id": call_id,
            "action": "BLOCK_SPAM",
            "status": "BLOCKED",
            "message": "Call blocked automatically."
        })

    elif rec_action == "VIP_BYPASS":
        # Ring through without interrupting
        db.update_call(call_id, {
            "status": "BYPASSED",
            "action_taken": "VIP_BYPASS",
            "urgency_score": 9,
            "urgency_label": "HIGH",
            "ai_summary": f"VIP Contact '{caller_name}' permitted to ring through."
        })
        await ws_manager.broadcast("CALL_ACTION", {
            "call_id": call_id,
            "action": "VIP_BYPASS",
            "status": "BYPASSED",
            "message": f"VIP Call from {caller_name} allowed to ring."
        })

    elif rec_action == "VOICE_SCREEN":
        # Answer call & initiate interactive voice screening
        logger.info(f"Answering call for AI Voice Screening: {caller_name}")
        answered = adb.answer_call()
        adb.toggle_speakerphone(True)

        db.update_call(call_id, {"status": "ANSWERED", "action_taken": "VOICE_SCREEN"})
        await ws_manager.broadcast("CALL_ACTION", {
            "call_id": call_id,
            "action": "VOICE_SCREEN",
            "status": "ANSWERED",
            "message": "Call answered. Starting AI voice agent..."
        })

        # Create screening session
        session = VoiceScreeningSession(
            call_id=call_id,
            caller_number=number,
            caller_name=caller_name,
            category=category,
            user_context=user_ctx,
            ai_engine=ai_engine,
            voice_pipeline=voice_pipeline,
            on_event=lambda ev, dat: ws_manager.broadcast("VOICE_EVENT", {"event": ev, **dat}),
            on_audio_ready=lambda path: adb.play_audio_on_phone(path)
        )
        active_call.screening_session = session
        greeting_text = await session.start_session()
        logger.info(f"Screening session started. Greeting: '{greeting_text}'")

    else: # Default: REJECT_AND_SMS
        # Reject call via ADB
        logger.info(f"Rejecting call and generating smart reply for: {caller_name}")
        adb.reject_call()

        # Classify & generate context-aware SMS
        ai_res = await ai_engine.classify_and_generate_reply(
            caller_number=number,
            caller_name=caller_name,
            category=category,
            user_context=user_ctx
        )

        # Ensure number has +91 prefix for Samsung SMS routing
        sms_number = number
        import re as _re
        clean_for_sms = _re.sub(r"[^\d]", "", number)
        if clean_for_sms and len(clean_for_sms) == 10:
            sms_number = f"+91{clean_for_sms}"
        elif clean_for_sms and len(clean_for_sms) == 12 and clean_for_sms.startswith("91"):
            sms_number = f"+{clean_for_sms}"
        logger.info(f"SMS target: raw='{number}' → formatted='{sms_number}' | msg='{ai_res.reply_message[:60]}'")

        # Dispatch SMS via ADB
        sms_sent, sms_detail = adb.send_sms(sms_number, ai_res.reply_message)

        # Update database
        db.update_call(call_id, {
            "status": "REJECTED",
            "action_taken": "REJECT_AND_SMS",
            "urgency_score": ai_res.urgency_score,
            "urgency_label": ai_res.urgency_label,
            "sms_sent": sms_sent,
            "sms_text": ai_res.reply_message,
            "ai_summary": f"Auto-rejected while {user_ctx.get('status')}. SMS sent: '{ai_res.reply_message}'"
        })

        # Broadcast completed action
        await ws_manager.broadcast("CALL_ACTION", {
            "call_id": call_id,
            "action": "REJECT_AND_SMS",
            "status": "COMPLETED",
            "urgency_score": ai_res.urgency_score,
            "urgency_label": ai_res.urgency_label,
            "sms_sent": sms_sent,
            "sms_text": ai_res.reply_message,
            "reasoning": ai_res.reasoning
        })
async def call_listener_loop():
    """
    Background worker loop that continuously queries telephony state via ADB.
    Detects incoming calls with debounce and handles call wrap-up.
    """
    poll_interval = float(config.get("adb", {}).get("poll_interval", 1.0))
    logger.info(f"Background Call Listener Worker started (polling every {poll_interval}s)")

    last_seen_state = TelephonyState.IDLE

    while True:
        try:
            state, number = adb.get_telephony_state()

            # State transition: IDLE -> RINGING
            if state == TelephonyState.RINGING and last_seen_state != TelephonyState.RINGING:
                if not number:
                    await asyncio.sleep(0.35)
                    _, retry_number = adb.get_telephony_state()
                    if retry_number:
                        number = retry_number
                clean_num = number if number else "Unknown Number"
                asyncio.create_task(handle_incoming_call(clean_num))

            # State transition: RINGING/OFFHOOK -> IDLE
            elif state == TelephonyState.IDLE and last_seen_state in [TelephonyState.RINGING, TelephonyState.OFFHOOK]:
                if active_call.current_call_id:
                    logger.info(f"Call {active_call.current_call_id} ended.")
                    if active_call.screening_session and active_call.screening_session.is_active:
                        await active_call.screening_session.end_session()

                    await ws_manager.broadcast("CALL_ENDED", {
                        "call_id": active_call.current_call_id,
                        # pyrefly: ignore[unknown-name]
                        "timestamp": datetime.now().isoformat()
                    })

                async with active_call.lock:
                    active_call.state = TelephonyState.IDLE
                    active_call.current_number = ""
                    active_call.current_name = ""
                    active_call.current_call_id = None
                    active_call.screening_session = None

            last_seen_state = state

        except Exception as e:
            logger.error(f"Error in call_listener_loop: {e}")

        await asyncio.sleep(poll_interval)


# ============================================================
# FastAPI Lifespan (Startup & Shutdown)
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Launch background listener worker
    listener_task = asyncio.create_task(call_listener_loop())
    logger.info("AI Call Screener Server started.")
    yield
    # Shutdown
    listener_task.cancel()
    logger.info("AI Call Screener Server shut down.")


app = FastAPI(
    title="AI Call Screener & Smart Auto-Responder",
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount audio folder for streaming
os.makedirs("data/audio_logs", exist_ok=True)
app.mount("/audio", StaticFiles(directory="data/audio_logs"), name="audio")


# ============================================================
# WebSocket Endpoint
# ============================================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        # Send initial state snapshot
        ctx = context_manager.get_current_context()
        devices = adb.get_connected_devices()
        await websocket.send_json({
            "event": "INITIAL_STATE",
            "data": {
                "context": ctx,
                "mode": context_manager.active_mode,
                "adb_connected": adb.is_device_ready(),
                "devices": devices,
                "simulation_mode": adb.simulation_mode,
                "active_call": {
                    "is_active": active_call.state != TelephonyState.IDLE,
                    "number": active_call.current_number,
                    "name": active_call.current_name,
                    "state": active_call.state
                }
            }
        })
        while True:
            # Keep connection open & handle incoming client messages
            msg = await websocket.receive_text()
            # Can support ping/pong or client action triggers
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.debug(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)


# ============================================================
# Request / Response Schemas
# ============================================================
class ContextUpdateRequest(BaseModel):
    status: str = Field(..., examples=["IN_CLASS"])
    note: str = Field(default="", examples=["Operating Systems Lecture"])
    until: str = Field(default="", examples=["16:00"])

class ModeUpdateRequest(BaseModel):
    mode: str = Field(..., examples=["smart_reject"])

class SimulateCallRequest(BaseModel):
    caller_number: str = Field(default="+919876543210")
    caller_name: str = Field(default="Mom")
    category: str = Field(default="Family")

class SimulateTurnRequest(BaseModel):
    caller_speech: str = Field(..., examples=["Hi, this is Mom calling to ask what time you'll be home for dinner."])

class WirelessConnectRequest(BaseModel):
    ip: str = Field(..., examples=["192.168.1.100"])
    port: int = Field(default=5555)

class VoiceTestRequest(BaseModel):
    text: str = Field(default="Hello, this is Aditya's AI Call Screener testing natural neural speech synthesis.")
    voice: Optional[str] = None


# ============================================================
# REST Endpoints
# ============================================================

# ----------------- Status & Context -----------------
@app.get("/api/status")
async def get_status():
    ctx = context_manager.get_current_context()
    devices = adb.get_connected_devices()
    return {
        "user_name": context_manager.user_name,
        "context": ctx,
        "active_mode": context_manager.active_mode,
        "adb": {
            "connected": adb.is_device_ready(),
            "simulation_mode": adb.simulation_mode,
            "devices": devices
        },
        "active_call": {
            "state": active_call.state,
            "caller_number": active_call.current_number,
            "caller_name": active_call.current_name,
            "call_id": active_call.current_call_id
        }
    }

@app.post("/api/context")
async def update_context(req: ContextUpdateRequest):
    context_manager.set_manual_status(req.status, req.note, req.until)
    ctx = context_manager.get_current_context()
    await ws_manager.broadcast("CONTEXT_UPDATED", ctx)
    return {"status": "success", "context": ctx}

@app.post("/api/context/clear")
async def clear_context():
    context_manager.clear_manual_override()
    ctx = context_manager.get_current_context()
    await ws_manager.broadcast("CONTEXT_UPDATED", ctx)
    return {"status": "success", "context": ctx}

@app.post("/api/mode")
async def update_mode(req: ModeUpdateRequest):
    context_manager.set_mode(req.mode)
    await ws_manager.broadcast("MODE_UPDATED", {"mode": context_manager.active_mode})
    return {"status": "success", "active_mode": context_manager.active_mode}


# ----------------- ADB Controls -----------------
@app.get("/api/adb/devices")
async def get_adb_devices():
    return {
        "devices": adb.get_connected_devices(),
        "is_ready": adb.is_device_ready(),
        "simulation_mode": adb.simulation_mode
    }

@app.post("/api/adb/connect")
async def connect_wireless_adb(req: WirelessConnectRequest):
    ok, msg = adb.connect_wireless(req.ip, req.port)
    devices = adb.get_connected_devices()
    await ws_manager.broadcast("ADB_UPDATED", {"connected": ok, "devices": devices})
    return {"success": ok, "message": msg, "devices": devices}

@app.post("/api/adb/simulation")
async def toggle_adb_simulation(enable: bool = Query(...)):
    adb.simulation_mode = enable
    devices = adb.get_connected_devices()
    await ws_manager.broadcast("ADB_UPDATED", {"simulation_mode": enable, "devices": devices})
    return {"status": "success", "simulation_mode": enable}


# ----------------- Call Logs & Actions -----------------
@app.get("/api/calls")
async def get_calls(limit: int = 50, urgency: Optional[str] = None):
    calls = db.get_all_calls(limit=limit)
    if urgency:
        calls = [c for c in calls if c.urgency_label.upper() == urgency.upper()]
    return calls

@app.get("/api/calls/{call_id}")
async def get_call(call_id: str):
    c = db.get_call_by_id(call_id)
    if not c:
        raise HTTPException(status_code=404, detail="Call record not found")
    return c

@app.delete("/api/calls/{call_id}")
async def delete_call(call_id: str):
    ok = db.delete_call(call_id)
    return {"status": "success", "deleted": ok}

@app.delete("/api/calls")
async def clear_all_calls():
    db.clear_all_calls()
    return {"status": "success", "message": "All call logs cleared"}


# ----------------- Call Simulation (Interactive Testing) -----------------
@app.post("/api/calls/simulate")
async def simulate_incoming_call(req: SimulateCallRequest):
    """Triggers a simulated incoming call to test the entire screener pipeline!"""
    # Look up contact or add temporary contact
    contact = db.get_contact_by_number(req.caller_number)
    if not contact:
        # Create temporary VIP/contact
        contact = VipContact(
            phone_number=req.caller_number,
            name=req.caller_name,
            category=req.category,
            priority="Normal" if req.category != "Family" else "VIP"
        )
        db.add_contact(contact)

    adb.simulate_incoming_call(req.caller_number, req.caller_name)
    asyncio.create_task(handle_incoming_call(req.caller_number))
    return {"status": "success", "message": f"Simulated call initiated from {req.caller_name} ({req.caller_number})"}

@app.post("/api/calls/simulate-turn")
async def simulate_caller_turn(req: SimulateTurnRequest):
    """Sends caller's speech to the currently active screening session."""
    session = active_call.screening_session
    if not session or not session.is_active:
        raise HTTPException(status_code=400, detail="No active voice screening session")

    turn_res = await session.process_caller_response(req.caller_speech)
    return turn_res

@app.post("/api/calls/end-active")
async def end_active_call():
    """Manually hangs up active call."""
    adb.reject_call()
    if active_call.screening_session and active_call.screening_session.is_active:
        await active_call.screening_session.end_session()
    active_call.state = TelephonyState.IDLE
    await ws_manager.broadcast("CALL_ENDED", {"call_id": active_call.current_call_id})
    return {"status": "success", "message": "Call terminated."}


# ----------------- VIP Contacts CRUD -----------------
@app.get("/api/contacts")
async def get_contacts():
    return db.get_all_contacts()

@app.post("/api/contacts")
async def create_or_update_contact(contact: VipContact):
    contact_id = db.add_contact(contact)
    return {"status": "success", "contact_id": contact_id}

@app.delete("/api/contacts/{contact_id}")
async def delete_contact(contact_id: str):
    ok = db.delete_contact(contact_id)
    return {"status": "success", "deleted": ok}


# ----------------- Timetable Schedules CRUD -----------------
@app.get("/api/schedules")
async def get_schedules():
    return db.get_all_schedules()

@app.post("/api/schedules")
async def create_schedule(schedule: ScheduleRule):
    rule_id = db.add_schedule(schedule)
    return {"status": "success", "schedule_id": rule_id}

@app.put("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, updates: Dict[str, Any]):
    ok = db.update_schedule(schedule_id, updates)
    return {"status": "success", "updated": ok}

@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    ok = db.delete_schedule(schedule_id)
    return {"status": "success", "deleted": ok}


# ----------------- Voice & TTS Preview -----------------
@app.post("/api/voice/test")
async def test_voice_synthesis(req: VoiceTestRequest):
    test_pipeline = voice_pipeline
    if req.voice:
        test_pipeline = VoicePipeline(voice=req.voice)
    audio_path = await test_pipeline.synthesize(req.text, filename_prefix="preview")
    filename = os.path.basename(audio_path)
    return {
        "status": "success",
        "audio_url": f"/audio/{filename}",
        "voice": req.voice or voice_pipeline.voice
    }


# ============================================================
# Frontend Dashboard Serving
# ============================================================
os.makedirs("frontend", exist_ok=True)

@app.get("/")
async def serve_index():
    index_file = os.path.join("frontend", "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return JSONResponse({"message": "AI Call Screener API is running. Place index.html in frontend/"})

# Mount frontend static directory if needed
app.mount("/static", StaticFiles(directory="frontend"), name="static")

if __name__ == "__main__":
    import uvicorn
    host = config.get("app", {}).get("host", "0.0.0.0")
    port = int(config.get("app", {}).get("port", 8000))
    uvicorn.run("backend.main:app", host=host, port=port, reload=False)
