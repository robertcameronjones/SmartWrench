import os
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()
sid = os.environ["TWILIO_ACCOUNT_SID"]
tok = os.environ["TWILIO_AUTH_TOKEN"]
from_num = os.environ["TWILIO_FROM_NUMBER"]
public = os.environ["PUBLIC_BASE_URL"].rstrip("/")
webhook = f"{public}/sms"

c = Client(sid, tok)
nums = c.incoming_phone_numbers.list(phone_number=from_num)
if not nums:
    raise SystemExit(f"No incoming number matching {from_num}")
n = nums[0]
print(f"Found number: {n.phone_number} sid={n.sid}")
print(f"  before: sms_url={n.sms_url!r} sms_method={n.sms_method!r}")
n = n.update(sms_url=webhook, sms_method="POST")
print(f"  after : sms_url={n.sms_url!r} sms_method={n.sms_method!r}")
