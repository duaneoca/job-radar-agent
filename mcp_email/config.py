"""
Email Reader MCP configuration.

Local self-host path reads everything from the environment (.env). Folder names are
user-configurable so this is portable across mailboxes. [D6/D7]

V2: a mail filter (e.g. a Proton sieve rule) sorts job alerts into `<root>/Postings`. The agent reads
UNREAD mail from that folder only (`Folders.source`) and moves problem mail to `<root>/Unprocessed`.
The Interaction / Social names are kept for the folder layout but V2 does not use them.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_settings import BaseSettings, SettingsConfigDict

from agent.paths import env_file


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=env_file(), extra="ignore")

    email_provider: str = "proton"           # proton | gmail | imap (e.g. Yahoo)

    # Folder layout (configurable names)
    email_root_folder: str = "hire-duane"
    email_folder_interaction: str = "Interaction"
    email_folder_postings: str = "Postings"
    email_folder_social: str = "Social"
    email_folder_unprocessed: str = "Unprocessed"

    # First-run / backlog controls
    max_email_age_days: int = 14     # ignore unread mail older than this (0/negative ⇒ no cutoff)
    max_emails_per_run: int = 25     # cap per run, newest-first

    # Proton Bridge (local path)
    proton_imap_host: str = "host.docker.internal"
    proton_imap_port: int = 1143
    proton_imap_user: str = ""
    proton_imap_password: str = ""

    # Generic IMAP over TLS (e.g. Yahoo: imap.mail.yahoo.com:993 + an app password)
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_use_ssl: bool = True

    # Gmail (API/OAuth)
    gmail_credentials_file: str = "credentials.json"
    gmail_token_file: str = "token.json"


@dataclass(frozen=True)
class Folders:
    """Resolved folder paths. Subfolders are nested under the root (e.g. `hire-duane/Interaction`)."""

    root: str
    interaction: str
    postings: str
    social: str
    unprocessed: str

    @classmethod
    def from_settings(cls, s: "Settings") -> "Folders":
        root = s.email_root_folder
        sep = "/"
        return cls(
            root=root,
            interaction=f"{root}{sep}{s.email_folder_interaction}",
            postings=f"{root}{sep}{s.email_folder_postings}",
            social=f"{root}{sep}{s.email_folder_social}",
            unprocessed=f"{root}{sep}{s.email_folder_unprocessed}",
        )

    @property
    def source(self) -> str:
        """The one folder V2 reads from."""
        return self.postings

    def v2_folders(self) -> list[str]:
        """Folders V2 needs to exist."""
        return [self.source, self.unprocessed]

    def sort_destinations(self) -> dict[str, str]:
        """Logical destination → full folder path, for the sorter's reader over the root."""
        return {"interaction": self.interaction, "postings": self.postings,
                "social": self.social, "unprocessed": self.unprocessed}

    def all_subfolders(self) -> list[str]:
        return [self.interaction, self.postings, self.social, self.unprocessed]


settings = Settings()
folders = Folders.from_settings(settings)
