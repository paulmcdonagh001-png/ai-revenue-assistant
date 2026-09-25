
import os
import re
import json
import base64
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from email.utils import parseaddr

from flask import Flask, render_template, request, jsonify, redirect, url_for, session

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("BASE_URL", "").startswith("https://")
)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SQLITE_PATH = os.getenv("DATABASE_PATH", "revenue_assistant.db")
USE_POSTGRES = DATABASE_URL.startswith("postgres")
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

STATUS_VALUES = ["New enquiry", "Considering", "Needs you", "Ready to send", "Accepted", "Lost", "Dormant"]

def utcnow():
    return datetime.now(timezone.utc).isoformat()

class DB:
    def __init__(self):
        if USE_POSTGRES:
            import psycopg
            from psycopg.rows import dict_row
            self.raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            self.raw = sqlite3.connect(SQLITE_PATH)
            self.raw.row_factory = sqlite3.Row

    def _sql(self, sql):
        return sql.replace("?", "%s") if USE_POSTGRES else sql

    def execute(self, sql, params=()):
        return self.raw.execute(self._sql(sql), params)

    def executemany(self, sql, rows):
        return self.raw.executemany(self._sql(sql), rows)

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()

def conn():
    return DB()

def init_db():
    c = conn()
    if USE_POSTGRES:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS opportunities(
                id BIGSERIAL PRIMARY KEY,
                customer_name TEXT NOT NULL,
                customer_email TEXT,
                project TEXT NOT NULL,
                location TEXT,
                quote_value DOUBLE PRECISION DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'New enquiry',
                objection TEXT,
                next_action TEXT,
                last_contact TEXT,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS interactions(
                id BIGSERIAL PRIMARY KEY,
                opportunity_id BIGINT,
                direction TEXT NOT NULL,
                channel TEXT NOT NULL,
                subject TEXT,
                body TEXT NOT NULL,
                classification TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS gmail_connection(
                id INTEGER PRIMARY KEY CHECK (id = 1),
                email TEXT,
                token_json TEXT,
                updated_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS processed_messages(
                gmail_id TEXT PRIMARY KEY,
                opportunity_id BIGINT,
                processed_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS action_items(
                id BIGSERIAL PRIMARY KEY,
                opportunity_id BIGINT NOT NULL,
                interaction_id BIGINT,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                owner TEXT NOT NULL,
                priority TEXT NOT NULL,
                confidence DOUBLE PRECISION DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id),
                FOREIGN KEY(interaction_id) REFERENCES interactions(id)
            )
            """,
            "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS ai_summary TEXT",
            "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS customer_intent TEXT",
            "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION",
            "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS human_required BOOLEAN DEFAULT FALSE",
            "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS analysis_source TEXT",
            "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS latest_text TEXT",
            "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS analysis_json TEXT"
        ]
    else:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS opportunities(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                customer_email TEXT,
                project TEXT NOT NULL,
                location TEXT,
                quote_value REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'New enquiry',
                objection TEXT,
                next_action TEXT,
                last_contact TEXT,
                created_at TEXT NOT NULL,
                ai_summary TEXT,
                customer_intent TEXT,
                confidence REAL,
                human_required INTEGER DEFAULT 0,
                analysis_source TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS interactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER,
                direction TEXT NOT NULL,
                channel TEXT NOT NULL,
                subject TEXT,
                body TEXT NOT NULL,
                classification TEXT,
                created_at TEXT NOT NULL,
                latest_text TEXT,
                analysis_json TEXT,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS gmail_connection(
                id INTEGER PRIMARY KEY CHECK (id = 1),
                email TEXT,
                token_json TEXT,
                updated_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS processed_messages(
                gmail_id TEXT PRIMARY KEY,
                opportunity_id INTEGER,
                processed_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS action_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER NOT NULL,
                interaction_id INTEGER,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                owner TEXT NOT NULL,
                priority TEXT NOT NULL,
                confidence REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id),
                FOREIGN KEY(interaction_id) REFERENCES interactions(id)
            )
            """
        ]
    for statement in statements:
        try:
            c.execute(statement)
        except Exception:
            if USE_POSTGRES:
                raise
    c.commit()
    c.close()

def money(v):
    return f"£{(v or 0):,.0f}"

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped

def newest_message_text(body):
    text = body or ""
    markers = [
        "\nOn ", "\r\nOn ", "\nFrom:", "\r\nFrom:",
        "\n-----Original Message-----", "\r\n-----Original Message-----"
    ]
    cut = len(text)
    for marker in markers:
        pos = text.find(marker)
        if pos != -1:
            cut = min(cut, pos)
    fresh_lines = [line for line in text[:cut].splitlines() if not line.lstrip().startswith(">")]
    return "\n".join(fresh_lines).strip()

def classify_email(subject, body):
    fresh = newest_message_text(body)
    text = f"{subject} {fresh}".lower()
    classification = "General reply"
    status = "Considering"
    objection = None
    next_action = "Review customer reply"

    accepted = any(x in text for x in [
        "go ahead", "accept", "yes please", "happy to proceed",
        "let's proceed", "we'd like to go ahead", "we would like to go ahead",
        "definitely go ahead"
    ])
    scheduling = any(x in text for x in [
        "start date", "when can you start", "availability", "before october",
        "in october", "start in october", "when could you"
    ])
    lost = any(x in text for x in [
        "another company", "gone elsewhere", "not proceeding",
        "decline", "no longer interested"
    ])
    price = any(x in text for x in [
        "too expensive", "more than expected", "price",
        "cheaper", "discount", "budget"
    ])

    if accepted:
        classification = "Accepted"
        status = "Accepted"
        if scheduling:
            next_action = "Confirm start date"
            objection = "Scheduling question"
        else:
            next_action = "Book start date"
    elif lost:
        classification = "Lost"
        status = "Lost"
        next_action = "Record reason lost"
    elif scheduling:
        classification = "Scheduling question"
        status = "Needs you"
        objection = "Scheduling question"
        next_action = "Confirm start date"
    elif price:
        classification = "Price objection"
        status = "Needs you"
        objection = "Price objection"
        next_action = "Respond to price concern"
    return classification, status, objection, next_action

def rules_analysis(subject, body):
    classification, status, objection, next_action = classify_email(subject, body)
    category = "other"
    if objection == "Price objection":
        category = "price"
    elif objection == "Scheduling question":
        category = "schedule"
    return {
        "summary": classification,
        "customer_intent": "accepted" if status == "Accepted" else ("declined" if status == "Lost" else "unclear"),
        "opportunity_status": status,
        "confidence": 0.55,
        "human_required": status == "Needs you",
        "primary_objection": category if category != "other" else "none",
        "next_action": next_action,
        "sentiment": "neutral",
        "questions": [],
        "dependencies": [],
        "actions": [{
            "description": next_action,
            "owner": "business",
            "priority": "high" if status == "Needs you" else "medium",
            "category": category,
            "confidence": 0.55
        }],
        "commercial_signals": [classification],
        "analysis_source": "rules"
    }

def openai_configured():
    return bool(os.getenv("OPENAI_API_KEY"))

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "customer_intent": {
            "type": "string",
            "enum": ["new_enquiry","considering","conditional_interest","wants_to_proceed","accepted","declined","deferred","unclear"]
        },
        "opportunity_status": {
            "type": "string",
            "enum": STATUS_VALUES
        },
        "confidence": {"type": "number"},
        "human_required": {"type": "boolean"},
        "primary_objection": {
            "type": "string",
            "enum": ["none","price","schedule","technical","scope","payment","access","competitor","trust","other"]
        },
        "next_action": {"type": "string"},
        "sentiment": {
            "type": "string",
            "enum": ["positive","neutral","concerned","frustrated","negative"]
        },
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "category": {"type": "string", "enum": ["price","schedule","technical","scope","payment","access","other"]},
                    "requires_human": {"type": "boolean"},
                    "confidence": {"type": "number"}
                },
                "required": ["question","category","requires_human","confidence"],
                "additionalProperties": False
            }
        },
        "dependencies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "owner": {"type": "string", "enum": ["customer","business","third_party","unknown"]},
                    "confidence": {"type": "number"}
                },
                "required": ["description","owner","confidence"],
                "additionalProperties": False
            }
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "owner": {"type": "string", "enum": ["business","customer","system"]},
                    "priority": {"type": "string", "enum": ["high","medium","low"]},
                    "category": {"type": "string", "enum": ["price","schedule","technical","scope","payment","access","follow_up","other"]},
                    "confidence": {"type": "number"}
                },
                "required": ["description","owner","priority","category","confidence"],
                "additionalProperties": False
            }
        },
        "commercial_signals": {
            "type": "array",
            "items": {"type": "string"}
        }
    },
    "required": [
        "summary","customer_intent","opportunity_status","confidence","human_required",
        "primary_objection","next_action","sentiment","questions","dependencies",
        "actions","commercial_signals"
    ],
    "additionalProperties": False
}

