"""
Direct pipeline runner — reads real Gmail via httpx (bypasses httplib2 timeout),
runs Planner LLM, pushes results to Redis so the dashboard shows live results.

Run:
    python tests/test_pipeline.py
Then open:
    http://localhost:8080

NOTE: This is a standalone runner script, not a pytest suite.  The filename
      intentionally does NOT start with ``test_`` to avoid confusing pytest.
      It lives in tests/ alongside the seed scripts for discoverability.
"""
import asyncio
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone

URGENCY_KEYWORDS = [
    "urgent", "asap", "deadline", "immediately", "critical",
    "overdue", "emergency", "important", "action required",
]


async def fetch_emails_httpx(creds, max_results=10):
    """Fetch unread Gmail messages using httpx (avoids httplib2 timeout issues)."""
    import httpx
    token = creds.token
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        # List message IDs
        r = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={"maxResults": max_results, "q": "is:unread"},
        )
        r.raise_for_status()
        message_ids = [m["id"] for m in r.json().get("messages", [])]

        # Fetch metadata for each
        emails = []
        for mid in message_ids:
            r2 = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}",
                headers=headers,
                params={"format": "metadata"},
            )
            r2.raise_for_status()
            emails.append(r2.json())
        return emails


async def main():
    from config.settings import get_settings
    from memory.redis_client import RedisClient
    from memory.chroma_memory import ChromaMemory
    from openai import AsyncOpenAI

    cfg = get_settings()
    redis = RedisClient.get_instance()
    memory = ChromaMemory.from_settings()
    await memory.seed_default_preferences()

    print("=" * 55)
    print("  PersonalOS Pipeline Test")
    print("=" * 55)

    # ── Step 1: Read Gmail via httpx ──────────────────────────
    print("\n[Step 1] Reading Gmail (httpx)...")
    from mcp_server.google_auth import get_credentials
    creds = await asyncio.to_thread(get_credentials)

    # Refresh token if needed
    if creds.expired:
        from google.auth.transport.requests import Request
        await asyncio.to_thread(creds.refresh, Request())

    raw_messages = await fetch_emails_httpx(creds, max_results=10)
    print(f"  Found {len(raw_messages)} unread emails")

    emails = []
    for full in raw_messages:
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        raw_text = f"{headers.get('From','')} {headers.get('Subject','')} {full.get('snippet','')}".lower()
        keywords = [kw for kw in URGENCY_KEYWORDS if kw in raw_text]
        emails.append({
            "event_id": hashlib.sha256(f"email:{full['id']}".encode()).hexdigest()[:16],
            "type": "email",
            "source": "gmail",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "id": full["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", "(no subject)"),
                "snippet": full.get("snippet", ""),
                "date": headers.get("Date", ""),
                "unread": True,
            },
            "urgency_keywords": keywords,
            "summary": f"Email from {headers.get('From','')}: {headers.get('Subject','')}",
        })
        kw_str = f"  *** URGENT: {keywords}" if keywords else ""
        print(f"  - {headers.get('Subject','')[:55]}{kw_str}")

    # Sort urgent first, limit to 5
    emails.sort(key=lambda e: -len(e["urgency_keywords"]))
    emails = emails[:5]
    print(f"\n  Processing top {len(emails)} emails")

    # ── Step 2: Planner LLM ───────────────────────────────────
    print(f"\n[Step 2] Running Planner LLM ({cfg.openrouter_model})...")
    llm = AsyncOpenAI(base_url=cfg.openrouter_base_url, api_key=cfg.openrouter_api_key)

    SCHEMA = """{
  "action": "send_email|create_event|list_files|move_file|no_action",
  "confidence": <0-100>,
  "priority": "high|medium|low",
  "reason": "<why>",
  "requires_approval": <true|false>,
  "alternatives": [{"action":"<tool>","confidence":<int>,"reason":"<why>"},{"action":"<tool>","confidence":<int>,"reason":"<why>"}],
  "explanation": "<why chosen over alternatives>",
  "action_args": {}
}"""

    plans = []
    for i, event in enumerate(emails):
        print(f"\n  [{i+1}/{len(emails)}] {event['summary'][:60]}")
        try:
            prefs = await memory.query_preferences(event["summary"], n_results=2)
            pref_ctx = "\n".join(f"  - {p['document']}" for p in prefs) if prefs else "None"

            # Retry up to 3 times on 429
            resp = None
            for attempt in range(3):
                try:
                    resp = await llm.chat.completions.create(
                        model=cfg.openrouter_model,
                        messages=[
                            {"role": "system", "content": f"""You are a personal AI assistant that decides what action to take for email events.
Available actions: send_email(to,subject,body), create_event(summary,start_datetime,end_datetime), list_files(directory), move_file(source,destination), no_action
User preferences:\n{pref_ctx}
Priority rules (evaluate from email content, NOT just keywords):
  high   — requires urgent attention: deadline, payment, academic/work issue, urgent reply needed
  medium — useful but not urgent: meeting request, follow-up, moderate importance
  low    — informational or promotional: newsletter, offer, notification, no action needed
Respond ONLY with valid JSON, no markdown, matching this schema:\n{SCHEMA}"""},
                            {"role": "user", "content": f"""Email event:
From: {event['payload']['from']}
Subject: {event['payload']['subject']}
Snippet: {event['payload']['snippet'][:300]}
Urgency keywords found: {event['urgency_keywords']}
Classify priority based on email content. What action should be taken? Respond with JSON only."""}
                        ],
                        temperature=0.2,
                        max_tokens=500,
                    )
                    break
                except Exception as retry_err:
                    if "429" in str(retry_err) and attempt < 2:
                        wait = 15 * (attempt + 1)
                        print(f"  (429 rate limit — retrying in {wait}s...)")
                        await asyncio.sleep(wait)
                    else:
                        raise
            if resp is None:
                raise RuntimeError("All LLM retries failed")
            raw = resp.choices[0].message.content or ""
            cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not match:
                raise ValueError(f"No JSON in response: {raw[:100]}")
            plan = json.loads(match.group(0))

            plan.setdefault("action", "no_action")
            plan.setdefault("confidence", 50)
            plan.setdefault("reason", "")
            plan.setdefault("requires_approval", True)
            plan.setdefault("alternatives", [])
            plan.setdefault("explanation", "")
            plan.setdefault("action_args", {})
            plan["confidence"] = max(0, min(100, int(plan["confidence"])))

            # Score adjustment
            urgency_mult = 1.0 + min(len(event["urgency_keywords"]) * 0.1, 0.3)
            rate = await memory.get_approval_rate(plan["action"])
            history_mult = 0.9 + (rate * 0.2)
            base = plan["confidence"]
            adjusted = max(0, min(100, int(base * urgency_mult * history_mult)))
            plan["confidence"] = adjusted
            plan["id"] = str(uuid.uuid4())
            plan["event_id"] = event["event_id"]
            plan["event_type"] = "email"
            plan["created_at"] = datetime.now(timezone.utc).isoformat()
            plan["subject"] = event["payload"]["subject"]
            plan["from_addr"] = event["payload"]["from"]
            plan["snippet"] = event["payload"].get("snippet", "")[:200]
            plan["urgency_keywords"] = event["urgency_keywords"]
            plan["scoring"] = {
                "base": base,
                "urgency_mult": round(urgency_mult, 2),
                "history_mult": round(history_mult, 2),
            }

            # Priority: use LLM's content-based classification, fallback to confidence-based
            llm_priority = plan.get("priority", "").lower()
            if llm_priority not in ("high", "medium", "low"):
                llm_priority = "high" if adjusted > 90 else "medium" if adjusted >= 70 else "low"
            plan["priority"] = llm_priority

            # User response (will be updated after routing)
            plan["user_response"] = "pending"

            # Call text — what the Twilio agent would say
            action_desc = plan["action"].replace("_", " ")
            plan["call_text"] = (
                f"Hello! This is your Personal OS Agent. "
                f"I detected a {plan['priority']} priority email "
                f"from {event['payload']['from'][:60]}. "
                f"Subject: {event['payload']['subject'][:80]}. "
                f"Confidence level: {adjusted} percent. "
                f"Recommended action: {action_desc}. "
                f"Reason: {plan['reason'][:120]}. "
                + ("Action has been automatically executed." if adjusted > 90
                   else "Please review and approve or reject on your dashboard."
                   if adjusted >= 70 else "Action was silently discarded due to low confidence.")
            )

            plans.append(plan)
            label = "AUTO-EXECUTE >90%" if adjusted > 90 else "DASHBOARD 70-90%" if adjusted >= 70 else "SILENT <70%"
            print(f"  → {label}  action={plan['action']}  confidence={adjusted}%")
            print(f"     reason: {plan['reason'][:80]}")

        except Exception as e:
            print(f"  [ERROR] {e}")

        # Rate limit: 8 req/min on free tier → wait 9s between calls
        if i < len(emails) - 1:
            print("  (waiting 9s for rate limit...)")
            await asyncio.sleep(9)

    # ── Step 3: Route to Redis / Dashboard ───────────────────
    print(f"\n[Step 3] Routing {len(plans)} plans to dashboard...")
    auto = dashboard_count = silent = 0

    for plan in plans:
        c = plan["confidence"]
        await redis.append_activity_log({
            "timestamp": plan["created_at"],
            "agent": "Planner",
            "action": f"Planned: {plan['action']} | {plan['subject'][:40]} | confidence={c}%",
            "plan_id": plan["id"],
        })

        if c > 90:
            plan["user_response"] = "auto_executed"
            await redis.push_approval(plan)
            auto += 1
            print(f"  AUTO     ({c:3d}%): {plan['subject'][:50]}")
        elif c >= 70:
            plan["user_response"] = "pending"
            await redis.push_dashboard_item(plan)
            dashboard_count += 1
            print(f"  DASHBOARD({c:3d}%): {plan['subject'][:50]}")
        else:
            plan["user_response"] = "silent_discarded"
            silent += 1
            print(f"  SILENT   ({c:3d}%): {plan['subject'][:50]}")
            await redis.append_activity_log({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": "Executor",
                "action": f"SILENT DISCARD: {plan['action']} | {plan['subject'][:40]} | confidence={c}%",
            })

        # Store every plan in emails:all regardless of confidence
        await redis.push_email_record(plan)

    print(f"\n{'='*55}")
    print(f"  {auto} auto-execute | {dashboard_count} dashboard | {silent} silent")
    print(f"  Open http://localhost:8080 to see results")
    print(f"{'='*55}")

    await redis.close()


if __name__ == "__main__":
    import sys as _sys
    import os as _os

    _sys.path.insert(0, ".")
    # Windows consoles default to cp1252; reconfigure only when running directly
    if hasattr(_sys.stdout, "reconfigure"):
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # Apply HuggingFace token before any HF library imports
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
    _hf = _os.getenv("HUGGINGFACE_TOKEN") or _os.getenv("HF_TOKEN")
    if _hf:
        _os.environ["HF_TOKEN"] = _hf

    asyncio.run(main())
