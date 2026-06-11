"""
Contract smoke test: the agent's RestWriter against a real Job Radar backend (staging).

Verifies the agent and backend actually agree on the /agent/* contract — auth, payload shapes,
responses. Read-leaning, but it DOES create one inbox row (on staging) to prove the write path.

Credentials from env (never hard-coded):
    JOBRADAR_API_URL    default https://staging.job-radar.net/api
    AGENT_API_KEY       generated in the UI: Settings → Agent Keys

Usage:
    AGENT_API_KEY=... python scripts/smoke_jobradar.py
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.writer_rest import RestWriter  # noqa: E402

BASE = os.environ.get("JOBRADAR_API_URL", "https://staging.job-radar.net/api")
KEY = os.environ.get("AGENT_API_KEY", "")


def main() -> int:
    if not KEY:
        print("✗ set AGENT_API_KEY (Settings → Agent Keys in the UI)")
        return 2

    w = RestWriter(BASE, KEY)
    print(f"→ {BASE} (X-Agent-Key …{KEY[-4:]})")
    try:
        # 1) auth + read: existing reviews for matching
        reviews = w.get_reviews()
        print(f"✓ GET /agent/reviews → {len(reviews)} review(s)")
        if reviews:
            r = reviews[0]
            print(f"   e.g. {r.get('company')} — {r.get('title')} [{r.get('status')}] id={r.get('review_id')}")

        # 2) write: a clearly-marked synthetic inbox entry (proves the write path persists)
        mid = f"<smoke-{uuid.uuid4()}@example.com>"
        resp = w.create_inbox_entry({
            "message_id": mid,
            "subject": "[smoke test] please ignore",
            "sender": "smoke@example.com",
            "received_at": "2026-06-11T20:00:00Z",
            "category": "job_alert",
            "confidence": 0.99,
            "langfuse_trace_id": None,
            "raw_extracted_json": {"smoke": True},
            "postings": [{
                "company": "SmokeTest Co", "role": "Test Engineer",
                "link": "https://example.com/job", "action_required": False,
                "possible_duplicate": False, "matched_review_id": None,
            }],
        })
        print(f"✓ POST /agent/inbox → {resp}")

        # 3) heartbeat
        run = w.report_run({
            "environment": "local", "agent_version": "0.0-smoke", "status": "success",
            "started_at": "2026-06-11T20:00:00Z", "finished_at": "2026-06-11T20:00:05Z",
            "emails_processed": 1, "postings_created": 1, "interactions_recorded": 0,
            "escalations": 0, "retries": 0, "error_summary": None,
        })
        print(f"✓ POST /agent/runs → {run}")
        print("\n✓ contract OK. (Created one synthetic inbox row on staging — delete it from the UI.)")
        return 0
    finally:
        w.close()


if __name__ == "__main__":
    raise SystemExit(main())
