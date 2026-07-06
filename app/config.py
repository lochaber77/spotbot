"""Configuration loaded from environment variables."""
import os

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

WAHA_URL = os.getenv("WAHA_URL", "http://waha:3000")
WAHA_SESSION = os.getenv("WAHA_SESSION", "default")
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")

# Comma-separated WhatsApp numbers (international format, no '+') allowed to use
# the bot. Everyone else is ignored. This is the privacy + anti-abuse gate.
ALLOWED_NUMBERS = {
    n.strip() for n in os.getenv("ALLOWED_NUMBERS", "").split(",") if n.strip()
}

TZ = os.getenv("TZ", "Europe/London")

# --- Datastore / scheduler (M3) ---
# Data lives on the mounted volume (see Dockerfile's DATA_DIR=/data and the
# app-data volume in docker-compose). SQLite is a single file we can copy on
# cutover; the APScheduler jobstore shares the same file so reminders survive
# restarts.
DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "app.sqlite"))
DB_URL = f"sqlite:///{DB_PATH}"

# --- Google Calendar (M3) ---
# One shared calendar, written via a service account (no per-person OAuth).
# GOOGLE_SERVICE_ACCOUNT_JSON is the path INSIDE the container to the mounted,
# read-only service-account key. Calendar features stay disabled (and the bot
# says so) until both the calendar id and a readable key file are present.
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
CALENDAR_ENABLED = bool(
    GOOGLE_CALENDAR_ID
    and GOOGLE_SERVICE_ACCOUNT_JSON
    and os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON)
)

# Confirm-first: how long a proposed action waits for the user's "yes".
CONFIRMATION_TTL_MINUTES = int(os.getenv("CONFIRMATION_TTL_MINUTES", "30"))
