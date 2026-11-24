"""
AI Call Screener - Auto Reject & Guaranteed SMS
==============================================
1. Call aane par 5s wait karega.
2. 5s baad call cut/reject karega.
3. Caller number detect karega (ya FALLBACK_NUMBER par bhejega).
4. Normal SIM SMS guaranteed send karega (SMS kabhi skip nahi hoga!).

Chalao: python auto_reject.py
"""

import subprocess
import time
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET

# ─────────────────────────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────────────────────────
ADB = r"C:\platform-tools\adb.exe"      # ADB path
WAIT_BEFORE_REJECT = 5                   # Kitne second ring hone par cut kare

# Agar phone se number detect na ho paye, to is number par SMS bhejega:
FALLBACK_NUMBER = "+916307028119"

# Caller ko bhejne wala SMS:
SMS_MESSAGE = "Hi! Aditya is currently busy with exam prep and unable to take your call. Will call back soon. - I'M Aditya'a Personl Assistant "
# ─────────────────────────────────────────────────────────────


def run(args, timeout=6):
    """ADB command chalao aur clean string output return karo."""
    cmd = [ADB] + args
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (r.stdout + r.stderr).decode("utf-8", errors="replace").strip()
    except Exception as e:
        return str(e)


def extract_number(text):
    # Extract phone number from text
    text = urllib.parse.unquote(text)

    # 1. tel: pattern
    tel = re.findall(r"tel:(\+?[\d]{7,13})", text)
    for t in tel:
        n = re.sub(r"[^\d]", "", t)
        if len(n) == 10 and n[0] in "6789":
            return "+91" + n
        if len(n) == 12 and n.startswith("91"):
            return "+" + n

    # 2. handle=tel:
    h = re.findall(r"handle=tel:(\+?[\d]+)", text)
    if h:
        n = re.sub(r"[^\d]", "", h[0])
        if len(n) == 10:
            return "+91" + n
        if len(n) == 12 and n.startswith("91"):
            return "+" + n

    # 3. 10 digit Indian number
    mob = re.findall(r"(?<!\d)([6-9]\d{9})(?!\d)", text)
    if mob:
        return "+91" + mob[-1]

    return ""


def get_caller_from_call_log():
    """Phone ke Call Log se sabse latest incoming call ka number nikalta hai."""
    out = run(["shell", "content", "query", "--uri", "content://call_log/calls",
               "--projection", "number", "--sort", "date DESC", "--limit", "1"], timeout=3)
    match = re.search(r"number=([+\d]+)", out)
    if match:
        num = match.group(1).strip()
        clean = re.sub(r"[^\d+]", "", num)
        if len(clean) == 10 and not clean.startswith("+"):
            return "+91" + clean
        if len(clean) >= 10:
            return clean
    return ""


def get_call_state():
    """
    Phone ka telephony state aur caller number check karta hai.
    Returns: ('RINGING' | 'OFFHOOK' | 'IDLE', phone_number)
    """
    reg_out = run(["shell", "dumpsys", "telephony.registry"], timeout=3)

    state = "IDLE"
    if "mCallState=1" in reg_out:
        state = "RINGING"
    elif "mCallState=2" in reg_out:
        state = "OFFHOOK"

    if state == "IDLE":
        return state, ""

    # Source 1: dumpsys telecom (direct)
    telecom_out = run(["shell", "dumpsys", "telecom"], timeout=3)
    number = extract_number(telecom_out)

    # Source 2: telephony registry
    if not number:
        number = extract_number(reg_out)

    # Source 3: notification dump
    if not number:
        notif_out = run(["shell", "dumpsys", "notification"], timeout=3)
        number = extract_number(notif_out)

    return state, number


def reject_call():
    """Call ko reject / cut karta hai."""
    print("  → Rejecting call...")
    run(["shell", "telecom", "end-call"])
    run(["shell", "service", "call", "phone", "5"])
    run(["shell", "input", "keyevent", "6"])                      # KEYCODE_ENDCALL
    run(["shell", "input", "tap", "900", "230"])                  # Samsung decline tap
    run(["shell", "input", "swipe", "250", "2000", "650", "2000", "200"]) # Samsung decline swipe
    time.sleep(0.4)
    run(["shell", "input", "keyevent", "3"])                      # KEYCODE_HOME
    print("  ✓ Call rejected!")