def get_recent_history(opportunity_id, limit=6):
    if not opportunity_id:
        return []
    c = conn()
    rows = c.execute(
        "SELECT created_at, classification, body FROM interactions WHERE opportunity_id=? ORDER BY created_at DESC LIMIT ?",
        (opportunity_id, limit)
    ).fetchall()
    c.close()
    result = []
    for row in reversed(rows):
        result.append({
            "date": row["created_at"],
            "classification": row["classification"] or "",
            "message": newest_message_text(row["body"])[:1200]
        })
    return result

def analyze_conversation(existing, subject, body, project, quote_value):
    fallback = rules_analysis(subject, body)
    if not openai_configured():
        fallback["analysis_source"] = "rules_no_api_key"
        return fallback

    latest = newest_message_text(body)
    history = get_recent_history(existing["id"] if existing else None)
    opportunity_context = {
        "customer_name": existing["customer_name"] if existing else "",
        "project": existing["project"] if existing else project,
        "quote_value_gbp": existing["quote_value"] if existing and existing["quote_value"] else quote_value,
        "current_status": existing["status"] if existing else "New enquiry",
        "current_next_action": existing["next_action"] if existing else ""
    }

    system_prompt = """You are the Conversation Analyst for a UK trade/home-improvement business.
Your job is to extract commercial meaning accurately, not persuade the customer.

Rules:
- The field latest_message is the customer's newest text and is authoritative for their current intent.
- conversation_history is context only. Do not mistake older quoted concerns for the current message.
- Extract every distinct question, objection, dependency and required action. A single email can contain many.
- Never invent a price, date, technical answer, promise, discount or commitment.
- Mark Accepted only when the latest message explicitly or unambiguously commits to proceeding.
- Positive interest without commitment is not Accepted.
- If a customer explicitly accepts but also asks unresolved questions, Accepted can still be correct and human_required should be true when a human must answer.
- If uncertainty is material, choose Needs you and lower confidence.
- Business-owned price, scheduling and technical decisions normally require a human.
- Use concise summaries and action descriptions.
"""

    payload = {
        "opportunity": opportunity_context,
        "conversation_history": history,
        "latest_message": {
            "subject": subject,
            "body": latest
        }
    }

    try:
        from openai import OpenAI
        client = OpenAI()
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "trade_conversation_analysis",
                    "strict": True,
                    "schema": ANALYSIS_SCHEMA
                }
            },
            max_output_tokens=1800
        )
        analysis = json.loads(response.output_text)
        analysis["analysis_source"] = f"openai:{OPENAI_MODEL}"

        confidence = float(analysis.get("confidence", 0))
        if confidence < 0.65 and analysis.get("opportunity_status") not in ["Accepted", "Lost"]:
            analysis["opportunity_status"] = "Needs you"
            analysis["human_required"] = True
            if not analysis.get("next_action"):
                analysis["next_action"] = "Review customer message"

        return analysis
    except Exception as exc:
        app.logger.error("AI analyst call failed: %s", type(exc).__name__)
        fallback["analysis_source"] = "rules_fallback"
        fallback["summary"] = f"{fallback['summary']} (AI fallback)"
        return fallback

