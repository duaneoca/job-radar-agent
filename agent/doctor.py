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
                                     root_folder=folders.source)
        else:
            from mcp_email.providers.proton import ProtonProvider
            provider = ProtonProvider(E.proton_imap_host, E.proton_imap_port,
                                      E.proton_imap_user, E.proton_imap_password)
        all_folders = provider.list_folders()
        check("email provider login", True, f"{len(all_folders)} folders/labels")
        for f in folders.v2_folders():
            check(f"folder exists: {f}", f in all_folders)
        try:
            n = len(provider.get_unread(folders.source, since_days=E.max_email_age_days or None, limit=5))
            check(f"unread in {folders.source} (≤{E.max_email_age_days}d)", True, f"{n}+ found",
                  critical=False)
        except Exception as exc:
            check("read unread", False, str(exc))
    except Exception as exc:
        check("email provider login", False, f"{type(exc).__name__}: {exc}")
    finally:
        if provider and hasattr(provider, "close"):
            provider.close()

    if not A.llm_api_key:
        check(f"LLM key set ({A.llm_provider}/{A.llm_model})", False)
    else:
        # Live 1-token completion — verifies the key AND the model string actually resolve.
        # A "key present" check passes even when the model id is wrong (e.g. a UI display name),
        # which then fails every call mid-run.
        try:
            import litellm
            from .llm_litellm import _model_string
            litellm.completion(
                model=_model_string(A.llm_provider, A.llm_model), api_key=A.llm_api_key,
                messages=[{"role": "user", "content": "ping"}], max_tokens=5, timeout=30,
            )
            check(f"LLM reachable ({A.llm_provider}/{A.llm_model})", True)
        except Exception as exc:
            check(f"LLM reachable ({A.llm_provider}/{A.llm_model})", False,
                  f"{type(exc).__name__}: {str(exc)[:160]}")

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

    if not (A.langfuse_public_key and A.langfuse_secret_key):
        check("Langfuse configured", False, "keys not set (tracing off)", critical=False)
    else:
        # Actually authenticate against the server — catches wrong keys AND wrong region/host,
        # which a "keys present" check silently misses.
        try:
            from .observability import get_langfuse
            lf = get_langfuse()
            ok_lf = bool(lf and lf.auth_check())
            check("Langfuse reachable + keys valid", ok_lf,
                  A.langfuse_host, critical=False)
        except Exception as exc:
            check("Langfuse reachable + keys valid", False,
                  f"{A.langfuse_host}: {type(exc).__name__}: {exc}", critical=False)
    # V2 policy (informational)
    from .config import make_sender_policy, zero_postings_action
    try:
        pol = make_sender_policy()
        doms = ", ".join(pol.allowed_domains) or "ANY sender (allow-list empty)"
        check("sender allow-list", bool(pol.allowed_domains), doms, critical=False)
        check("sender DMARC check", pol.require_auth,
              "on (" + ", ".join(pol.trusted_authserv_ids) + ")" if pol.require_auth else "OFF",
              critical=False)
        check(f"zero-postings action: {zero_postings_action()}", True, critical=False)
    except ValueError as exc:
        check("V2 policy settings", False, str(exc))
    if A.dedup_window_days > 0:
        from .paths import data_dir
        check(f"duplicate window {A.dedup_window_days}d", True,
              str(data_dir() / "dedup.sqlite"), critical=False)
    else:
        check("duplicate suppression", False, "DEDUP_WINDOW_DAYS=0 (off)", critical=False)
    check(f"notifier ({A.notifier})", A.notifier != "null", critical=False)
    check(f"daily spend ceiling ${A.daily_spend_ceiling_usd}", A.daily_spend_ceiling_usd > 0,
          "0 = disabled", critical=False)

    print("\n" + ("✓ preflight passed — safe to schedule." if ok
                  else "✗ preflight FAILED — fix the ✗ items above."))
    return 0 if ok else 1