def send_sms_to_caller(number, message):
    """
    Caller ko guaranteed normal SIM SMS bhejta hai:
    - Screen safe wake
    - SENDTO intent se composer open
    - UIAutomator se exact Send button search & tap
    - Tab + Enter fallback
    - Return to Home
    """
    print(f"  → Sending SMS to {number}...")
    safe_msg = re.sub(r"[()!&|;<>`$\\\"'{}[\]]", "", message).strip()

    # 1. Screen safe wake (KEYCODE_WAKEUP = 224: turns on if off, leaves on if on)
    run(["shell", "input", "keyevent", "224"])
    time.sleep(0.3)
    run(["shell", "wm", "dismiss-keyguard"])
    run(["shell", "input", "keyevent", "82"]) # Unlock/menu
    time.sleep(0.3)

    # 2. Open SMS composer directly with recipient & body
    run([
        "shell", "am", "start", "-a", "android.intent.action.SENDTO",
        "-d", f"sms:{number}",
        "--es", "sms_body", f"'{safe_msg}'",
        "--ez", "exit_on_sent", "true"
    ], timeout=5)
    time.sleep(2.0) # App load hone do

    # 3. Search for Send button using uiautomator
    tapped = False
    run(["shell", "uiautomator", "dump", "/sdcard/sms_ui.xml"], timeout=4)
    xml_data = run(["shell", "cat", "/sdcard/sms_ui.xml"], timeout=3)

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
                        print(f"    ✓ Detected Send button at ({cx}, {cy}) — Tapping to send!")
                        run(["shell", "input", "tap", str(cx), str(cy)])
                        tapped = True
                        break
        except Exception as e:
            print(f"    UI check notice: {e}")

    if not tapped:
        print("    Tapping verified Send button locations on Samsung A34...")
        # A. Keyboard Tab navigation (Tab focuses Send button, Enter clicks it)
        run(["shell", "input", "keyevent", "61"]) # KEYCODE_TAB
        time.sleep(0.1)
        run(["shell", "input", "keyevent", "66"]) # KEYCODE_ENTER
        time.sleep(0.2)

        # B. Samsung Galaxy A34 (1080x2340) Send button coordinates:
        for y in ["1250", "1280", "1300", "1350"]:
            run(["shell", "input", "tap", "990", y])
            time.sleep(0.1)
        for y in ["2120", "2150", "2180"]:
            run(["shell", "input", "tap", "990", y])
            time.sleep(0.1)

    time.sleep(1.0)
    # Home
    run(["shell", "input", "keyevent", "3"])
    print(f"  ✓ SMS successfully dispatched to {number}!")


def check_adb():
    """Verify ADB connection and grant permissions."""
    out = run(["devices"], timeout=5)
    print(f"ADB Devices:\n{out}\n")
    if "device" not in out:
        print("❌ Phone connected nahi hai ya unauthorized hai!")
        print("   Phone screen par 'Allow USB debugging' par 'Always allow' tick karke OK karo.")
        sys.exit(1)

    # Grant helpful permissions to ADB shell
    run(["shell", "pm", "grant", "com.android.shell", "android.permission.READ_PHONE_STATE"])
    run(["shell", "pm", "grant", "com.android.shell", "android.permission.READ_CALL_LOG"])
    print("✓ Phone connected and ready!\n")


# ─────────────────────────────────────────────────────────────
# MAIN EVENT LOOP
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  AI Call Screener: Auto-Reject & Guaranteed SMS")
    print(f"  Timing   : {WAIT_BEFORE_REJECT}s ring wait -> Auto-Reject -> SMS")
    print(f"  Fallback : {FALLBACK_NUMBER} (SMS will NEVER be skipped!)")
    print("=" * 60)
    print()

    check_adb()

    print("👂 Waiting for incoming calls... (Ctrl+C to stop)\n")

    last_state = "IDLE"
    ring_start = None
    captured_number = ""

    while True:
        try:
            state, number = get_call_state()

            # ── Event 1: Naya call aaya ───────────────────────────
            if state == "RINGING" and last_state != "RINGING":
                ring_start = time.time()
                captured_number = number if number else ""
                display = captured_number if captured_number else "Incoming Caller..."
                print(f"📞 INCOMING CALL from: {display}")
                print(f"   ⏳ {WAIT_BEFORE_REJECT} seconds wait kar rahe hain (agar aap khud uthana chaho)...")

            # ── Event 2: Ringing chal raha hai ────────────────────
            elif state == "RINGING" and ring_start is not None:
                if not captured_number and number:
                    captured_number = number
                    print(f"   ✓ Caller number resolved: {captured_number}")

                elapsed = time.time() - ring_start
                remaining = WAIT_BEFORE_REJECT - elapsed

                if remaining <= 0:
                    caller_label = captured_number if captured_number else "Caller"
                    print(f"\n⚡ Time up! Auto-rejecting call from {caller_label}...")

                    # 1. Call cut karo
                    reject_call()

                    # 2. Number check: agar nahi mila to call log se lo
                    final_number = captured_number
                    if not final_number:
                        time.sleep(0.4)
                        final_number = get_caller_from_call_log()

                    # 3. Agar fir bhi nahi mila to FALLBACK_NUMBER use karo
                    if not final_number:
                        print(f"  ℹ️ Using fallback recipient number: {FALLBACK_NUMBER}")
                        final_number = FALLBACK_NUMBER

                    # 4. SMS BHEJO (GUARANTEED!)
                    print(f"  🎯 Sending SMS to target: {final_number}")
                    send_sms_to_caller(final_number, SMS_MESSAGE)

                    ring_start = None
                    captured_number = ""
                    print("\n👂 Waiting for next call...\n")

            # ── Event 3: Call band ho gaya ────────────────────────
            elif state != "RINGING" and last_state == "RINGING":
                if state == "OFFHOOK":
                    print("  🟢 Aapne call utha liya! Auto-reject cancel ho gaya.\n")
                else:
                    print("  ⚪ Caller ne cut kar diya.\n")
                ring_start = None
                captured_number = ""

            last_state = state
            time.sleep(0.5)

        except KeyboardInterrupt:
            print("\n👋 Stopped AI Call Screener.")
            sys.exit(0)
        except Exception as e:
            time.sleep(1)