def guess_name(email_addr, body=""):
    local = (email_addr or "customer").split("@")[0]
    candidate = re.sub(r"[._+-]+"," ",local).strip().title()
    return candidate if candidate else "New Customer"

def clean_test_subject(subject):
    cleaned = re.sub(r"^\s*\[ARA TEST\]\s*", "", subject or "", flags=re.I).strip()
    cleaned = re.sub(r"^(re|fw|fwd):\s*", "", cleaned, flags=re.I).strip()
    return cleaned or "Email enquiry"

def extract_quote_value(text):
    matches = re.findall(r"£\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)", text or "")
    if not matches:
        return 0
    try:
        return float(matches[-1].replace(",",""))
    except ValueError:
        return 0

def get_metrics():
    c = conn()
    rows = c.execute("SELECT * FROM opportunities").fetchall()
    total = sum(r["quote_value"] or 0 for r in rows)
    needs = sum((r["quote_value"] or 0) for r in rows if r["status"]=="Needs you")
    ready = sum((r["quote_value"] or 0) for r in rows if r["status"]=="Ready to send")
    accepted = sum((r["quote_value"] or 0) for r in rows if r["status"]=="Accepted")
    open_actions = c.execute("SELECT COUNT(*) n FROM action_items WHERE status='open' AND owner='business'").fetchone()["n"]
    c.close()
    return {"open": total, "needs": needs, "ready": ready, "accepted": accepted, "open_actions": open_actions}

