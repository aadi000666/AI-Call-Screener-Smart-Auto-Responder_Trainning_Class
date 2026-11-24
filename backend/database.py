"""
AI Call Screener & Smart Auto-Responder - Database Layer
Supports MongoDB with automatic graceful fallback to persistent local storage.
"""

from __future__ import annotations
import os
import json
import uuid
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("CallScreener.Database")

# ============================================================
# Pydantic Schemas & Models
# ============================================================

class CallLog(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    caller_number: str
    caller_name: str = "Unknown"
    category: str = "Unknown" # Family, Professor, Colleague, Emergency, Delivery, Spam, Unknown
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    duration_seconds: int = 0
    mode_used: str = "smart_reject" # smart_reject | voice_screen | vip_bypass
    action_taken: str = "REJECT_AND_SMS" # REJECT_AND_SMS | VOICE_SCREEN | VIP_BYPASS | BLOCK_SPAM | NO_ACTION
    user_context: Dict[str, Any] = Field(default_factory=dict)
    urgency_score: int = 1 # 1 - 10
    urgency_label: str = "LOW" # LOW | MEDIUM | HIGH | CRITICAL
    sms_sent: bool = False
    sms_text: Optional[str] = None
    transcript: List[Dict[str, Any]] = Field(default_factory=list)
    ai_summary: Optional[str] = None
    audio_file: Optional[str] = None
    status: str = "COMPLETED" # RINGING | REJECTED | ANSWERED | SCREENED | BYPASSED | FAILED

class VipContact(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    phone_number: str
    name: str
    category: str = "Family" # Family, Professor, Colleague, Emergency, Delivery, Spam
    priority: str = "VIP" # VIP, Normal, Block
    bypass_enabled: bool = True
    custom_sms_template: Optional[str] = None
    notes: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class ScheduleRule(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = "Class / Study"
    day_of_week: int = -1 # -1 = All Days, 0 = Monday ... 6 = Sunday
    start_time: str = "09:00" # HH:MM (24-hr)
    end_time: str = "12:00"
    status: str = "IN_CLASS" # IN_CLASS, CODING_FOCUS, SLEEPING, BUSY, DND
    note: str = "Lecture / Deep Focus"
    is_active: bool = True

class SystemState(BaseModel):
    active_mode: str = "smart_reject" # smart_reject | voice_screen | vip_bypass
    current_status: str = "AVAILABLE" # AVAILABLE | BUSY | IN_CLASS | CODING_FOCUS | SLEEPING | DND | CUSTOM
    custom_note: str = ""
    until_time: str = ""
    override_active: bool = False
    adb_connected: bool = False
    adb_device: str = "None"
    last_updated: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ============================================================
# Database Engine Implementation
# ============================================================

class DatabaseManager:
    """
    Unified Database abstraction that connects to MongoDB if available,
    or falls back to local JSON storage if MongoDB daemon is inactive.
    """

    def __init__(self, uri: str = "mongodb://localhost:27017", db_name: str = "ai_call_screener", json_fallback: str = "data/screener_db.json"):
        self.uri = os.environ.get("MONGODB_URI") or uri
        self.db_name = os.environ.get("DATABASE_NAME") or db_name
        self.json_fallback = json_fallback
        self.is_mongo = False
        self.client = None
        self.db = None
        self.local_cache: Dict[str, List[Dict[str, Any]]] = {
            "calls": [],
            "contacts": [],
            "schedules": [],
            "settings": []
        }
        self._initialize()

    def _initialize(self):
        os.makedirs(os.path.dirname(self.json_fallback) or ".", exist_ok=True)
        # Attempt MongoDB connection with 1.5s timeout
        try:
            from pymongo import MongoClient
            client = MongoClient(self.uri, serverSelectionTimeoutMS=1500)
            client.server_info() # Will trigger exception if not reachable
            self.client = client
            self.db = client[self.db_name]
            self.is_mongo = True
            logger.info(f"Connected to MongoDB database '{self.db_name}' at {self.uri}")
        except Exception as e:
            self.is_mongo = False
            logger.warning(f"MongoDB not reachable ({e}). Using local JSON storage ({self.json_fallback})")
            self._load_local_json()

        # Seed initial sample data if empty
        self._seed_default_data_if_empty()

    def _load_local_json(self):
        if os.path.exists(self.json_fallback):
            try:
                with open(self.json_fallback, "r", encoding="utf-8") as f:
                    self.local_cache = json.load(f)
                    for k in ["calls", "contacts", "schedules", "settings"]:
                        if k not in self.local_cache:
                            self.local_cache[k] = []
            except Exception as e:
                logger.error(f"Error reading JSON db: {e}")
        else:
            self._save_local_json()

    def _save_local_json(self):
        try:
            with open(self.json_fallback, "w", encoding="utf-8") as f:
                json.dump(self.local_cache, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving to JSON db: {e}")

    def _seed_default_data_if_empty(self):
        contacts = self.get_all_contacts()
        if not contacts:
            defaults = [
                VipContact(
                    name="Mom",
                    phone_number="+919876543210",
                    category="Family",
                    priority="VIP",
                    bypass_enabled=True,
                    notes="Always bypass DND & screeners for Mom."
                ),
                VipContact(
                    name="Dr. R. Sharma (HOD / Professor)",
                    phone_number="+919811223344",
                    category="Professor",
                    priority="VIP",
                    bypass_enabled=False,
                    custom_sms_template="Respected Dr. Sharma, Aditya is currently in an academic/coding block. He has received your notification and will call back promptly.",
                    notes="Academic advisor - screen or notify immediately."
                ),
                VipContact(
                    name="Amazon Delivery Partner",
                    phone_number="+919700112233",
                    category="Delivery",
                    priority="Normal",
                    bypass_enabled=False,
                    custom_sms_template="Hello! Please leave the package with security / at the front door. Thank you!",
                    notes="Package delivery partner."
                ),
                VipContact(
                    name="Credit Card Spam",
                    phone_number="+911409998877",
                    category="Spam",
                    priority="Block",
                    bypass_enabled=False,
                    notes="Telemarketing nuisance."
                )
            ]
            for c in defaults:
                self.add_contact(c)

        schedules = self.get_all_schedules()
        if not schedules:
            default_schedules = [
                ScheduleRule(
                    title="Morning Lectures & Classes",
                    day_of_week=-1,
                    start_time="09:00",
                    end_time="13:00",
                    status="IN_CLASS",
                    note="University Lectures - Auto Reject & SMS"
                ),
                ScheduleRule(
                    title="Deep Coding Focus Sprint",
                    day_of_week=-1,
                    start_time="14:30",
                    end_time="18:00",
                    status="CODING_FOCUS",
                    note="AI & Systems Project Sprint"
                ),
                ScheduleRule(
                    title="Night Sleep & Rest (DND)",
                    day_of_week=-1,
                    start_time="23:30",
                    end_time="07:30",
                    status="SLEEPING",
                    note="Sleep hours - VIP bypass only"
                )
            ]
            for s in default_schedules:
                self.add_schedule(s)

        # Seed sample call history if empty
        calls = self.get_all_calls()
        if not calls:
            sample_calls = [
                CallLog(
                    caller_number="+919876543210",
                    caller_name="Mom",
                    category="Family",
                    duration_seconds=45,
                    mode_used="vip_bypass",
                    action_taken="VIP_BYPASS",
                    urgency_score=8,
                    urgency_label="HIGH",
                    sms_sent=False,
                    status="BYPASSED",
                    ai_summary="Direct VIP Bypass allowed for Mom while user in Coding Focus."
                ),
                CallLog(
                    caller_number="+919811223344",
                    caller_name="Dr. R. Sharma (HOD / Professor)",
                    category="Professor",
                    duration_seconds=32,
                    mode_used="voice_screen",
                    action_taken="VOICE_SCREEN",
                    urgency_score=7,
                    urgency_label="HIGH",
                    sms_sent=True,
                    sms_text="Respected Dr. Sharma, Aditya is currently in an academic block. He has received your notification and will call back promptly.",
                    transcript=[
                        {"role": "assistant", "message": "Hello, this is Aditya's AI Screener. Aditya is currently in an academic session. How may I direct your call?"},
                        {"role": "caller", "message": "Hi, this is Dr. Sharma. Please tell Aditya to email his final project report by 4 PM today."},
                        {"role": "assistant", "message": "Noted Dr. Sharma. I have logged this high priority task and will alert him immediately."}
                    ],
                    ai_summary="Dr. Sharma requested Aditya to submit the final project report before 4 PM today. Action required.",
                    status="SCREENED"
                ),
                CallLog(
                    caller_number="+911409998877",
                    caller_name="Unknown Telemarketer",
                    category="Spam",
                    duration_seconds=3,
                    mode_used="smart_reject",
                    action_taken="BLOCK_SPAM",
                    urgency_score=1,
                    urgency_label="LOW",
                    sms_sent=False,
                    status="REJECTED",
                    ai_summary="Robocall / Telemarketer detected and instantly dropped without interruption."
                )
            ]
            for c in sample_calls:
                self.add_call(c)

    # ------------------------------------------------------------
    # Call Log Operations
    # ------------------------------------------------------------
    def add_call(self, call: CallLog) -> str:
        data = call.model_dump()
        if self.is_mongo:
            self.db.calls.insert_one(data)
        else:
            self.local_cache["calls"].insert(0, data)
            self._save_local_json()
        return call.id

    def update_call(self, call_id: str, updates: Dict[str, Any]) -> bool:
        if self.is_mongo:
            result = self.db.calls.update_one({"id": call_id}, {"$set": updates})
            return result.modified_count > 0
        else:
            for item in self.local_cache["calls"]:
                if item["id"] == call_id:
                    item.update(updates)
                    self._save_local_json()
                    return True
        return False

    def get_call_by_id(self, call_id: str) -> Optional[CallLog]:
        if self.is_mongo:
            doc = self.db.calls.find_one({"id": call_id}, {"_id": 0})
            return CallLog(**doc) if doc else None
        else:
            for item in self.local_cache["calls"]:
                if item["id"] == call_id:
                    return CallLog(**item)
        return None

    def get_all_calls(self, limit: int = 100) -> List[CallLog]:
        if self.is_mongo:
            cursor = self.db.calls.find({}, {"_id": 0}).sort("timestamp", -1).limit(limit)
            return [CallLog(**doc) for doc in cursor]
        else:
            sorted_calls = sorted(self.local_cache["calls"], key=lambda x: x.get("timestamp", ""), reverse=True)
            return [CallLog(**c) for c in sorted_calls[:limit]]

    def delete_call(self, call_id: str) -> bool:
        if self.is_mongo:
            res = self.db.calls.delete_one({"id": call_id})
            return res.deleted_count > 0
        else:
            self.local_cache["calls"] = [c for c in self.local_cache["calls"] if c["id"] != call_id]
            self._save_local_json()
            return True

    def clear_all_calls(self) -> bool:
        if self.is_mongo:
            self.db.calls.delete_many({})
        else:
            self.local_cache["calls"] = []
            self._save_local_json()
        return True

    # ------------------------------------------------------------
    # VIP Contacts Operations
    # ------------------------------------------------------------
    def add_contact(self, contact: VipContact) -> str:
        data = contact.model_dump()
        if self.is_mongo:
            self.db.contacts.update_one({"phone_number": contact.phone_number}, {"$set": data}, upsert=True)
        else:
            # Replace if phone number matches, else insert
            self.local_cache["contacts"] = [c for c in self.local_cache["contacts"] if c["phone_number"] != contact.phone_number]
            self.local_cache["contacts"].append(data)
            self._save_local_json()
        return contact.id

    def get_contact_by_number(self, phone_number: str) -> Optional[VipContact]:
        normalized = phone_number.strip().replace(" ", "").replace("-", "")
        if self.is_mongo:
            # Try exact match or substring suffix match
            doc = self.db.contacts.find_one({
                "$or": [
                    {"phone_number": normalized},
                    {"phone_number": {"$regex": f"{normalized[-10:]}$"}}
                ]
            }, {"_id": 0})
            return VipContact(**doc) if doc else None
        else:
            for item in self.local_cache["contacts"]:
                target = item["phone_number"].strip().replace(" ", "").replace("-", "")
                if target == normalized or target.endswith(normalized[-10:]) or normalized.endswith(target[-10:]):
                    return VipContact(**item)
        return None

    def get_all_contacts(self) -> List[VipContact]:
        if self.is_mongo:
            cursor = self.db.contacts.find({}, {"_id": 0}).sort("name", 1)
            return [VipContact(**doc) for doc in cursor]
        else:
            sorted_contacts = sorted(self.local_cache["contacts"], key=lambda x: x.get("name", "").lower())
            return [VipContact(**c) for c in sorted_contacts]

    def delete_contact(self, contact_id: str) -> bool:
        if self.is_mongo:
            res = self.db.contacts.delete_one({"id": contact_id})
            return res.deleted_count > 0
        else:
            self.local_cache["contacts"] = [c for c in self.local_cache["contacts"] if c["id"] != contact_id]
            self._save_local_json()
            return True

    # ------------------------------------------------------------
    # Schedules Operations
    # ------------------------------------------------------------
    def add_schedule(self, schedule: ScheduleRule) -> str:
        data = schedule.model_dump()
        if self.is_mongo:
            self.db.schedules.insert_one(data)
        else:
            self.local_cache["schedules"].append(data)
            self._save_local_json()
        return schedule.id

    def get_all_schedules(self) -> List[ScheduleRule]:
        if self.is_mongo:
            cursor = self.db.schedules.find({}, {"_id": 0})
            return [ScheduleRule(**doc) for doc in cursor]
        else:
            return [ScheduleRule(**s) for s in self.local_cache["schedules"]]

    def update_schedule(self, schedule_id: str, updates: Dict[str, Any]) -> bool:
        if self.is_mongo:
            res = self.db.schedules.update_one({"id": schedule_id}, {"$set": updates})
            return res.modified_count > 0
        else:
            for s in self.local_cache["schedules"]:
                if s["id"] == schedule_id:
                    s.update(updates)
                    self._save_local_json()
                    return True
        return False

    def delete_schedule(self, schedule_id: str) -> bool:
        if self.is_mongo:
            res = self.db.schedules.delete_one({"id": schedule_id})
            return res.deleted_count > 0
        else:
            self.local_cache["schedules"] = [s for s in self.local_cache["schedules"] if s["id"] != schedule_id]
            self._save_local_json()
            return True

    # ------------------------------------------------------------
    # System State Persistence
    # ------------------------------------------------------------
    def save_system_state(self, state: SystemState):
        data = state.model_dump()
        if self.is_mongo:
            self.db.settings.update_one({"type": "system_state"}, {"$set": data}, upsert=True)
        else:
            self.local_cache["settings"] = [s for s in self.local_cache.get("settings", []) if s.get("type") != "system_state"]
            data["type"] = "system_state"
            self.local_cache["settings"].append(data)
            self._save_local_json()

    def get_system_state(self) -> Optional[SystemState]:
        if self.is_mongo:
            doc = self.db.settings.find_one({"type": "system_state"}, {"_id": 0})
            return SystemState(**doc) if doc else None
        else:
            for s in self.local_cache.get("settings", []):
                if s.get("type") == "system_state":
                    return SystemState(**s)
        return None

# Global Singleton Database Instance
db = DatabaseManager()
