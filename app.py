
import os
import re
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")
DB = os.getenv("DATABASE_PATH", "revenue_assistant.db")

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

def classify_email(subject, body):
    text = f"{subject} {body}".lower()
    classification = "General reply"
    status = "Considering"
    objection = None
    next_action = "Review customer reply"

    if any(x in text for x in ["too expensive","more than expected","price","cheaper","discount","budget"]):
        classification = "Price objection"
        status = "Needs you"
        objection = "Price objection"
        next_action = "Respond to price concern"
    elif any(x in text for x in ["go ahead","accept","yes please","happy to proceed","let's proceed"]):
        classification = "Accepted"
        status = "Accepted"
        next_action = "Book start date"
    elif any(x in text for x in ["another company","gone elsewhere","not proceeding","decline","no longer interested"]):
        classification = "Lost"
        status = "Lost"
        next_action = "Record reason lost"
    elif any(x in text for x in ["start date","when can you start","availability","before october","when could you"]):
        classification = "Scheduling question"
        status = "Needs you"
        objection = "Scheduling question"
        next_action = "Confirm start date"
    elif any(x in text for x in ["quote","quotation","estimate"]) and any(x in text for x in ["received","thanks","thank you"]):
        classification = "Quote acknowledged"
        status = "Considering"
        next_action = "Watch / follow up later"
    return classification, status, objection, next_action

def guess_name(email, body):
    local = (email or "customer").split("@")[0]
    candidate = re.sub(r"[._-]+"," ",local).strip().title()
    return candidate if candidate else "New Customer"

def get_metrics():
    c = conn()
    rows = c.execute("SELECT * FROM opportunities").fetchall()
    total = sum(r["quote_value"] or 0 for r in rows)
    needs = sum((r["quote_value"] or 0) for r in rows if r["status"]=="Needs you")
    ready = sum((r["quote_value"] or 0) for r in rows if r["status"]=="Ready to send")
    accepted = sum((r["quote_value"] or 0) for r in rows if r["status"]=="Accepted")
    c.close()
    return {"open": total, "needs": needs, "ready": ready, "accepted": accepted}

@app.route("/")
def dashboard():
    c = conn()
    opps = c.execute("SELECT * FROM opportunities ORDER BY quote_value DESC").fetchall()
    c.close()
    return render_template("index.html", opps=opps, metrics=get_metrics(), money=money)

@app.route("/opportunity/<int:oid>")
def opportunity(oid):
    c = conn()
    opp = c.execute("SELECT * FROM opportunities WHERE id=?", (oid,)).fetchone()
    interactions = c.execute("SELECT * FROM interactions WHERE opportunity_id=? ORDER BY created_at DESC",(oid,)).fetchall()
    c.close()
    if not opp:
        return "Not found",404
    return render_template("opportunity.html", opp=opp, interactions=interactions, money=money)

@app.route("/test-inbox", methods=["GET","POST"])
def test_inbox():
    result = None
    if request.method == "POST":
        sender = request.form.get("sender","").strip()
        subject = request.form.get("subject","").strip()
        body = request.form.get("body","").strip()
        value = float(request.form.get("quote_value") or 0)
        project = request.form.get("project","New enquiry").strip() or "New enquiry"
        name = request.form.get("customer_name","").strip() or guess_name(sender, body)
        classification, status, objection, next_action = classify_email(subject, body)

        c = conn()
        existing = c.execute("SELECT * FROM opportunities WHERE lower(customer_email)=lower(?) ORDER BY id DESC LIMIT 1",(sender,)).fetchone()
        now = datetime.utcnow().isoformat()
        if existing:
            oid = existing["id"]
            c.execute("""UPDATE opportunities SET status=?, objection=?, next_action=?, last_contact=?,
                       quote_value=CASE WHEN ? > 0 THEN ? ELSE quote_value END
                       WHERE id=?""",(status,objection,next_action,now[:10],value,value,oid))
        else:
            cur = c.execute("""INSERT INTO opportunities
            (customer_name,customer_email,project,location,quote_value,status,objection,next_action,last_contact,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (name,sender,project,"",value,status,objection,next_action,now[:10],now))
            oid = cur.lastrowid
        c.execute("""INSERT INTO interactions
        (opportunity_id,direction,channel,subject,body,classification,created_at)
        VALUES(?,?,?,?,?,?,?)""",(oid,"inbound","email",subject,body,classification,now))
        c.commit()
        c.close()
        result = {"classification":classification,"status":status,"next_action":next_action,"opportunity_id":oid}
    return render_template("test_inbox.html", result=result)

@app.route("/api/opportunities")
def api_opportunities():
    c=conn()
    rows=[dict(r) for r in c.execute("SELECT * FROM opportunities ORDER BY id DESC").fetchall()]
    c.close()
    return jsonify(rows)

@app.route("/api/opportunity/<int:oid>/mark-reviewed", methods=["POST"])
def mark_reviewed(oid):
    c=conn()
    c.execute("UPDATE opportunities SET next_action='Reviewed by owner' WHERE id=?",(oid,))
    c.commit(); c.close()
    return jsonify({"ok":True})

@app.route("/health")
def health():
    return jsonify({"status":"ok"})

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","5000")), debug=os.getenv("FLASK_DEBUG")=="1")
else:
    init_db()
