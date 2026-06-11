"""
Email Reader MCP configuration.

Local self-host path reads everything from the environment (.env). The agent only ever processes
UNREAD mail in the configured ROOT folder; subfolders are move destinations. Folder names are
user-configurable so this is portable across mailboxes. [D6/D7]
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    email_provider: str = "proton"           # proton | gmail

    # Folder layout (configurable names)
    email_root_folder: str = "hire-duane"
    email_folder_interaction: str = "Interaction"
    email_folder_postings: str = "Postings"
    email_folder_social: str = "Social"
    email_folder_unprocessed: str = "Unprocessed"

    # Proton Bridge (local path)
    proton_imap_host: str = "host.docker.internal"
    proton_imap_port: int = 1143
    proton_imap_user: str = ""
    proton_imap_password: str = ""


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

    def all_subfolders(self) -> list[str]:
        return [self.interaction, self.postings, self.social, self.unprocessed]


settings = Settings()
folders = Folders.from_settings(settings)