def ingest_email(sender, subject, body, project="Email enquiry", quote_value=0, name=None, external_id=None):
    name = name or guess_name(sender, body)
    now = utcnow()
    c = conn()

    if external_id:
        seen = c.execute("SELECT gmail_id FROM processed_messages WHERE gmail_id=?", (external_id,)).fetchone()
        if seen:
            c.close()
            return None, "duplicate"

    existing = c.execute(
        "SELECT * FROM opportunities WHERE lower(customer_email)=lower(?) ORDER BY id DESC LIMIT 1",
        (sender,)
    ).fetchone()
    c.close()

    analysis = analyze_conversation(existing, subject, body, project, quote_value)
    status = analysis["opportunity_status"]
    objection = None if analysis["primary_objection"] == "none" else analysis["primary_objection"].replace("_"," ").title()
    next_action = analysis["next_action"]
    classification = analysis["summary"][:180]
    latest_text = newest_message_text(body)

    c = conn()
    if existing:
        oid = existing["id"]
        c.execute(
            """UPDATE opportunities
               SET status=?, objection=?, next_action=?, last_contact=?,
                   quote_value=CASE WHEN ? > 0 THEN ? ELSE quote_value END,
                   project=CASE WHEN project='Email enquiry' OR project LIKE '[ARA TEST]%' THEN ? ELSE project END,
                   ai_summary=?, customer_intent=?, confidence=?, human_required=?, analysis_source=?
               WHERE id=?""",
            (
                status, objection, next_action, now[:10],
                quote_value, quote_value, project,
                analysis["summary"], analysis["customer_intent"], float(analysis["confidence"]),
                bool(analysis["human_required"]), analysis["analysis_source"], oid
            )
        )
    else:
        cur = c.execute(
            """INSERT INTO opportunities
               (customer_name,customer_email,project,location,quote_value,status,objection,next_action,last_contact,created_at,
                ai_summary,customer_intent,confidence,human_required,analysis_source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               RETURNING id""",
            (
                name, sender, project, "", quote_value, status, objection, next_action, now[:10], now,
                analysis["summary"], analysis["customer_intent"], float(analysis["confidence"]),
                bool(analysis["human_required"]), analysis["analysis_source"]
            )
        )
        oid = cur.fetchone()["id"]

    cur = c.execute(
        """INSERT INTO interactions
           (opportunity_id,direction,channel,subject,body,classification,created_at,latest_text,analysis_json)
           VALUES(?,?,?,?,?,?,?,?,?)
           RETURNING id""",
        (
            oid, "inbound", "email", subject, body, classification, now,
            latest_text, json.dumps(analysis, ensure_ascii=False)
        )
    )
    interaction_id = cur.fetchone()["id"]

    for item in analysis.get("actions", []):
        c.execute(
            """INSERT INTO action_items
               (opportunity_id,interaction_id,category,description,owner,priority,confidence,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                oid, interaction_id, item["category"], item["description"],
                item["owner"], item["priority"], float(item["confidence"]), "open", now
            )
        )

    if external_id:
        c.execute(
            "INSERT INTO processed_messages(gmail_id,opportunity_id,processed_at) VALUES(?,?,?)",
            (external_id, oid, now)
        )

    c.commit()
    c.close()
    return {
        "classification": classification,
        "status": status,
        "next_action": next_action,
        "opportunity_id": oid,
        "analysis": analysis
    }, "created"


def run_ai_self_test():
    if os.getenv("AI_SELF_TEST_ON_START") != "1":
        return
    if not openai_configured():
        app.logger.error("AI_SELF_TEST_FAILED type=MissingAPIKey")
        return
    try:
        sample = """Thanks again for coming round last week. We really like the design and we're keen to get it done. It is a bit more than we originally budgeted though. Would there be any saving if we left out the raised beds? Also, could you start around the middle of October, and does the quote include taking the old paving away? We need to check with our neighbour about access, but assuming that's okay we'd like to move forward."""
        analysis = analyze_conversation(
            None,
            "Patio quotation",
            sample,
            "Patio quotation",
            8450
        )
        app.logger.warning(
            "AI_SELF_TEST_OK source=%s status=%s intent=%s confidence=%.2f questions=%s actions=%s dependencies=%s human_required=%s summary=%s",
            analysis.get("analysis_source"),
            analysis.get("opportunity_status"),
            analysis.get("customer_intent"),
            float(analysis.get("confidence", 0)),
            len(analysis.get("questions", [])),
            len(analysis.get("actions", [])),
            len(analysis.get("dependencies", [])),
            analysis.get("human_required"),
            (analysis.get("summary") or "")[:220]
        )
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        code = None
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            code = body.get("code")
            if not code and isinstance(body.get("error"), dict):
                code = body["error"].get("code")
        app.logger.error(
            "AI_SELF_TEST_FAILED type=%s status=%s code=%s",
            type(exc).__name__, status, code
        )

def google_configured():
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))

