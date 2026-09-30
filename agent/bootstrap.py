"""
Wiring shared by the CLI (`job-radar-agent run`), the interval loop, and scripts/run_local.py.

build_components() reads the local `.env` and constructs the full local stack: reader (Postings
folder), writer, the link-picker LLM, sender policy, duplicate store, and notifier — plus a closer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from .config import (make_dedup_store, make_llm, make_notifier, make_sender_policy,
                     make_sort_stage, zero_postings_action)
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
    policy: object
    dedup: object
    zero_postings_action: str
    notifier: object
    spend_store: object          # DailySpendStore — the $ ceiling is enforced on EVERY entry point
    daily_ceiling: float
    inbox_base_url: str
    close: Callable[[], None]
    sort_stage: object = None     # SortStage over the root folder, when SORTER_ENABLED


def _build_provider():
    if email_settings.email_provider == "gmail":
        from mcp_email.providers.gmail import GmailProvider
        return GmailProvider(token_file=email_settings.gmail_token_file,
                             credentials_file=email_settings.gmail_credentials_file,
                             root_folder=folders.source,
                             managed_folders=[folders.root, *folders.all_subfolders()])
    if email_settings.email_provider == "imap":
        from mcp_email.providers.imap import ImapProvider
        return ImapProvider({"host": email_settings.imap_host, "port": email_settings.imap_port,
                             "username": email_settings.imap_user,
                             "password": email_settings.imap_password,
                             "use_ssl": email_settings.imap_use_ssl})
    from mcp_email.providers.proton import ProtonProvider
    return ProtonProvider(email_settings.proton_imap_host, email_settings.proton_imap_port,
                          email_settings.proton_imap_user, email_settings.proton_imap_password)


def build_components() -> Components:
    if not agent_settings.agent_api_key:
        raise SystemExit("✗ AGENT_API_KEY not set")
    if not agent_settings.llm_api_key:
        raise SystemExit("✗ LLM_API_KEY not set")

    # Email Reader transport: 'mcp' consumes the stdio Email Reader server; 'direct' (default) uses
    # the provider in-process. Both expose the same EmailReaderClient interface.
    since = email_settings.max_email_age_days if email_settings.max_email_age_days > 0 else None
    sort_stage = None
    if os.environ.get("EMAIL_READER_TRANSPORT", "direct").lower() == "mcp":
        from .reader_mcp import McpReaderClient
        reader: object = McpReaderClient()
        if agent_settings.sorter_enabled:
            raise SystemExit("✗ the sorter needs EMAIL_READER_TRANSPORT=direct")
    else:
        provider = _build_provider()
        reader = ProviderReader(
            provider, source=folders.source,
            dest_folders={"unprocessed": folders.unprocessed},
            since_days=since, limit=email_settings.max_emails_per_run,
        )
        # The sorter reads the ROOT folder through the same provider (one mailbox connection).
        sort_stage = make_sort_stage(ProviderReader(
            provider, source=folders.root, dest_folders=folders.sort_destinations(),
            since_days=since, limit=email_settings.max_emails_per_run))
    writer = RestWriter(agent_settings.jobradar_api_url, agent_settings.agent_api_key)
    dedup = make_dedup_store()
    notifier = make_notifier()
    from .budget import DailySpendStore

    def _close():
        for obj in (reader, writer, dedup, notifier, getattr(sort_stage, "jev", None)):
            if hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    pass

    return Components(
        reader=reader, writer=writer, llm=make_llm(), policy=make_sender_policy(), dedup=dedup,
        zero_postings_action=zero_postings_action(), notifier=notifier,
        spend_store=DailySpendStore(), daily_ceiling=agent_settings.daily_spend_ceiling_usd,
        inbox_base_url=agent_settings.jobradar_api_url.replace("/api", ""), close=_close,
        sort_stage=sort_stage,
    )
