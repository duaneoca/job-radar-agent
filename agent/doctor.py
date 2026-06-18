"""
Preflight check — verify every dependency the agent needs and print a ✓/✗ checklist.
Exposed as `job-radar-agent doctor`. Returns 0 if all CRITICAL checks pass, else 1.
"""

from __future__ import annotations

from .config import settings as A
from mcp_email.config import folders
from mcp_email.config import settings as E


def run() -> int:
    ok = True

    def check(label, passed, detail="", critical=True):
        nonlocal ok
        mark = "✓" if passed else ("✗" if critical else "•")
        print(f"  {mark} {label}{(' — ' + detail) if detail else ''}")
        if critical and not passed:
            ok = False

    from .paths import agent_home, env_file
    print(f"=== preflight: provider={E.email_provider}  →  {A.jobradar_api_url} ===")
    print(f"    config: {env_file()}   home: {agent_home()}")

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

    check(f"LLM key set ({A.llm_provider}/{A.llm_model})", bool(A.llm_api_key))

    if not A.agent_api_key:
        check("agent API key set", False)
    else:
        try:
            from .writer_rest import RestWriter
            w = RestWriter(A.jobradar_api_url, agent_key=A.agent_api_key)
            try:
                check("Job Radar reachable + key valid", True,
                      f"GET /agent/reviews → {len(w.get_reviews())}")
            finally:
                w.close()
        except Exception as exc:
            check("Job Radar reachable + key valid", False, f"{type(exc).__name__}: {exc}")

    check("Langfuse configured", bool(A.langfuse_public_key and A.langfuse_secret_key),
          f"→ {A.langfuse_host}" if A.langfuse_public_key else "", critical=False)
    check(f"notifier ({A.notifier})", A.notifier != "null", critical=False)
    check(f"daily spend ceiling ${A.daily_spend_ceiling_usd}", A.daily_spend_ceiling_usd > 0,
          "0 = disabled", critical=False)

    print("\n" + ("✓ preflight passed — safe to schedule." if ok
                  else "✗ preflight FAILED — fix the ✗ items above."))
    return 0 if ok else 1
