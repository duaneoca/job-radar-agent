"""
Local end-to-end runner — read real Proton mail → classify (real LLM) → write to Job Radar.

DRY-RUN by default: classifies and prints what WOULD happen, but moves nothing and writes nothing.
Pass --commit to actually move emails + write to Job Radar.

Env (from .env.smoke / .env):
    PROTON_IMAP_* , EMAIL_ROOT_FOLDER, MAX_EMAIL_AGE_DAYS, MAX_EMAILS_PER_RUN   (email)
    LLM_PROVIDER / LLM_MODEL / LLM_API_KEY                                       (BYOK)
    JOBRADAR_API_URL / AGENT_API_KEY                                            (writer)

Usage:
    set -a; . ./.env.smoke; set +a
    python scripts/run_local.py            # dry run
    python scripts/run_local.py --commit   # for real
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import make_critic_llm, make_llm                # noqa: E402
from agent.prompts import SeedPromptProvider                      # noqa: E402
from agent.reader import ProviderReader                           # noqa: E402
from agent.runner import run_once                                 # noqa: E402
from agent.writer_rest import RestWriter                          # noqa: E402
from mcp_email.config import folders, settings as email_settings  # noqa: E402
from mcp_email.providers.proton import ProtonProvider             # noqa: E402


def main(argv) -> int:
    commit = "--commit" in argv
    base = os.environ.get("JOBRADAR_API_URL", "https://staging.job-radar.net/api")
    agent_key = os.environ.get("AGENT_API_KEY", "")
    if not os.environ.get("LLM_API_KEY"):
        print("✗ set LLM_API_KEY (and LLM_PROVIDER/LLM_MODEL)"); return 2
    if not agent_key:
        print("✗ set AGENT_API_KEY"); return 2

    provider = ProtonProvider(
        email_settings.proton_imap_host, email_settings.proton_imap_port,
        email_settings.proton_imap_user, email_settings.proton_imap_password,
    )
    reader = ProviderReader(
        provider, root=folders.root,
        dest_folders={"interaction": folders.interaction, "postings": folders.postings,
                      "social": folders.social, "unprocessed": folders.unprocessed},
        since_days=email_settings.max_email_age_days if email_settings.max_email_age_days > 0 else None,
        limit=email_settings.max_emails_per_run,
    )
    writer = RestWriter(base, agent_key)

    mode = "COMMIT" if commit else "DRY-RUN"
    print(f"=== {mode} — {folders.root} → {base} (model {os.environ.get('LLM_MODEL')}) ===")
    try:
        res = run_once(
            reader=reader, writer=writer,
            llm=make_llm(), critic_llm=make_critic_llm(),
            prompts=SeedPromptProvider(),
            environment="local", dry_run=not commit,
        )
    finally:
        provider.close()
        writer.close()

    if res.skipped:
        print("• skipped (another run holds the lock)"); return 0
    for d in res.details:
        conf = f"{d['confidence']:.2f}" if d["confidence"] is not None else "—"
        print(f"  [{d['outcome'] or 'ERR':12}] {d['category'] or '?':24} conf={conf}  "
              f"{'→ '+d['destination'] if d['destination'] else '(no move)':14}  {d['subject']!r}")
    print(f"\n{mode} summary: status={res.status} processed={res.emails_processed} "
          f"postings={res.postings_created} interactions={res.interactions_recorded} "
          f"escalations={res.escalations} retries={res.retries}")
    for e in res.errors:
        print("  ! " + e)
    if not commit:
        print("\n(nothing moved or written — re-run with --commit to act)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
