"""
AI Call Screener & Smart Auto-Responder - CLI Application
=========================================================
Standalone Windows executable script.
Prompts for user configuration / Gemini API Key and runs autonomous call monitoring.
"""

import os
import sys
import time
import re
import shutil
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def find_adb():
    """Finds adb executable from standard paths, PATH, or local directory."""
    candidates = [
        r"C:\platform-tools\adb.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
        os.path.join(os.getcwd(), "adb.exe"),
        os.path.join(os.getcwd(), "platform-tools", "adb.exe"),
        shutil.which("adb") or ""
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return "adb"


ADB_BIN = find_adb()


def run_adb(args, timeout=6):
    """Executes ADB command safely."""
    cmd = [ADB_BIN] + args
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (r.stdout + r.stderr).decode("utf-8", errors="replace").strip()
    except Exception as e:
        return str(e)


def extract_number(text):
    text = urllib.parse.unquote(text)
    # tel: pattern
    tel = re.findall(r"tel:(\+?[\d]{7,13})", text)
    for t in tel:
        n = re.sub(r"[^\d]", "", t)
        if len(n) == 10 and n[0] in "6789":
            return "+91" + n
        if len(n) == 12 and n.startswith("91"):
            return "+" + n

    # handle=tel:
    h = re.findall(r"handle=tel:(\+?[\d]+)", text)
    if h:
        n = re.sub(r"[^\d]", "", h[0])
        if len(n) == 10:
            return "+91" + n
        if len(n) == 12 and n.startswith("91"):
            return "+" + n

    # 10 digit Indian number
    mob = re.findall(r"(?<!\d)([6-9]\d{9})(?!\d)", text)
    if mob:
        return "+91" + mob[-1]

    return ""


def get_call_state():
    reg_out = run_adb(["shell", "dumpsys", "telephony.registry"], timeout=3)
    state = "IDLE"
    if "mCallState=1" in reg_out:
        state = "RINGING"
    elif "mCallState=2" in reg_out:
        state = "OFFHOOK"

    if state == "IDLE":
        return state, ""

    telecom_out = run_adb(["shell", "dumpsys", "telecom"], timeout=3)
    number = extract_number(telecom_out)

    if not number:
        number = extract_number(reg_out)

    return state, number


def get_caller_from_call_log():
    # Android content query on call_log
    out = run_adb(["shell", "content", "query", "--uri", "content://call_log/calls",
                   "--projection", "number", "--sort", r"date\ DESC"], timeout=4)
    # Match the latest entry's number
    match = re.search(r"Row:\s*0\s+number=([+\d]+)", out)
    if not match:
        match = re.search(r"number=([+\d]+)", out)
    if match:
        num = match.group(1).strip()
        clean = re.sub(r"[^\d+]", "", num)
        if len(clean) == 10 and not clean.startswith("+"):
            return "+91" + clean
        if len(clean) >= 10:
            return clean
    return ""


def reject_call():
    print("  -> Rejecting incoming call via ADB...")
    run_adb(["shell", "telecom", "end-call"])
    run_adb(["shell", "service", "call", "phone", "5"])
    run_adb(["shell", "input", "keyevent", "6"])
    run_adb(["shell", "input", "tap", "900", "230"])
    run_adb(["shell", "input", "swipe", "250", "2000", "650", "2000", "200"])
    time.sleep(0.4)
    run_adb(["shell", "input", "keyevent", "3"])
    print("  [SUCCESS] Call rejected.")


def send_sms(number, message):
    print(f"  -> Dispatching SMS to {number}...")
    safe_msg = re.sub(r"[()!&|;<>`$\\\"'{}[\]]", "", message).strip()

    run_adb(["shell", "input", "keyevent", "224"])
    time.sleep(0.3)
    run_adb(["shell", "wm", "dismiss-keyguard"])
    run_adb(["shell", "input", "keyevent", "82"])
    time.sleep(0.3)

    run_adb([
        "shell", "am", "start", "-a", "android.intent.action.SENDTO",
        "-d", f"sms:{number}",
        "--es", "sms_body", f"'{safe_msg}'",
        "--ez", "exit_on_sent", "true"
    ], timeout=5)
    time.sleep(2.0)

    tapped = False
    run_adb(["shell", "uiautomator", "dump", "/sdcard/sms_ui.xml"], timeout=4)
    xml_data = run_adb(["shell", "cat", "/sdcard/sms_ui.xml"], timeout=3)

    if "<hierarchy" in xml_data:
        try:
            root = ET.fromstring(xml_data)
            for node in root.iter("node"):
                res = node.get("resource-id", "").lower()
                desc = node.get("content-desc", "").lower()
                text = node.get("text", "").lower()
                if "send" in res or "send" in desc or desc == "भेजें" or text == "send":
                    bounds = node.get("bounds", "")
                    nums = list(map(int, re.findall(r"\d+", bounds)))
                    if len(nums) == 4:
                        cx = (nums[0] + nums[2]) // 2
                        cy = (nums[1] + nums[3]) // 2
                        print(f"  [UIAutomator] Found Send button at ({cx}, {cy}). Tapping...")
                        run_adb(["shell", "input", "tap", str(cx), str(cy)])
                        tapped = True
                        break
        except Exception:
            pass

    if not tapped:
        run_adb(["shell", "input", "keyevent", "61"])
        time.sleep(0.1)
        run_adb(["shell", "input", "keyevent", "66"])
        time.sleep(0.2)
        for y in ["1250", "1280", "1300", "2150"]:
            run_adb(["shell", "input", "tap", "990", y])
            time.sleep(0.1)

    time.sleep(1.0)
    run_adb(["shell", "input", "keyevent", "3"])
    print(f"  [SUCCESS] Auto-Reply SMS sent to {number}!")


def generate_ai_reply(caller_number, user_context, api_key):
    """Generates an intelligent context-aware reply using Google Gemini API if key is provided."""
    if not api_key or api_key.strip() == "":
        return f"Hi! I am currently occupied with {user_context} and cannot answer your call. I will get back to you soon."

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            f"You are an AI Call Screener replying on behalf of the user who is currently: '{user_context}'. "
            f"Generate a polite, concise SMS reply (under 140 chars) to an incoming caller from {caller_number}. "
            f"Do not include quotes or conversational filler. Output only the message text."
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        text = response.text.strip() if response and response.text else ""
        if text:
            return text
    except Exception as e:
        print(f"  [AI Engine Note] Gemini API error ({e}), using contextual fallback.")

    return f"Hi! I am currently occupied with {user_context} and cannot take your call right now. I will call you back shortly."


def main():
    clear_screen()
    print("=" * 65)
    print("      AI CALL SCREENER & SMART AUTO-RESPONDER v1.0")
    print("=" * 65)
    print(" Autonomous Android Telephony Gatekeeper & Smart Responder")
    print(" Developed by Aditya | Compatible with Samsung / Stock Android")
    print("=" * 65)
    print()

    # 1. API KEY CONFIGURATION SECTION
    print("[SECTION 1: AI CONFIGURATION]")
    saved_key = os.environ.get("GEMINI_API_KEY", "")
    print("Enter your Google Gemini API Key for dynamic contextual AI replies.")
    if saved_key:
        print(f"Detected existing key: {saved_key[:6]}...{saved_key[-4:]}")
        user_key = input("Press ENTER to use existing key, or type new key: ").strip()
        api_key = user_key if user_key else saved_key
    else:
        api_key = input("Gemini API Key (optional - press Enter for rule-based mode): ").strip()

    if api_key:
        print("-> Gemini AI Engine: ENABLED (Model: gemini-2.5-flash)\n")
    else:
        print("-> Gemini AI Engine: Rule-Based Heuristic Mode\n")

    # 2. USER CONTEXT CONFIGURATION
    print("[SECTION 2: USER AVAILABILITY STATUS]")
    print("Choose your current availability status:")
    print("  1. Final Exam Preparation (Default)")
    print("  2. Deep Coding Sprint / Focus")
    print("  3. Attending Lecture / In Class")
    print("  4. Sleeping / Resting (DND)")
    print("  5. Custom Status Note")
    choice = input("Select option [1-5, default=1]: ").strip()

    if choice == "2":
        context_status = "Deep Coding Focus Sprint"
    elif choice == "3":
        context_status = "College Lecture / In Class"
    elif choice == "4":
        context_status = "Resting / Do Not Disturb"
    elif choice == "5":
        custom = input("Enter custom status message: ").strip()
        context_status = custom if custom else "Busy right now"
    else:
        context_status = "Final Exam Preparation"

    print(f"-> Active Context: '{context_status}'\n")

    # 3. TIMING & FALLBACK CONFIGURATION
    wait_time_input = input("Wait time before auto-rejecting (seconds, default=5): ").strip()
    try:
        wait_seconds = float(wait_time_input) if wait_time_input else 5.0
    except ValueError:
        wait_seconds = 5.0

    fallback_num = input("Fallback phone number if caller ID is private [default: +916307028119]: ").strip()
    if not fallback_num:
        fallback_num = "+916307028119"

    print("\n[SECTION 3: DEVICE CONNECTION]")
    print(f"ADB Binary: {ADB_BIN}")
    devices = run_adb(["devices"])
    print(f"Attached Devices:\n{devices}\n")

    if "device" not in devices:
        print("[WARNING] No device authorized or connected!")
        print("Please ensure USB/Wireless debugging is enabled and authorized on your phone.")
        input("Press Enter once connected to continue...")

    run_adb(["shell", "pm", "grant", "com.android.shell", "android.permission.READ_PHONE_STATE"])
    run_adb(["shell", "pm", "grant", "com.android.shell", "android.permission.READ_CALL_LOG"])

    print("\n" + "=" * 65)
    print("  SYSTEM READY - LISTENING FOR INCOMING CALLS")
    print(f"  Status       : {context_status}")
    print(f"  Grace Period : {wait_seconds}s before rejection")
    print("  Press Ctrl+C to stop.")
    print("=" * 65 + "\n")

    last_state = "IDLE"
    ring_start = None
    captured_number = ""

    while True:
        try:
            state, number = get_call_state()

            # Incoming call detected
            if state == "RINGING" and last_state != "RINGING":
                ring_start = time.time()
                captured_number = number if number else ""
                disp = captured_number if captured_number else "Incoming Caller..."
                print(f"[INCOMING CALL] Detected ringing from: {disp}")
                print(f"  Waiting {wait_seconds}s (answer manually if you want)...")

            # Call continues ringing past grace period
            elif state == "RINGING" and ring_start is not None:
                if not captured_number and number:
                    captured_number = number
                    print(f"  -> Caller number identified: {captured_number}")

                elapsed = time.time() - ring_start
                if elapsed >= wait_seconds:
                    caller_disp = captured_number if captured_number else "Caller"
                    print(f"\n[TIME UP] Auto-rejecting call from {caller_disp}...")

                    reject_call()

                    target = captured_number
                    if not target:
                        time.sleep(0.4)
                        target = get_caller_from_call_log()

                    if not target and fallback_num:
                        target = fallback_num

                    if target:
                        reply_msg = generate_ai_reply(target, context_status, api_key)
                        send_sms(target, reply_msg)
                    else:
                        print("  [NOTICE] Number could not be extracted; SMS skipped.")

                    ring_start = None
                    captured_number = ""
                    print("\nListening for next incoming call...\n")

            # Call disconnected or answered manually
            elif state != "RINGING" and last_state == "RINGING":
                if state == "OFFHOOK":
                    print("  [USER ANSWERED] Call picked up manually. Automation canceled.\n")
                else:
                    print("  [DISCONNECT] Caller hung up.\n")
                ring_start = None
                captured_number = ""

            last_state = state
            time.sleep(0.5)

        except KeyboardInterrupt:
            print("\nExiting AI Call Screener. Goodbye!")
            sys.exit(0)
        except Exception as e:
            time.sleep(1)


if __name__ == "__main__":
    main()
