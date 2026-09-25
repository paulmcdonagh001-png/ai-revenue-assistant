# AI Revenue Assistant V1

This is the first backend-driven prototype.

## What is real now
- Flask backend
- SQLite database
- Opportunities persist while the app process remains running
- Test Inbox ingests a simulated customer email
- Rule-based classifier detects:
  - price objections
  - scheduling questions
  - quote acceptance
  - lost quotes
  - general consideration
- Dashboard metrics come from the database
- Individual opportunity records display ingested interactions

## What is deliberately not connected yet
- Gmail OAuth
- WhatsApp
- Google Calendar
- live customer sending
- production database

## Run locally
```bash
pip install -r requirements.txt
python app.py
```
Then visit http://localhost:5000

## Deploy
The repository is Render-ready using `render.yaml`.

Before a real pilot, replace SQLite with Render Postgres and add Gmail OAuth.
