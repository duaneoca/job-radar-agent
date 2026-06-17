"""
Preflight check for a LOCAL deployment — run before scheduling, and any time something's off.

Verifies each dependency the local agent needs and prints a ✓/✗ checklist:
  email provider reachable (Proton Bridge login / Gmail token), folders/labels exist, LLM key set,
  Job Radar reachable + agent key valid. Exits non-zero if a CRITICAL check fails.

Reads config from the environment / .env (same as the agent). Run from the repo root:
    set -a; . ./.env; set +a; python scripts/doctor.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import settings as A          # noqa: E402
from mcp_email.config import folders            # noqa: E402
from mcp_email.config import settings as E      # noqa: E402

ok = True


def check(label, passed, detail="", critical=True):
    global ok
    mark = "✓" if passed else ("✗" if critical else "•")
    print(f"  {mark} {label}{(' — ' + detail) if detail else ''}")
    if critical and not passed:
        ok = False


def main() -> int:
    print(f"=== preflight: provider={E.email_provider}  →  {A.jobradar_api_url} ===")

    # 1. email provider reachable + folders
    provider = None
    try:
        if E.email_provider == "gmail":
            from mcp_email.providers.gmail import GmailProvider
            provider = GmailProvider(token_file=E.gmail_token_file,
                                     credentials_file=E.gmail_credentials_file,
                                     root_folder=folders.root)
        else:
            from mcp_email.providers.proton import ProtonProvider
            provider = ProtonProvider(E.proton_imap_host, E.proton_imap_port,
                                      E.proton_imap_user, E.proton_imap_password)
        all_folders = provider.list_folders()
        check("email provider login", True, f"{len(all_folders)} folders/labels")
        for f in [folders.root, *folders.all_subfolders()]:
            check(f"folder exists: {f}", f in all_folders, critical=(f == folders.root))
        try:
            n = len(provider.get_unread(folders.root, since_days=E.max_email_age_days or None, limit=5))
            check(f"unread in root (≤{E.max_email_age_days}d)", True, f"{n}+ found", critical=False)
        except Exception as exc:
            check("read unread", False, str(exc))
    except Exception as exc:
        check("email provider login", False, f"{type(exc).__name__}: {exc}")
    finally:
        if provider and hasattr(provider, "close"):
            provider.close()

    # 2. LLM key present (not called)
    check(f"LLM key set ({A.llm_provider}/{A.llm_model})", bool(A.llm_api_key))

    # 3. Job Radar reachable + agent key valid
    if not A.agent_api_key:
        check("agent API key set", False)
    else:
        try:
            from agent.writer_rest import RestWriter
            w = RestWriter(A.jobradar_api_url, agent_key=A.agent_api_key)
            try:
                reviews = w.get_reviews()
                check("Job Radar reachable + key valid", True, f"GET /agent/reviews → {len(reviews)}")
            finally:
                w.close()
        except Exception as exc:
            check("Job Radar reachable + key valid", False, f"{type(exc).__name__}: {exc}")

    # 4. optional integrations (informational)
    check("Langfuse configured", bool(os.environ.get("LANGFUSE_PUBLIC_KEY")), critical=False)
    check(f"notifier ({A.notifier})", A.notifier != "null", critical=False)
    check(f"daily spend ceiling ${A.daily_spend_ceiling_usd}", A.daily_spend_ceiling_usd > 0,
          "0 = disabled", critical=False)

    print("\n" + ("✓ preflight passed — safe to schedule." if ok else "✗ preflight FAILED — fix the ✗ items above."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
