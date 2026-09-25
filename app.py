
import os
import re
import json
import base64
import sqlite3
from datetime import datetime
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

DB = os.getenv("DATABASE_PATH", "revenue_assistant.db")
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = conn()
    c.executescript("""
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
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS interactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        opportunity_id INTEGER,
        direction TEXT NOT NULL,
        channel TEXT NOT NULL,
        subject TEXT,
        body TEXT NOT NULL,
        classification TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(opportunity_id) REFERENCES opportunities(id)
    );
    CREATE TABLE IF NOT EXISTS gmail_connection(
        id INTEGER PRIMARY KEY CHECK (id = 1),
        email TEXT,
        token_json TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS processed_messages(
        gmail_id TEXT PRIMARY KEY,
        opportunity_id INTEGER,
        processed_at TEXT NOT NULL
    );
    """)
    count = c.execute("SELECT COUNT(*) n FROM opportunities").fetchone()["n"]
    if count == 0:
        seed = [
            ("Sarah Smith","sarah@example.com","Patio replacement","Tonbridge",8450,"Needs you","Price objection","Respond to price concern","2026-09-21"),
            ("James Jones","james@example.com","Driveway","Sevenoaks",6750,"Needs you","Scheduling question","Confirm start date","2026-09-19"),
            ("Mark Taylor","mark@example.com","Garden landscaping","Tunbridge Wells",3250,"Needs you",None,"Reply to enquiry","2026-09-24"),
            ("Richard Brown","richard@example.com","Resin driveway","Otford",5600,"Ready to send",None,"Send follow-up","2026-09-22"),
            ("Anita Patel","anita@example.com","Garden redesign","Sevenoaks",7850,"Ready to send",None,"Send follow-up","2026-09-20"),
            ("Tom Clarke","tom@example.com","Patio & fencing","Riverhead",6200,"Considering",None,"Follow up Monday","2026-09-18"),
            ("Lisa Williams","lisa@example.com","Driveway","Kemsing",9400,"Considering",None,"Follow up Tuesday","2026-09-17"),
            ("David Carter","david@example.com","Garden room","Sevenoaks",12800,"Quote sent",None,"Follow up Wednesday","2026-09-16"),
        ]
        c.executemany("""INSERT INTO opportunities
        (customer_name,customer_email,project,location,quote_value,status,objection,next_action,last_contact,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", [x+(datetime.utcnow().isoformat(),) for x in seed])
    c.commit()
    c.close()

def money(v):
    return f"£{v:,.0f}"

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped

def newest_message_text(body):
    text = body or ""
    # Gmail/plain-text replies commonly include quoted history after these markers.
    markers = [
        "\nOn ", "\r\nOn ", "\nFrom:", "\r\nFrom:",
        "\n-----Original Message-----", "\r\n-----Original Message-----"
    ]
    cut = len(text)
    for marker in markers:
        pos = text.find(marker)
        if pos != -1:
            cut = min(cut, pos)
    # Also remove common quoted lines beginning with >
    fresh_lines = [line for line in text[:cut].splitlines() if not line.lstrip().startswith(">")]
    return "\n".join(fresh_lines).strip()

def classify_email(subject, body):
    fresh = newest_message_text(body)
    text = f"{subject} {fresh}".lower()

    classification = "General reply"
    status = "Considering"
    objection = None
    next_action = "Review customer reply"

    accepted = any(x in text for x in ["go ahead","accept","yes please","happy to proceed","let's proceed","we'd like to go ahead","we would like to go ahead"])
    scheduling = any(x in text for x in ["start date","when can you start","availability","before october","in october","start in october","when could you"])
    lost = any(x in text for x in ["another company","gone elsewhere","not proceeding","decline","no longer interested"])
    price = any(x in text for x in ["too expensive","more than expected","price","cheaper","discount","budget"])

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
    elif any(x in text for x in ["quote","quotation","estimate"]) and any(x in text for x in ["received","thanks","thank you"]):
        classification = "Quote acknowledged"
        status = "Considering"
        next_action = "Watch / follow up later"
    return classification, status, objection, next_action

def guess_name(email_addr, body=""):
    local = (email_addr or "customer").split("@")[0]
    candidate = re.sub(r"[._+-]+"," ",local).strip().title()
    return candidate if candidate else "New Customer"

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
    c.close()
    return {"open": total, "needs": needs, "ready": ready, "accepted": accepted}

def ingest_email(sender, subject, body, project="Email enquiry", quote_value=0, name=None, external_id=None):
    classification, status, objection, next_action = classify_email(subject, body)
    name = name or guess_name(sender, body)
    now = datetime.utcnow().isoformat()
    c = conn()
    if external_id:
        seen = c.execute("SELECT gmail_id FROM processed_messages WHERE gmail_id=?", (external_id,)).fetchone()
        if seen:
            c.close()
            return None, "duplicate"

    existing = c.execute("SELECT * FROM opportunities WHERE lower(customer_email)=lower(?) ORDER BY id DESC LIMIT 1",(sender,)).fetchone()
    if existing:
        oid = existing["id"]
        c.execute("""UPDATE opportunities SET status=?, objection=?, next_action=?, last_contact=?,
                   quote_value=CASE WHEN ? > 0 THEN ? ELSE quote_value END
                   WHERE id=?""",(status,objection,next_action,now[:10],quote_value,quote_value,oid))
    else:
        cur = c.execute("""INSERT INTO opportunities
        (customer_name,customer_email,project,location,quote_value,status,objection,next_action,last_contact,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (name,sender,project,"",quote_value,status,objection,next_action,now[:10],now))
        oid = cur.lastrowid
    c.execute("""INSERT INTO interactions
    (opportunity_id,direction,channel,subject,body,classification,created_at)
    VALUES(?,?,?,?,?,?,?)""",(oid,"inbound","email",subject,body,classification,now))
    if external_id:
        c.execute("INSERT INTO processed_messages(gmail_id,opportunity_id,processed_at) VALUES(?,?,?)",(external_id,oid,now))
    c.commit()
    c.close()
    return {"classification":classification,"status":status,"next_action":next_action,"opportunity_id":oid}, "created"

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
        c.execute("UPDATE gmail_connection SET token_json=?, updated_at=? WHERE id=1",
                  (creds.to_json(), datetime.utcnow().isoformat()))
        c.commit(); c.close()
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
    opps = c.execute("SELECT * FROM opportunities ORDER BY quote_value DESC").fetchall()
    c.close()
    return render_template("index.html", opps=opps, metrics=get_metrics(), money=money)

@app.route("/opportunity/<int:oid>")
@login_required
def opportunity(oid):
    c = conn()
    opp = c.execute("SELECT * FROM opportunities WHERE id=?", (oid,)).fetchone()
    interactions = c.execute("SELECT * FROM interactions WHERE opportunity_id=? ORDER BY created_at DESC",(oid,)).fetchall()
    c.close()
    if not opp:
        return "Not found",404
    return render_template("opportunity.html", opp=opp, interactions=interactions, money=money)

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
        result, _ = ingest_email(sender,subject,body,project,value,name)
    return render_template("test_inbox.html", result=result)

@app.route("/gmail")
@login_required
def gmail_page():
    c = conn()
    row = c.execute("SELECT * FROM gmail_connection WHERE id=1").fetchone()
    count = c.execute("SELECT COUNT(*) n FROM processed_messages").fetchone()["n"]
    c.close()
    return render_template("gmail.html", configured=google_configured(), connection=row, processed=count, message=request.args.get("message"))

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
    c.execute("""INSERT INTO gmail_connection(id,email,token_json,updated_at)
                 VALUES(1,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET email=excluded.email, token_json=excluded.token_json, updated_at=excluded.updated_at""",
              (email_addr,creds.to_json(),datetime.utcnow().isoformat()))
    c.commit(); c.close()
    return redirect(url_for("gmail_page", message=f"Connected {email_addr}"))

@app.route("/gmail/disconnect", methods=["POST"])
@login_required
def gmail_disconnect():
    c=conn(); c.execute("DELETE FROM gmail_connection WHERE id=1"); c.commit(); c.close()
    return redirect(url_for("gmail_page", message="Gmail disconnected."))

@app.route("/gmail/sync", methods=["POST"])
@login_required
def gmail_sync():
    service, connected_email = gmail_service()
    if not service:
        return redirect(url_for("gmail_page", message="Connect Gmail first."))
    response = service.users().messages().list(userId="me", q='in:inbox newer_than:14d subject:"[ARA TEST]"', maxResults=25).execute()
    ids = [x["id"] for x in response.get("messages",[])]
    created = duplicates = skipped = 0
    for mid in reversed(ids):
        c = conn()
        already = c.execute("SELECT gmail_id FROM processed_messages WHERE gmail_id=?",(mid,)).fetchone()
        c.close()
        if already:
            duplicates += 1
            continue
        msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
        payload = msg.get("payload",{})
        headers = {h.get("name","").lower():h.get("value","") for h in payload.get("headers",[])}
        display_name, sender = parseaddr(headers.get("from",""))
        if not sender or sender.lower() == (connected_email or "").lower():
            skipped += 1
            continue
        subject = headers.get("subject","(no subject)")
        body = gmail_body(payload) or msg.get("snippet","")
        quote_value = extract_quote_value(subject + " " + body)
        project = subject[:120] if subject else "Email enquiry"
        result, state = ingest_email(sender,subject,body,project,quote_value,display_name or None,external_id=mid)
        if state == "created":
            created += 1
    return redirect(url_for("gmail_page", message=f"Sync complete: {created} new messages ingested, {duplicates} already seen, {skipped} skipped."))

@app.route("/api/opportunities")
@login_required
def api_opportunities():
    c=conn()
    rows=[dict(r) for r in c.execute("SELECT * FROM opportunities ORDER BY id DESC").fetchall()]
    c.close()
    return jsonify(rows)

@app.route("/api/opportunity/<int:oid>/mark-reviewed", methods=["POST"])
@login_required
def mark_reviewed(oid):
    c=conn()
    c.execute("UPDATE opportunities SET next_action='Reviewed by owner' WHERE id=?",(oid,))
    c.commit(); c.close()
    return jsonify({"ok":True})

@app.route("/health")
def health():
    return jsonify({"status":"ok","google_client_id":bool(os.getenv("GOOGLE_CLIENT_ID")),"google_client_secret":bool(os.getenv("GOOGLE_CLIENT_SECRET")),"google_configured":google_configured()})

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","5000")), debug=os.getenv("FLASK_DEBUG")=="1")
else:
    init_db()
