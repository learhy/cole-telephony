"""Verify the sole-proprietor brand OTP (code Twilio texts to Dan's cell)
and immediately register the A2P campaign for the Cole gateway number.

Usage: .venv/bin/python verify_otp.py <6-digit-code>
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
cfg = {}
for line in (BASE / ".env").read_text().splitlines():
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()

SID, TOK = cfg["TWILIO_ACCOUNT_SID"], cfg["TWILIO_AUTH_TOKEN"]
MG = cfg["TWILIO_MESSAGING_SERVICE_SID"]
BRAND = "BNd9db28609e16aa48ba3d26abc75e3021"


def api(url, data=None):
    r = urllib.request.Request(url)
    import base64
    r.add_header("Authorization", "Basic " + base64.b64encode(f"{SID}:{TOK}".encode()).decode())
    body = urllib.parse.urlencode(data).encode() if data else None
    try:
        with urllib.request.urlopen(r, data=body, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main():
    if len(sys.argv) != 2 or not sys.argv[1].strip().isdigit():
        print("usage: verify_otp.py <6-digit-code>")
        sys.exit(1)
    code = sys.argv[1].strip()

    st, result = api(
        f"https://messaging.twilio.com/v1/a2p/BrandRegistrations/{BRAND}/SmsOtp",
        {"TwilioSmsVerificationCode": code},
    )
    print("OTP verify:", st, json.dumps(result)[:400])
    if st not in (200, 201):
        sys.exit(2)

    pairs = [
        ("BrandRegistrationSid", BRAND),
        ("UsAppToPersonUsecase", "CUSTOMER_CARE"),
        ("Description", "Personal SMS assistant gateway for the account owner. Relays inbound messages from personal contacts and group threads to the owner and sends brief assistant replies on the owner's behalf."),
        ("MessageFlow", "A person texts the gateway number with a question or update. The message is relayed to the account owner, who may respond through the assistant. Users receive HELP and opt-in/opt-out confirmations."),
        ("MessageSamples", "Hey, are we still on for Saturday? (from a friend, relayed to the owner)"),
        ("MessageSamples", "Cole gateway: relayed your message to Dan. He will get back to you shortly."),
        ("MessageSamples", "Running 10 min late to pickup - Dan"),
        ("MessageSamples", "HELP: This is Dan Rohan's assistant gateway. Reply STOP to opt out."),
        ("HasEmbeddedLinks", "false"),
        ("HasEmbeddedPhone", "false"),
        ("AgeGated", "false"),
        ("DirectLending", "false"),
        ("OptInMessage", "By messaging this number you consent to messages being relayed to the account owner. Message and data rates may apply."),
        ("OptOutMessage", "You have opted out and will no longer receive messages from this number. Reply START to resubscribe."),
        ("HelpMessage", "This is Dan Rohan's assistant gateway. Messages are relayed to Dan. Reply STOP to opt out, START to resume, HELP for this message."),
    ]
    st2, camp = api(f"https://messaging.twilio.com/v1/Services/{MG}/Compliance/Usa2p", pairs)
    print("Campaign create:", st2, json.dumps(camp)[:500])
    if st2 in (200, 201):
        print("\nCampaign SID:", camp.get("sid"), "| status:", camp.get("status"))
        print("Carrier vetting next; outbound will 30034 until it passes.")


if __name__ == "__main__":
    main()