def gmail_flow(state=None, code_verifier=None):
    from google_auth_oauthlib.flow import Flow
    config = {
        "web": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"{BASE_URL}/gmail/callback"]
        }
    }
    flow = Flow.from_client_config(
        config,
        scopes=GMAIL_SCOPES,
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=(code_verifier is None)
    )
    flow.redirect_uri = f"{BASE_URL}/gmail/callback"
    return flow

def gmail_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest
    from googleapiclient.discovery import build

    c = conn()
    row = c.execute("SELECT * FROM gmail_connection WHERE id=1").fetchone()
    c.close()
    if not row or not row["token_json"]:
        return None, None

    info = json.loads(row["token_json"])
    creds = Credentials.from_authorized_user_info(info, GMAIL_SCOPES)

    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        c = conn()
        c.execute(
            "UPDATE gmail_connection SET token_json=?, updated_at=? WHERE id=1",
            (creds.to_json(), utcnow())
        )
        c.commit()
        c.close()

    return build("gmail","v1",credentials=creds,cache_discovery=False), row["email"]

def decode_part(data):
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")
    except Exception:
        return ""

def gmail_body(payload):
    mime = payload.get("mimeType","")
    body = payload.get("body",{}).get("data")
    if body and mime in ("text/plain","text/html"):
        text = decode_part(body)
        if mime == "text/html":
            text = re.sub(r"<[^>]+>"," ",text)
        return re.sub(r"\s+"," ",text).strip()
    for part in payload.get("parts",[]) or []:
        text = gmail_body(part)
        if text:
            return text
    return ""

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if ADMIN_PASSWORD and request.form.get("password","") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        return render_template("login.html", error="Incorrect password.")
    return render_template("login.html", error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def dashboard():
    c = conn()
    opps = c.execute("SELECT * FROM opportunities ORDER BY quote_value DESC, id DESC").fetchall()
    c.close()
    return render_template("index.html", opps=opps, metrics=get_metrics(), money=money)

@app.route("/opportunity/<int:oid>")
@login_required
def opportunity(oid):
    c = conn()
    opp = c.execute("SELECT * FROM opportunities WHERE id=?", (oid,)).fetchone()
    interactions = c.execute(
        "SELECT * FROM interactions WHERE opportunity_id=? ORDER BY created_at DESC",
        (oid,)
    ).fetchall()
    actions = c.execute(
        "SELECT * FROM action_items WHERE opportunity_id=? AND status='open' ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, id DESC",
        (oid,)
    ).fetchall()
    c.close()
    if not opp:
        return "Not found", 404

    latest_analysis = None
    if interactions and interactions[0]["analysis_json"]:
        try:
            latest_analysis = json.loads(interactions[0]["analysis_json"])
        except Exception:
            latest_analysis = None

    return render_template(
        "opportunity.html",
        opp=opp,
        interactions=interactions,
        actions=actions,
        latest_analysis=latest_analysis,
        money=money
    )

@app.route("/analyst-lab", methods=["GET","POST"])
@login_required
def analyst_lab():
    result = None
    sample = """Thanks again for coming round last week. We really like the design and we're keen to get it done. It is a bit more than we originally budgeted though. Would there be any saving if we left out the raised beds? Also, could you start around the middle of October, and does the quote include taking the old paving away? We need to check with our neighbour about access, but assuming that's okay we'd like to move forward."""
    subject = request.form.get("subject", "Patio quotation") if request.method == "POST" else "Patio quotation"
    body = request.form.get("body", sample) if request.method == "POST" else sample
    quote_value = float(request.form.get("quote_value") or 8450) if request.method == "POST" else 8450
    if request.method == "POST":
        result = analyze_conversation(None, subject, body, subject, quote_value)
    return render_template(
        "analyst_lab.html",
        result=result,
        subject=subject,
        body=body,
        quote_value=quote_value,
        ai_ready=openai_configured(),
        model=OPENAI_MODEL
    )

@app.route("/test-inbox", methods=["GET","POST"])
@login_required
def test_inbox():
    result = None
    if request.method == "POST":
        sender = request.form.get("sender","").strip()
        subject = request.form.get("subject","").strip()
        body = request.form.get("body","").strip()
        value = float(request.form.get("quote_value") or 0)
        project = request.form.get("project","New enquiry").strip() or "New enquiry"
        name = request.form.get("customer_name","").strip() or guess_name(sender, body)
        result, _ = ingest_email(sender, subject, body, project, value, name)
    return render_template("test_inbox.html", result=result)

@app.route("/gmail")
@login_required
def gmail_page():
    c = conn()
    row = c.execute("SELECT * FROM gmail_connection WHERE id=1").fetchone()
    count = c.execute("SELECT COUNT(*) n FROM processed_messages").fetchone()["n"]
    c.close()
    return render_template(
        "gmail.html",
        configured=google_configured(),
        connection=row,
        processed=count,
        message=request.args.get("message"),
        ai_ready=openai_configured(),
        model=OPENAI_MODEL
    )

@app.route("/gmail/connect")
@login_required
def gmail_connect():
    if not google_configured():
        return redirect(url_for("gmail_page", message="Google OAuth is not configured yet."))
    flow = gmail_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )
    session["oauth_state"] = state
    session["oauth_code_verifier"] = flow.code_verifier
    return redirect(auth_url)

