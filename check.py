"""
PersonalOS Agent — Pre-flight API Health Checker
=================================================
Run this BEFORE main.py to verify every service is reachable and configured.

Usage:
  python check.py           # check all services
  python check.py --fix     # auto-fix what can be fixed (e.g. trigger OAuth flow)

Exit codes:
  0 — all checks passed
  1 — one or more checks failed
"""
import asyncio
import os
import sys
import time
from pathlib import Path

# Reconfigure stdout/stderr to UTF-8 so Unicode box-drawing characters
# (═, →, etc.) don't crash on Windows with cp1252 default encoding.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── ANSI colors (Windows-safe via colorama) ──────────────────────────────────
try:
    import colorama
    colorama.init(autoreset=True)
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"
except ImportError:
    GREEN = RED = YELLOW = CYAN = BOLD = RESET = ""

PASS  = f"{GREEN}[PASS]{RESET}"
FAIL  = f"{RED}[FAIL]{RESET}"
WARN  = f"{YELLOW}[WARN]{RESET}"
INFO  = f"{CYAN}[INFO]{RESET}"

results: list[tuple[str, bool, str]] = []  # (check_name, passed, detail)


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    icon = PASS if passed else FAIL
    line = f"  {icon}  {name}"
    if detail:
        line += f"  →  {detail}"
    print(line)


def section(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'─' * 50}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 50}{RESET}")


# ── 1. Environment & .env ────────────────────────────────────────────────────

def check_env() -> None:
    section("1. Environment")

    # Python version
    major, minor = sys.version_info[:2]
    ok = major == 3 and minor >= 11
    record("Python 3.11+", ok, f"found {major}.{minor}")

    # .env file exists
    env_path = Path(".env")
    record(".env file exists", env_path.exists(), str(env_path.resolve()))

    if not env_path.exists():
        print(f"  {WARN}  Run: cp .env.example .env  and fill in your keys")
        return

    # Load settings
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from config.settings import get_settings
        cfg = get_settings()

        record("OPENROUTER_API_KEY set",
               bool(cfg.openrouter_api_key and not cfg.openrouter_api_key.startswith("sk-or-v1-...")),
               cfg.openrouter_api_key[:20] + "..." if cfg.openrouter_api_key else "MISSING")

        record("GOOGLE_CREDENTIALS_PATH set",
               bool(cfg.google_credentials_path),
               cfg.google_credentials_path)

        record("credentials.json exists",
               Path(cfg.google_credentials_path).exists(),
               str(Path(cfg.google_credentials_path).resolve()))

        record("token.json exists (OAuth done?)",
               Path(cfg.google_token_path).exists(),
               str(Path(cfg.google_token_path).resolve()) if Path(cfg.google_token_path).exists()
               else "Run main.py once to trigger OAuth browser flow")

        record("Twilio configured",
               cfg.twilio_enabled,
               f"from={cfg.twilio_from_number} to={cfg.twilio_to_number}"
               if cfg.twilio_enabled else "SIMULATION MODE — set TWILIO_* vars to enable real calls")

    except Exception as e:
        record("Settings loaded", False, str(e))


# ── 2. Redis ─────────────────────────────────────────────────────────────────

async def check_redis() -> None:
    section("2. Redis")
    try:
        import redis.asyncio as aioredis
        from config.settings import get_settings
        cfg = get_settings()

        r = aioredis.from_url(cfg.redis_url, socket_connect_timeout=3)
        t0 = time.perf_counter()
        pong = await r.ping()
        latency_ms = (time.perf_counter() - t0) * 1000
        await r.aclose()

        record("Redis reachable", bool(pong), f"{latency_ms:.1f}ms latency  ({cfg.redis_url})")

        # Check queue lengths
        r2 = aioredis.from_url(cfg.redis_url, encoding="utf-8", decode_responses=True)
        eq = await r2.llen("events:queue")
        aq = await r2.llen("approvals:pending")
        dp = await r2.hlen("dashboard:pending")
        await r2.aclose()
        record("Queue status",
               True,
               f"events:queue={eq}  approvals:pending={aq}  dashboard:pending={dp}")

    except Exception as e:
        record("Redis reachable", False, f"{e}  →  Start with: redis-server")


# ── 3. OpenRouter / LLM ──────────────────────────────────────────────────────

