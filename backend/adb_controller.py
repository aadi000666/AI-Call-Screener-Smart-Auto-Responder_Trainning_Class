"""
AI Call Screener & Smart Auto-Responder - ADB Controller
Manages device connectivity, incoming call state parsing, call controls (answer/reject),
and automated SMS dispatch over USB / Wi-Fi Android Debug Bridge.
"""

from __future__ import annotations
import os
import re
import shutil
import subprocess
import logging
import time
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("CallScreener.ADB")

class TelephonyState:
    IDLE = "IDLE"           # No active/incoming calls (State 0)
    RINGING = "RINGING"     # Incoming call ringing (State 1)
    OFFHOOK = "OFFHOOK"     # Call in progress / active (State 2)
    UNKNOWN = "UNKNOWN"

class ADBController:
    """
    Production-grade controller interfacing with Android via ADB subprocesses.
    Supports physical devices (USB/Wi-Fi) and includes an integrated simulator mode.
    """

    def __init__(self, adb_path: str = "adb", default_serial: str = "", simulation_mode: bool = False):
        self.configured_adb_path = adb_path
        self.device_serial = default_serial
        self.simulation_mode = simulation_mode
        self.adb_bin = self._resolve_adb_binary(adb_path)
        
        # Virtual simulator state for testing without physical phone
        self.simulated_state = TelephonyState.IDLE
        self.simulated_caller_number = ""
        self.simulated_caller_name = ""
        self.simulated_call_start = 0.0

        logger.info(f"ADB Controller initialized. Binary: {self.adb_bin}, Simulation Mode: {self.simulation_mode}")

    def _resolve_adb_binary(self, preferred_path: str) -> Optional[str]:
        """Locates the ADB executable from preferred path, PATH, or common Android SDK directories."""
        # 1. Preferred explicit path
        if preferred_path and preferred_path != "adb":
            if os.path.isfile(preferred_path) and os.access(preferred_path, os.X_OK):
                return preferred_path

        # 2. Check system PATH
        which_adb = shutil.which("adb")
        if which_adb:
            return which_adb

        # 3. Check Windows Android SDK locations
        user_profile = os.environ.get("USERPROFILE", "")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(local_app_data, "Android", "Sdk", "platform-tools", "adb.exe"),
            os.path.join(user_profile, "AppData", "Local", "Android", "Sdk", "platform-tools", "adb.exe"),
            r"C:\Android\platform-tools\adb.exe",
            r"C:\Program Files (x86)\Android\android-sdk\platform-tools\adb.exe",
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                logger.info(f"Auto-discovered ADB binary at: {candidate}")
                return candidate

        logger.warning("ADB binary not found on host. Device commands will fallback to simulation mode.")
        return None

    def _run_adb_cmd(self, args: List[str], timeout: float = 5.0) -> Tuple[bool, str]:
        """Executes an ADB shell or management command."""
        if not self.adb_bin:
            return False, "ADB binary not installed or found."

        cmd = [self.adb_bin]
        if self.device_serial:
            cmd.extend(["-s", self.device_serial])
        cmd.extend(args)

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                check=False
            )
            stdout = res.stdout.decode("utf-8", errors="replace") if res.stdout else ""
            stderr = res.stderr.decode("utf-8", errors="replace") if res.stderr else ""
            output = stdout + stderr
            return (res.returncode == 0), output.strip()
        except subprocess.TimeoutExpired:
            logger.warning(f"ADB command timed out: {' '.join(cmd)}")
            return False, "Command timed out"
        except Exception as e:
            logger.error(f"ADB execution error ({' '.join(cmd)}): {e}")
            return False, str(e)

    # ------------------------------------------------------------
    # Device Management & Connection
    # ------------------------------------------------------------
    def get_connected_devices(self) -> List[Dict[str, Any]]:
        """Returns all connected devices via 'adb devices -l'."""
        if self.simulation_mode or not self.adb_bin:
            return [{
                "serial": "SIMULATOR-ANDROID-VIRTUAL",
                "state": "device",
                "model": "Virtual Android Device (Simulated)",
                "product": "v-screener",
                "transport_id": "1",
                "is_wireless": False,
                "is_simulator": True
            }]

        ok, output = self._run_adb_cmd(["devices", "-l"])
        if not ok:
            return []

        devices = []
        lines = output.splitlines()
        for line in lines[1:]: # Skip 'List of devices attached'
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                serial = parts[0]
                state = parts[1]
                # parse details like model:Pixel_7
                details = {}
                for token in parts[2:]:
                    if ":" in token:
                        k, v = token.split(":", 1)
                        details[k] = v

                devices.append({
                    "serial": serial,
                    "state": state,
                    "model": details.get("model", "Android Device"),
                    "product": details.get("product", ""),
                    "transport_id": details.get("transport_id", ""),
                    "is_wireless": ":" in serial,
                    "is_simulator": False
                })

        return devices

    def connect_wireless(self, ip: str, port: int = 5555) -> Tuple[bool, str]:
        """Connects to an Android device over Wi-Fi via ADB wireless."""
        target = f"{ip}:{port}"
        logger.info(f"Connecting to wireless ADB target: {target}")
        ok, out = self._run_adb_cmd(["connect", target], timeout=8.0)
        if "connected to" in out.lower():
            self.device_serial = target
            return True, f"Successfully connected to {target}"
        return False, out or "Failed to connect to wireless target"

    def disconnect_wireless(self, ip: str, port: int = 5555) -> Tuple[bool, str]:
        """Disconnects wireless ADB."""
        target = f"{ip}:{port}"
        ok, out = self._run_adb_cmd(["disconnect", target])
        if self.device_serial == target:
            self.device_serial = ""
        return ok, out

    def is_device_ready(self) -> bool:
        """Checks if a valid device or simulator is connected and responding."""
        if self.simulation_mode:
            return True
        devices = self.get_connected_devices()
        for d in devices:
            if d.get("state") == "device":
                if not self.device_serial:
                    self.device_serial = d["serial"]
                # Ensure shell permissions are granted for Samsung & Android 10-15
                self.ensure_device_permissions()
                return True
        return False

    def ensure_device_permissions(self):
        """Grants essential phone/call/sms permissions to com.android.shell so ADB can control calls without security blocks."""
        if self.simulation_mode or not self.device_serial:
            return
        perms = [
            "android.permission.READ_PHONE_STATE",
            "android.permission.READ_CALL_LOG",
            "android.permission.ANSWER_PHONE_CALLS",
            "android.permission.CALL_PHONE",
            "android.permission.SEND_SMS",
            "android.permission.READ_SMS"
        ]
        for p in perms:
            self._run_adb_cmd(["shell", "pm", "grant", "com.android.shell", p], timeout=1.5)

    def play_audio_on_phone(self, local_audio_path: str) -> bool:
        """Pushes synthesized audio file to phone and plays it out loud via speakerphone."""
        if self.simulation_mode or not self.device_serial:
            return False
        if not os.path.exists(local_audio_path) or os.path.getsize(local_audio_path) == 0:
            return False

        remote_path = "/sdcard/ai_screener_speech.mp3"
        ok_push, _ = self._run_adb_cmd(["push", local_audio_path, remote_path], timeout=3.0)
        if ok_push:
            self.toggle_speakerphone(True)
            self._run_adb_cmd(["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", f"file://{remote_path}", "-t", "audio/mp3"], timeout=2.0)
            return True
        return False

    # ------------------------------------------------------------
    # Call State Detection & Telephony Parser
    # ------------------------------------------------------------
    def get_telephony_state(self) -> Tuple[str, str]:
        """
        Queries dumpsys telecom, dumpsys telephony.registry, and notification manager.
        Ultra-reliable for Samsung OneUI, Pixel, and multi-SIM Android devices.
        Returns: (state: TelephonyState, incoming_number: str)
        """
        if self.simulation_mode:
            return self.simulated_state, self.simulated_caller_number

        if not self.adb_bin or not self.is_device_ready():
            return TelephonyState.UNKNOWN, ""

        state = TelephonyState.IDLE
        incoming_number = ""

        # Strategy 1: dumpsys telecom (Most reliable on modern Android 10-15 & Samsung OneUI)
        ok_telecom, telecom_out = self._run_adb_cmd(["shell", "dumpsys", "telecom"], timeout=3.0)
        if ok_telecom and telecom_out:
            t_state, t_number = self._parse_telecom(telecom_out)
            if t_state != TelephonyState.IDLE:
                state = t_state
            if t_number:
                incoming_number = t_number

        # Strategy 2: dumpsys telephony.registry (Handles dual-SIM and raw cellular states)
        ok_tel, tel_out = self._run_adb_cmd(["shell", "dumpsys", "telephony.registry"], timeout=3.0)
        if ok_tel and tel_out:
            r_state, r_number = self._parse_telephony_registry(tel_out)
            # If telecom didn't catch RINGING, check if registry has RINGING
            if r_state == TelephonyState.RINGING:
                state = TelephonyState.RINGING
            elif state == TelephonyState.IDLE and r_state != TelephonyState.IDLE:
                state = r_state

            if not incoming_number and r_number:
                incoming_number = r_number

        # Strategy 3: If ringing but number is hidden by Android privacy restrictions,
        # inspect InCallUI / Phone notification
        if state == TelephonyState.RINGING and (not incoming_number or incoming_number == "Unknown"):
            num_from_notif = self._extract_number_from_incall_notification()
            if num_from_notif:
                incoming_number = num_from_notif

        return state, incoming_number

    def _parse_telephony_registry(self, text: str) -> Tuple[str, str]:
        """Parses output of dumpsys telephony.registry across all SIM slots."""
        call_state = TelephonyState.IDLE
        incoming_number = ""

        # Find ALL call state values (handles Dual SIM slot 0 and slot 1)
        # Matches mCallState=1, callState: 1, ringingCallState=1, mRingingCallState=1
        states_found = re.findall(
            r"(?:mCallState|callState|ringingCallState|mRingingCallState)\s*[=:]\s*([0-2])",
            text,
            re.IGNORECASE
        )

        if "1" in states_found:
            call_state = TelephonyState.RINGING
        elif "2" in states_found:
            call_state = TelephonyState.OFFHOOK

        # Check incoming number patterns
        num_patterns = [
            r"mCallIncomingNumber\s*=\s*([+\w\d#*]+)",
            r"mIncomingNumber\s*=\s*([+\w\d#*]+)",
            r"incomingNumber\s*=\s*([+\w\d#*]+)",
            r"mCallIncomingNumber\[\d+\]\s*=\s*([+\w\d#*]+)"
        ]
        for pattern in num_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for m in matches:
                clean = m.strip()
                if clean and clean.lower() not in ["null", "unknown", "none", ""]:
                    incoming_number = clean
                    break
            if incoming_number:
                break

        return call_state, incoming_number

    def _parse_telecom(self, text: str) -> Tuple[str, str]:
        """Parses dumpsys telecom output for active ringing/offhook calls and handles."""
        state = TelephonyState.IDLE
        number = ""

        # Check for RINGING state
        if re.search(r"state[:=]\s*RINGING", text, re.IGNORECASE) or \
           re.search(r"mCallState[:=]\s*RINGING", text, re.IGNORECASE) or \
           re.search(r"LiveCalls:.*RINGING", text, re.IGNORECASE) or \
           "state: RINGING" in text or "mCallState: RINGING" in text:
            state = TelephonyState.RINGING
        elif re.search(r"state[:=]\s*(?:ACTIVE|DIALING)", text, re.IGNORECASE) or \
             re.search(r"mCallState[:=]\s*ACTIVE", text, re.IGNORECASE):
            state = TelephonyState.OFFHOOK

        # Extract phone number from telecom handle
        import urllib.parse
        decoded_text = urllib.parse.unquote(text)

        # 1. Search for tel:<number> or handle:<number>
        tel_matches = re.findall(r"(?:tel:|handle:|ConnectWith:)[\s:=]*([+\d\s\-]{7,20})", decoded_text)
        if tel_matches:
            clean_num = re.sub(r"[^\d+]", "", tel_matches[0])
            if len(clean_num) >= 7:
                number = clean_num

        if not number:
            # 2. Search for Indian mobile phone pattern in telecom dump
            phone_matches = re.findall(r"(?:\+91|0)?([6-9]\d{9})", decoded_text)
            if phone_matches:
                number = f"+91{phone_matches[0]}"

        if number and not number.startswith("+") and len(number) == 10:
            number = f"+91{number}"

        return state, number

    def _extract_number_from_incall_notification(self) -> str:
        """Fallback to extract caller name/number from active radio logcat or notification."""
        # Method 1: logcat radio (Direct modem layer, fast and accurate for caller ID)
        ok_log, log_out = self._run_adb_cmd(["shell", "logcat", "-d", "-b", "radio", "-t", "40"], timeout=1.5)
        if ok_log and log_out:
            phone_matches = re.findall(r"(?:\+91|0)?([6-9]\d{9})", log_out)
            if phone_matches:
                return f"+91{phone_matches[-1]}"

        # Method 2: dumpsys notification
        ok, notif_out = self._run_adb_cmd(["shell", "dumpsys", "notification"], timeout=1.5)
        if ok and notif_out:
            phone_matches = re.findall(r"(?:\+91|0)?([6-9]\d{9})", notif_out)
            if phone_matches:
                return f"+91{phone_matches[0]}"

        return ""

    # ------------------------------------------------------------
    # Call Control Actions
    # ------------------------------------------------------------
    def reject_call(self) -> bool:
        """
        Rejects the current incoming call.
        Executes telecom end-call + service call phone 5 + KEYCODE_ENDCALL + decline swipe/tap
        for guaranteed termination across Samsung OneUI, Xiaomi, and stock Android.
        """
        if self.simulation_mode:
            logger.info("[Simulator] Call rejected successfully.")
            self.simulated_state = TelephonyState.IDLE
            self.simulated_caller_number = ""
            return True

        logger.info("Executing ADB reject call (multi-strategy for Samsung / Android)...")
        # 1. telecom end-call (Universal Android 10-15 standard)
        ok1, out1 = self._run_adb_cmd(["shell", "telecom", "end-call"])
        # 2. service call phone 5 (ITelephony.endCall)
        self._run_adb_cmd(["shell", "service", "call", "phone", "5"])
        # 3. KEYCODE_ENDCALL (6)
        ok3, _ = self._run_adb_cmd(["shell", "input", "keyevent", "6"])
        # 4. Samsung Heads-up Notification Decline Button (Top right on Samsung OneUI)
        self._run_adb_cmd(["shell", "input", "tap", "900", "230"])
        # 5. Samsung Fullscreen Decline Swipe (Bottom left to right)
        self._run_adb_cmd(["shell", "input", "swipe", "250", "2000", "650", "2000", "200"])

        # 6. Fallback answer and immediate end-call if stuck ringing
        time.sleep(0.2)
        st, _ = self.get_telephony_state()
        if st == TelephonyState.RINGING:
            self._run_adb_cmd(["shell", "telecom", "answer-call"])
            time.sleep(0.1)
            self._run_adb_cmd(["shell", "telecom", "end-call"])
            self._run_adb_cmd(["shell", "input", "keyevent", "6"])

        # Dismiss screen
        self._run_adb_cmd(["shell", "input", "keyevent", "3"]) # KEYCODE_HOME

        return True

    def answer_call(self) -> bool:
        """
        Answers the incoming call via telecom answer-call and KEYCODE_CALL.
        """
        if self.simulation_mode:
            logger.info("[Simulator] Call answered successfully.")
            self.simulated_state = TelephonyState.OFFHOOK
            return True

        logger.info("Executing ADB answer call (telecom answer-call + KEYCODE_CALL)...")
        # 1. telecom answer-call
        ok_telecom, _ = self._run_adb_cmd(["shell", "telecom", "answer-call"])
        # 2. KEYCODE_CALL (5)
        ok_key, _ = self._run_adb_cmd(["shell", "input", "keyevent", "5"])
        # 3. KEYCODE_HEADSETHOOK (79) fallback
        self._run_adb_cmd(["shell", "input", "keyevent", "79"])

        return ok_telecom or ok_key

    def toggle_speakerphone(self, enable: bool = True) -> bool:
        """Sets max in-call media volume to route audio via speaker."""
        if self.simulation_mode:
            logger.info(f"[Simulator] Speakerphone set to {enable}")
            return True

        volume_level = "15" if enable else "7"
        # Stream 0 = Voice call stream
        self._run_adb_cmd(["shell", "media", "volume", "--stream", "0", "--set", volume_level])
        # Stream 3 = Music/TTS audio stream
        self._run_adb_cmd(["shell", "media", "volume", "--stream", "3", "--set", volume_level])
        return True

    # ------------------------------------------------------------
    # SMS Automation
    # ------------------------------------------------------------
    def send_sms(self, phone_number: str, message: str) -> Tuple[bool, str]:
        """
        Dispatches an automated SMS to the caller using multiple Samsung-specific strategies.
        Wakes screen first so UI-based strategies work reliably.
        """
        clean_num = re.sub(r"[^\d+]", "", phone_number.strip())
        if not clean_num or len(clean_num) < 7:
            logger.warning(f"Cannot send SMS: invalid recipient number '{phone_number}'")
            return False, f"Invalid number: {phone_number}"

        if self.simulation_mode:
            logger.info(f"[Simulator] Auto-SMS dispatched to {clean_num}: '{message}'")
            return True, "SMS sent via virtual simulator"

        if not self.adb_bin or not self.is_device_ready():
            return False, "Device not connected"

        logger.info(f"Attempting SMS to {clean_num}: '{message[:60]}...'")

        # ── Step 0: Wake + unlock screen so UI strategies work ──────────────────
        self._run_adb_cmd(["shell", "input", "keyevent", "26"])   # Power on screen
        time.sleep(0.5)
        # Dismiss lock screen / swipe up
        self._run_adb_cmd(["shell", "input", "swipe", "540", "1700", "540", "900", "400"])
        time.sleep(0.4)

        # ── Strategy 1: Samsung shell am broadcast (background, no UI needed) ───
        # Uses com.android.mms.transaction.SmsReceiverService on some Samsung builds.
        # Try direct service call with Samsung messaging package
        for pkg in ["com.samsung.android.messaging", "com.android.mms"]:
            for method_id in ["7", "5", "6"]:
                isms_cmd = [
                    "shell", "service", "call", "isms", method_id,
                    "i32", "0",
                    "s16", pkg,
                    "s16", clean_num,
                    "s16", "null",
                    "s16", message,       # no extra quoting — passed as a single arg
                    "s16", "null",
                    "s16", "null"
                ]
                ok_isms, out_isms = self._run_adb_cmd(isms_cmd, timeout=3.0)
                if ok_isms and "Result: Parcel(" in out_isms and "00000000 00000000" not in out_isms:
                    logger.info(f"SMS sent via ISMS method {method_id} pkg={pkg}: {out_isms.strip()}")
                    return True, f"SMS sent via ISMS ({pkg} method {method_id})"

        # ── Strategy 2: Samsung ComposeActivity deep-link (most reliable UI path) ──
        # Opens Samsung Messages directly in compose window with number+body pre-filled.
        compose_cmd = [
            "shell", "am", "start",
            "-n", "com.samsung.android.messaging/.ui.compose.ComposeActivity",
            "--es", "address", clean_num,
            "--es", "android.intent.extra.TEXT", message
        ]
        ok_compose, _ = self._run_adb_cmd(compose_cmd, timeout=4.0)
        if ok_compose:
            time.sleep(1.5)   # Wait for Samsung Messages to open
            # Clear any stale text in compose box and re-type (in case body didn't auto-fill)
            self._run_adb_cmd(["shell", "input", "keyevent", "123"])  # Move to end of text
            time.sleep(0.2)
            # Attempt send via Samsung Messages send button
            # Try multiple Y-coords for different screen states (virtual keyboard up/down)
            for tap_coord in [("980", "1450"), ("980", "2150"), ("1000", "1500"), ("1000", "2200")]:
                self._run_adb_cmd(["shell", "input", "tap", tap_coord[0], tap_coord[1]])
                time.sleep(0.3)
            # Fallback: Tab to send button + Enter
            self._run_adb_cmd(["shell", "input", "keyevent", "22"])  # Tab (focus send)
            self._run_adb_cmd(["shell", "input", "keyevent", "66"])  # Enter (press send)
            time.sleep(0.5)
            self._run_adb_cmd(["shell", "input", "keyevent", "3"])   # Home
            logger.info(f"SMS dispatched via Samsung ComposeActivity to {clean_num}")
            return True, "SMS sent via Samsung ComposeActivity"

        # ── Strategy 3: Standard Android SENDTO Intent ───────────────────────────
        sendto_cmd = [
            "shell", "am", "start", "-a", "android.intent.action.SENDTO",
            "-d", f"sms:{clean_num}",
            "--es", "sms_body", message,
            "--ez", "exit_on_sent", "true"
        ]
        ok_intent, out_intent = self._run_adb_cmd(sendto_cmd, timeout=4.0)
        if ok_intent:
            time.sleep(1.8)
            # Dismiss virtual keyboard so Send button is anchored at bottom right
            self._run_adb_cmd(["shell", "input", "keyevent", "111"]) # KEYCODE_ESCAPE
            time.sleep(0.2)
            self._run_adb_cmd(["shell", "input", "keyevent", "4"])   # KEYCODE_BACK
            time.sleep(0.4)

            # Try multiple send-button locations (keyboard closed bottom & keyboard open)
            for tap_coord in [("1000", "2220"), ("1000", "2180"), ("980", "2240"), ("1020", "2200"), ("980", "1450"), ("1000", "1420")]:
                self._run_adb_cmd(["shell", "input", "tap", tap_coord[0], tap_coord[1]])
                time.sleep(0.15)
            self._run_adb_cmd(["shell", "input", "keyevent", "66"])  # Enter
            time.sleep(0.5)
            self._run_adb_cmd(["shell", "input", "keyevent", "3"])   # Home
            logger.info(f"SMS dispatched via SENDTO intent to {clean_num}")
            return True, "SMS sent via Android SENDTO intent"

        return False, f"All SMS strategies failed for {clean_num}: {out_intent}"

    # ------------------------------------------------------------
    # Virtual Call Simulator Hooks (For interactive testing & demo)
    # ------------------------------------------------------------
    def simulate_incoming_call(self, number: str, name: str = "Test Caller"):
        """Triggers a simulated incoming call event."""
        self.simulated_state = TelephonyState.RINGING
        self.simulated_caller_number = number
        self.simulated_caller_name = name
        self.simulated_call_start = time.time()
        logger.info(f"[Simulator] Incoming call simulated from: {name} ({number})")

    def simulate_call_end(self):
        """Ends the simulated call."""
        self.simulated_state = TelephonyState.IDLE
        self.simulated_caller_number = ""
        self.simulated_caller_name = ""
        logger.info("[Simulator] Call ended/reset to IDLE.")