@app.route("/gmail/callback")
@login_required
def gmail_callback():
    if not google_configured():
        return redirect(url_for("gmail_page", message="Google OAuth is not configured."))

    flow = gmail_flow(
        session.get("oauth_state"),
        session.pop("oauth_code_verifier", None)
    )
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials

    from googleapiclient.discovery import build
    service = build("gmail","v1",credentials=creds,cache_discovery=False)
    email_addr = service.users().getProfile(userId="me").execute().get("emailAddress","")

    c = conn()
    c.execute(
        """INSERT INTO gmail_connection(id,email,token_json,updated_at)
           VALUES(1,?,?,?)
           ON CONFLICT(id) DO UPDATE
           SET email=excluded.email, token_json=excluded.token_json, updated_at=excluded.updated_at""",
        (email_addr, creds.to_json(), utcnow())
    )
    c.commit()
    c.close()
    return redirect(url_for("gmail_page", message=f"Connected {email_addr}"))

@app.route("/gmail/disconnect", methods=["POST"])
@login_required
def gmail_disconnect():
    c = conn()
    c.execute("DELETE FROM gmail_connection WHERE id=1")
    c.commit()
    c.close()
    return redirect(url_for("gmail_page", message="Gmail disconnected."))

@app.route("/gmail/sync", methods=["POST"])
@login_required
def gmail_sync():
    service, connected_email = gmail_service()
    if not service:
        return redirect(url_for("gmail_page", message="Connect Gmail first."))

    response = service.users().messages().list(
        userId="me",
        q='in:inbox newer_than:14d subject:"[ARA TEST]"',
        maxResults=25
    ).execute()

    ids = [x["id"] for x in response.get("messages",[])]
    created = duplicates = skipped = 0

    for mid in reversed(ids):
        c = conn()
        already = c.execute(
            "SELECT gmail_id FROM processed_messages WHERE gmail_id=?",
            (mid,)
        ).fetchone()
        c.close()

        if already:
            duplicates += 1
            continue

        msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
        payload = msg.get("payload",{})
        headers = {h.get("name","").lower(): h.get("value","") for h in payload.get("headers",[])}
        display_name, sender = parseaddr(headers.get("from",""))

        if not sender or sender.lower() == (connected_email or "").lower():
            skipped += 1
            continue

        subject = headers.get("subject","(no subject)")
        body = gmail_body(payload) or msg.get("snippet","")
        quote_value = extract_quote_value(subject + " " + body)
        project = clean_test_subject(subject)[:120]

        result, state = ingest_email(
            sender,
            subject,
            body,
            project,
            quote_value,
            display_name or None,
            external_id=mid
        )
        if state == "created":
            created += 1

    return redirect(url_for(
        "gmail_page",
        message=f"Sync complete: {created} new messages ingested, {duplicates} already seen, {skipped} skipped."
    ))

@app.route("/api/opportunities")
@login_required
def api_opportunities():
    c = conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM opportunities ORDER BY id DESC").fetchall()]
    c.close()
    return jsonify(rows)

@app.route("/api/opportunity/<int:oid>/mark-reviewed", methods=["POST"])
@login_required
def mark_reviewed(oid):
    c = conn()
    c.execute("UPDATE opportunities SET next_action='Reviewed by owner' WHERE id=?", (oid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "database": "postgres" if USE_POSTGRES else "sqlite",
        "google_configured": google_configured(),
        "ai_configured": openai_configured(),
        "ai_model": OPENAI_MODEL
    })

if __name__ == "__main__":
    init_db()
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT","5000")),
        debug=os.getenv("FLASK_DEBUG")=="1"
    )
else:
    init_db()
    run_ai_self_test()