async def check_openrouter() -> None:
    section("3. OpenRouter LLM")
    try:
        import httpx
        from config.settings import get_settings
        cfg = get_settings()

        if not cfg.openrouter_api_key:
            record("OpenRouter API key", False, "MISSING — set OPENROUTER_API_KEY in .env")
            return

        # Use /models (free, no rate limit) instead of a completion call
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{cfg.openrouter_base_url}/models",
                headers={"Authorization": f"Bearer {cfg.openrouter_api_key}"},
            )
        latency_ms = (time.perf_counter() - t0) * 1000

        if r.status_code == 200:
            models = r.json().get("data", [])
            ids = [m["id"] for m in models]
            model_valid = cfg.openrouter_model in ids
            record("OpenRouter API key valid", True,
                   f"latency={latency_ms:.0f}ms  models available={len(ids)}")
            record(f"Model '{cfg.openrouter_model}' exists", model_valid,
                   "OK" if model_valid else
                   f"NOT FOUND — available free: {[i for i in ids if ':free' in i][:3]}")
        else:
            record("OpenRouter API key valid", False,
                   f"HTTP {r.status_code} — check OPENROUTER_API_KEY")

    except Exception as e:
        record("OpenRouter reachable", False, str(e)[:120])


# ── 4. Google APIs ────────────────────────────────────────────────────────────

async def check_google() -> None:
    section("4. Google APIs (Gmail + Calendar)")
    try:
        from config.settings import get_settings
        cfg = get_settings()

        creds_path = Path(cfg.google_credentials_path)
        if not creds_path.exists():
            record("credentials.json", False,
                   "File not found — download from Google Cloud Console → APIs & Services → Credentials")
            return

        # Validate credentials.json structure
        import json
        with open(creds_path) as f:
            creds_data = json.load(f)
        client_id = creds_data.get("installed", {}).get("client_id", "")
        record("credentials.json valid", bool(client_id),
               f"client_id={client_id[:30]}..." if client_id else "Invalid structure")

        # Check token.json
        token_path = Path(cfg.google_token_path)
        if not token_path.exists():
            record("token.json (OAuth)", False,
                   "Not found — run 'python main.py' once to complete OAuth browser flow")
            return

        # Try loading and refreshing the token
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from mcp_server.google_auth import SCOPES

        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            record("token.json (OAuth)", True, "Expired — auto-refreshed successfully")
        else:
            record("token.json (OAuth)", creds.valid,
                   "Valid" if creds.valid else "Invalid — delete token.json and re-run OAuth")

        if not creds.valid:
            return

        # Test Gmail API
        try:
            from googleapiclient.discovery import build
            t0 = time.perf_counter()
            gmail = build("gmail", "v1", credentials=creds)
            profile = gmail.users().getProfile(userId="me").execute()
            latency_ms = (time.perf_counter() - t0) * 1000
            record("Gmail API", True,
                   f"account={profile.get('emailAddress')}  latency={latency_ms:.0f}ms")
        except Exception as e:
            record("Gmail API", False, str(e)[:100])

        # Test Calendar API
        try:
            from googleapiclient.discovery import build
            from datetime import datetime, timezone
            t0 = time.perf_counter()
            cal = build("calendar", "v3", credentials=creds)
            cal_list = cal.calendarList().list(maxResults=1).execute()
            latency_ms = (time.perf_counter() - t0) * 1000
            items = cal_list.get("items", [])
            cal_name = items[0].get("summary", "primary") if items else "primary"
            record("Calendar API", True,
                   f"calendar='{cal_name}'  latency={latency_ms:.0f}ms")
        except Exception as e:
            record("Calendar API", False, str(e)[:100])

    except Exception as e:
        record("Google APIs", False, str(e)[:120])


# ── 5. Twilio ─────────────────────────────────────────────────────────────────

async def check_twilio() -> None:
    section("5. Twilio (Phone Calls)")
    try:
        from config.settings import get_settings
        cfg = get_settings()

        if not cfg.twilio_enabled:
            record("Twilio", False,
                   "Not configured — SIMULATION MODE active (set TWILIO_* vars to enable real calls)")
            return

        import asyncio
        from twilio.rest import Client

        t0 = time.perf_counter()
        client = Client(cfg.twilio_account_sid, cfg.twilio_auth_token)
        # Fetch account info (lightweight API call to validate credentials)
        account = await asyncio.to_thread(lambda: client.api.accounts(cfg.twilio_account_sid).fetch())
        latency_ms = (time.perf_counter() - t0) * 1000

        record("Twilio credentials valid", True,
               f"account='{account.friendly_name}'  status={account.status}  latency={latency_ms:.0f}ms")
        record("Twilio from number", bool(cfg.twilio_from_number), cfg.twilio_from_number)
        record("Twilio to number",   bool(cfg.twilio_to_number),   cfg.twilio_to_number)

    except Exception as e:
        err = str(e)
        if "20003" in err or "authenticate" in err.lower():
            record("Twilio credentials valid", False, "Invalid TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN")
        else:
            record("Twilio", False, err[:120])


