"""
AI Call Screener & Smart Auto-Responder - Context & Rules Manager
Tracks user availability, timetable schedules, VIP contact matching, and operational modes.
"""

from __future__ import annotations
import os
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple
from dotenv import load_dotenv
from backend.database import db, VipContact, ScheduleRule

load_dotenv(override=True)

logger = logging.getLogger("CallScreener.Context")

class UserStatus:
    AVAILABLE = "AVAILABLE"
    BUSY = "BUSY"
    IN_CLASS = "IN_CLASS"
    CODING_FOCUS = "CODING_FOCUS"
    SLEEPING = "SLEEPING"
    DND = "DND"
    CUSTOM = "CUSTOM"

class ContextManager:
    """
    Evaluates current time against schedule rules or manual override
    to provide the active context for call screening and auto-responses.
    """

    def __init__(self, user_name: str = "Aditya", default_mode: str = "smart_reject"):
        self.user_name = user_name
        self.active_mode = default_mode # smart_reject | voice_screen | vip_bypass
        self.manual_status: Optional[str] = None
        self.manual_note: str = ""
        self.manual_until: str = ""
        self.manual_override: bool = False

        # Attempt to load saved state from database
        self._load_saved_state()

    def _load_saved_state(self):
        env_mode = os.environ.get("ACTIVE_MODE")
        state = db.get_system_state()
        if env_mode:
            self.active_mode = env_mode
        elif state and state.active_mode:
            self.active_mode = state.active_mode

        if state and state.override_active:
            self.manual_status = state.current_status
            self.manual_note = state.custom_note
            self.manual_until = state.until_time
            self.manual_override = True

    def set_mode(self, mode: str):
        """Sets operational mode: 'smart_reject', 'voice_screen', or 'vip_bypass'."""
        if mode in ["smart_reject", "voice_screen", "vip_bypass"]:
            self.active_mode = mode
            logger.info(f"Operational mode updated to: {mode}")
            self._persist()

    def set_manual_status(self, status: str, note: str = "", until: str = ""):
        """Sets manual context override (e.g. IN_CLASS, CODING_FOCUS, etc.)."""
        self.manual_status = status.upper()
        self.manual_note = note
        self.manual_until = until
        self.manual_override = (self.manual_status != UserStatus.AVAILABLE or bool(note))
        logger.info(f"Manual context set: {self.manual_status} ('{note}'), Until: {until}")
        self._persist()

    def clear_manual_override(self):
        """Clears manual override and returns to automated schedule."""
        self.manual_override = False
        self.manual_status = None
        self.manual_note = ""
        self.manual_until = ""
        logger.info("Manual context override cleared. Resuming automated timetable.")
        self._persist()

    def _persist(self):
        from backend.database import SystemState
        ctx = self.get_current_context()
        state = SystemState(
            active_mode=self.active_mode,
            current_status=ctx["status"],
            custom_note=ctx["note"],
            until_time=ctx["until"],
            override_active=self.manual_override
        )
        db.save_system_state(state)

    def get_current_context(self) -> Dict[str, Any]:
        """
        Calculates the effective user context at this exact moment.
        If manual override is active, returns manual status.
        Otherwise, inspects current day and time against active ScheduleRules.
        """
        now = datetime.now()
        current_time_str = now.strftime("%H:%M") # "14:15"
        current_weekday = now.weekday() # 0 = Monday, 6 = Sunday

        # 1. Manual override takes top priority
        if self.manual_override and self.manual_status:
            return {
                "status": self.manual_status,
                "note": self.manual_note,
                "until": self.manual_until,
                "is_manual": True,
                "source": "Manual Override"
            }

        # 2. Check scheduled rules from database
        schedules = db.get_all_schedules()
        for rule in schedules:
            if not rule.is_active:
                continue

            # Day check (-1 means every day)
            if rule.day_of_week != -1 and rule.day_of_week != current_weekday:
                continue

            # Time range check
            if rule.start_time <= current_time_str <= rule.end_time:
                return {
                    "status": rule.status,
                    "note": rule.note or rule.title,
                    "until": rule.end_time,
                    "is_manual": False,
                    "source": f"Timetable: {rule.title}"
                }

        # 3. Default to AVAILABLE
        return {
            "status": UserStatus.AVAILABLE,
            "note": "Ready to take calls",
            "until": "",
            "is_manual": False,
            "source": "Default Idle"
        }

    def evaluate_incoming_caller(self, phone_number: str) -> Tuple[str, Optional[VipContact], Dict[str, Any]]:
        """
        Evaluates an incoming call against VIP rules and active mode.
        Returns:
            recommended_action: 'VIP_BYPASS' | 'REJECT_AND_SMS' | 'VOICE_SCREEN' | 'BLOCK_SPAM'
            contact: Optional[VipContact]
            context: Dict[str, Any]
        """
        context = self.get_current_context()
        contact = db.get_contact_by_number(phone_number)

        # 1. Explicit Spam / Block list
        if contact and contact.priority == "Block":
            return "BLOCK_SPAM", contact, context

        # 2. VIP Contact with Bypass Enabled
        if contact and contact.priority == "VIP" and contact.bypass_enabled:
            # If user is Sleeping/DND, check if Emergency or Family
            if context["status"] in [UserStatus.SLEEPING, UserStatus.DND]:
                if contact.category in ["Family", "Emergency"]:
                    return "VIP_BYPASS", contact, context
                else:
                    # Let screen or reject during deep sleep even if VIP
                    return ("VOICE_SCREEN" if self.active_mode == "voice_screen" else "REJECT_AND_SMS"), contact, context
            return "VIP_BYPASS", contact, context

        # 3. Check Global Operating Mode
        if self.active_mode == "vip_bypass":
            # In VIP Bypass Only mode, non-VIP callers are rejected with polite SMS
            return "REJECT_AND_SMS", contact, context

        if self.active_mode == "voice_screen":
            # If user is in class or sleeping, voice screening might be disruptive on speakerphone,
            # but virtual screening or text-summary is great.
            return "VOICE_SCREEN", contact, context

        # Default Mode: smart_reject
        return "REJECT_AND_SMS", contact, context
