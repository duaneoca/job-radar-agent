"""
Generic IMAP provider for the CLOUD path (user-supplied mailbox over real TLS).

The local Proton path already speaks IMAP via `ProtonProvider`; the only thing that differs for an
arbitrary cloud IMAP account is the *connection* (real `IMAP4_SSL`, vs Proton Bridge's
plaintext-loopback + opportunistic STARTTLS). So this subclasses `ProtonProvider` and reuses ALL of
its read / move-and-mark / body-extraction logic — including the link-preserving HTML strip, the
per-posting structure, and the invisible-char cleanup — overriding only `__init__` and `_imap()`.

Built from the `email_credentials` blob Job Radar serves for IMAP users:
`{provider:"imap", host, port, username, password, use_ssl}` (see INTEGRATION_SPEC §1.5).
"""

from __future__ import annotations

import imaplib

from .proton import ProtonProvider


class ImapProvider(ProtonProvider):
    def __init__(self, creds_info: dict, timeout: float = 30.0):
        super().__init__(
            host=creds_info["host"],
            port=int(creds_info.get("port") or 993),
            user=creds_info["username"],
            password=creds_info["password"],
            timeout=timeout,
        )
        # default to TLS — only go plaintext if the config explicitly says so
        self._use_ssl = bool(creds_info.get("use_ssl", True))

    def _imap(self) -> imaplib.IMAP4:
        if self._conn is None:
            if self._use_ssl:
                conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    self._host, self._port, timeout=self._timeout
                )
            else:
                # plaintext requested — still try STARTTLS opportunistically before logging in
                conn = imaplib.IMAP4(self._host, self._port, timeout=self._timeout)
                try:
                    conn.starttls()
                except (imaplib.IMAP4.error, OSError):
                    pass
            conn.login(self._user, self._password)
            self._conn = conn
        return self._conn