# ── 6. ChromaDB ──────────────────────────────────────────────────────────────

async def check_chromadb() -> None:
    section("6. ChromaDB (Vector Memory)")
    try:
        import chromadb
        from config.settings import get_settings
        cfg = get_settings()

        t0 = time.perf_counter()
        client = chromadb.PersistentClient(path=cfg.chroma_persist_path)
        collections = await asyncio.to_thread(client.list_collections)
        latency_ms = (time.perf_counter() - t0) * 1000
        col_names = [c.name for c in collections]
        record("ChromaDB init", True,
               f"path={cfg.chroma_persist_path}  collections={col_names or 'none yet'}  latency={latency_ms:.0f}ms")

        # Test embedding model
        try:
            from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
            t0 = time.perf_counter()
            ef = SentenceTransformerEmbeddingFunction(
                model_name=cfg.chroma_embedding_model, device="cpu"
            )
            embeddings = await asyncio.to_thread(ef, ["health check"])
            latency_ms = (time.perf_counter() - t0) * 1000
            dim = len(embeddings[0]) if embeddings else 0
            record(f"Embedding model ({cfg.chroma_embedding_model})", dim > 0,
                   f"dim={dim}  latency={latency_ms:.0f}ms")
        except Exception as e:
            record("Embedding model", False, str(e)[:100])

    except Exception as e:
        record("ChromaDB", False, str(e)[:120])


# ── 7. MCP Server ─────────────────────────────────────────────────────────────

async def check_mcp_server() -> None:
    section("7. MCP Server (port 8000)")
    try:
        import httpx
        from config.settings import get_settings
        cfg = get_settings()
        # Probe the root URL, NOT cfg.mcp_sse_url (/sse).
        # The /sse endpoint streams indefinitely — a GET against it would block
        # forever (or ReadTimeout) regardless of whether the server is healthy.
        # Any HTTP response from "/" confirms the ASGI app is routing.
        root_url = f"http://{cfg.mcp_server_host}:{cfg.mcp_server_port}/"

        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=3.0) as client:
            try:
                r = await client.get(root_url)
                latency_ms = (time.perf_counter() - t0) * 1000
                record("MCP Server running", True,
                       f"url={cfg.mcp_sse_url}  status={r.status_code}  latency={latency_ms:.0f}ms")
            except httpx.ConnectError:
                record("MCP Server running", False,
                       f"Not reachable at {cfg.mcp_sse_url} — it starts automatically when you run main.py")
    except Exception as e:
        record("MCP Server", False, str(e)[:100])


# ── 8. Installed packages ─────────────────────────────────────────────────────

def check_packages() -> None:
    section("8. Required Packages")
    required = [
        ("mcp",                      "mcp"),
        ("openai",                   "openai"),
        ("google.auth",              "google-auth"),
        ("googleapiclient",          "google-api-python-client"),
        ("redis",                    "redis[asyncio]"),
        ("chromadb",                 "chromadb"),
        ("sentence_transformers",    "sentence-transformers"),
        ("fastapi",                  "fastapi"),
        ("uvicorn",                  "uvicorn"),
        ("pydantic_settings",        "pydantic-settings"),
        ("httpx",                    "httpx"),
        ("twilio",                   "twilio"),
        ("structlog",                "structlog"),
        ("colorama",                 "colorama"),
    ]
    for module, pkg in required:
        try:
            __import__(module)
            record(pkg, True)
        except ImportError:
            record(pkg, False, f"Run: pip install {pkg}")


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary() -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total  = len(results)

    print(f"\n{BOLD}{'═' * 50}{RESET}")
    print(f"{BOLD}  Summary: {GREEN}{passed} passed{RESET}  {RED}{failed} failed{RESET}  (total {total}){RESET}")
    print(f"{BOLD}{'═' * 50}{RESET}\n")

    if failed == 0:
        print(f"{GREEN}All checks passed! Ready to run: python main.py{RESET}\n")
        return 0
    else:
        print(f"{RED}Fix the {failed} failed check(s) above, then re-run: python check.py{RESET}\n")
        print("Quick start for demo (no Google OAuth needed):")
        print("  python tests/seed_events.py")
        print("  python main.py --skip-poll --no-mcp\n")
        return 1


async def run_checks() -> int:
    print(f"\n{BOLD}PersonalOS Agent — Pre-flight Health Check{RESET}")
    print(f"{'═' * 50}")

    check_packages()
    check_env()
    await check_redis()
    await check_openrouter()
    await check_google()
    await check_twilio()
    await check_chromadb()
    await check_mcp_server()

    return print_summary()


if __name__ == "__main__":
    exit_code = asyncio.run(run_checks())
    sys.exit(exit_code)
