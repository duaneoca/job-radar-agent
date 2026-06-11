"""
Mint an agent API key headlessly (until the Email Agent settings UI exists).

Logs into Job Radar with the user's own credentials, calls POST /agent/keys, and writes the
returned plaintext key straight into AGENT_API_KEY in .env.smoke (gitignored) — so the secret is
never pasted by hand or echoed.

Env (from .env.smoke):
    JOBRADAR_API_URL     default https://staging.job-radar.net/api
    JOBRADAR_EMAIL       your Job Radar login
    JOBRADAR_PASSWORD    your Job Radar password

Usage:
    set -a; . ./.env.smoke; set +a
    python scripts/mint_agent_key.py
"""

import os
import re
import sys

import httpx

BASE = os.environ.get("JOBRADAR_API_URL", "https://staging.job-radar.net/api").rstrip("/")
EMAIL = os.environ.get("JOBRADAR_EMAIL", "")
PASSWORD = os.environ.get("JOBRADAR_PASSWORD", "")
ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.smoke")


def _extract_key(data) -> str | None:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for k in ("key", "api_key", "agent_key", "plaintext", "token", "value"):
            if data.get(k):
                return data[k]
    return None


def _write_env(key: str) -> None:
    with open(ENV_PATH, encoding="utf-8") as f:
        text = f.read()
    if re.search(r"^AGENT_API_KEY=.*$", text, flags=re.M):
        text = re.sub(r"^AGENT_API_KEY=.*$", f"AGENT_API_KEY={key}", text, flags=re.M)
    else:
        text += f"\nAGENT_API_KEY={key}\n"
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def main() -> int:
    if not EMAIL or not PASSWORD:
        print("✗ set JOBRADAR_EMAIL and JOBRADAR_PASSWORD in .env.smoke first")
        return 2
    with httpx.Client(base_url=BASE, timeout=30.0) as c:
        r = c.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
        if r.status_code >= 400:
            print(f"✗ login failed ({r.status_code}): {r.text[:200]}")
            return 1
        token = r.json().get("access_token")
        if not token:
            print(f"✗ no access_token in login response: {r.text[:200]}")
            return 1
        c.headers["Authorization"] = f"Bearer {token}"

        r = c.post("/agent/keys", json={"name": "local-proton-agent"})
        if r.status_code >= 400:
            print(f"✗ POST /agent/keys failed ({r.status_code}): {r.text[:200]}")
            return 1
        key = _extract_key(r.json())
        if not key:
            print(f"✗ could not find the plaintext key in response: {r.json()}")
            return 1

    _write_env(key)
    print(f"✓ minted agent key (…{key[-4:]}) and wrote AGENT_API_KEY into .env.smoke")
    print("  Next: python scripts/smoke_jobradar.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
