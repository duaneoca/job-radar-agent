"""
Wiring shared by the one-shot CLI (run_local) and the interval loop (run_loop).

build_components() reads config (provider chosen by EMAIL_PROVIDER) and constructs the reader,
writer, LLM clients, and notifier — the full local-path stack — plus a closer for cleanup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .config import make_critic_llm, make_llm, make_notifier
from .config import settings as agent_settings
from .reader import ProviderReader
from .writer_rest import RestWriter
from mcp_email.config import folders
from mcp_email.config import settings as email_settings


@dataclass
class Components:
    reader: object
    writer: object
    llm: object
    critic_llm: object
    notifier: object
    inbox_base_url: str
    close: Callable[[], None]


def _build_provider():
    if email_settings.email_provider == "gmail":
        from mcp_email.providers.gmail import GmailProvider
        return GmailProvider(token_file=email_settings.gmail_token_file,
                             credentials_file=email_settings.gmail_credentials_file,
                             root_folder=folders.root)
    from mcp_email.providers.proton import ProtonProvider
    return ProtonProvider(email_settings.proton_imap_host, email_settings.proton_imap_port,
                          email_settings.proton_imap_user, email_settings.proton_imap_password)


def build_components() -> Components:
    if not agent_settings.agent_api_key:
        raise SystemExit("✗ AGENT_API_KEY not set")
    if not agent_settings.llm_api_key:
        raise SystemExit("✗ LLM_API_KEY not set")

    provider = _build_provider()
    reader = ProviderReader(
        provider, root=folders.root,
        dest_folders={"interaction": folders.interaction, "postings": folders.postings,
                      "social": folders.social, "unprocessed": folders.unprocessed},
        since_days=email_settings.max_email_age_days if email_settings.max_email_age_days > 0 else None,
        limit=email_settings.max_emails_per_run,
    )
    writer = RestWriter(agent_settings.jobradar_api_url, agent_settings.agent_api_key)
    notifier = make_notifier()

    def _close():
        for obj in (provider, writer, notifier):
            if hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    pass

    return Components(
        reader=reader, writer=writer, llm=make_llm(), critic_llm=make_critic_llm(),
        notifier=notifier, inbox_base_url=agent_settings.jobradar_api_url.replace("/api", ""),
        close=_close,
    )
