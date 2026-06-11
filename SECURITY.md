# Job Radar Email Agent — Security Review (Threat Model)

**Status:** Round 1 review, pre-implementation
**Date:** 2026-06-06
**Reviewer hat:** offensive / red-team
**Scope:** the agentic email pipeline + its integration into job-radar

> Findings are ordered by severity. Each has a concrete, testable mitigation that must be
> baked into the relevant build phase — see the "Phase" tag. Nothing here is generic; all
> are specific to this design.

---

## Severity legend
🔴 CRITICAL — direct path to account/data compromise; fix before any prod exposure
🟠 HIGH — serious; exploitable under realistic conditions
🟡 MEDIUM — meaningful risk, defense-in-depth
🟢 LOWER — noted, low impact or low likelihood

---

## 🔴 CRITICAL

### C1 — Prompt injection via email (inherent threat)
**Phase:** A-3 (graph), A-4 (prompts)
Every email is attacker-controlled input fed to Classifier + Critic. Injection can force
misclassification, wrong folder routing, or a wrong status write on a real `UserJobReview`.
The **Critic is not a defense** — it is the same injectable LLM reading the same hostile text.
Injected `company`/`role` can steer an interaction to mutate a legitimate tracked job's status.

**Mitigations (all required):**
- Treat email body strictly as DATA: wrap in explicit delimiters; system prompt states content
  between delimiters is never instructions.
- Minimal tool surface (already): read / mark-read / move / write-inbox / constrained status.
- Server-side enforcement of the 4-value writable status enum (`applied|interviewing|offer|rejected`);
  reject illogical transitions.
- `confidence` is a structured field the model must justify; the email text can never set it.
- Cannot be eliminated — this is the core argument for HITL + the `Unprocessed` queue.

### C2 — Stored XSS from email content → job-radar account takeover
**Phase:** JR-4 (frontend), JR-2 (API write validation)
Agent-extracted `company`, `role`, `links`, `sender` are rendered in the inbox/dashboard. A
`javascript:` link or `<img onerror=...>` field can execute JS in the authenticated job-radar
session → cookie/session theft → full account takeover, reachable by anyone who can email you.

**Mitigations:**
- URL scheme allowlist: only `http`/`https` survive; drop `javascript:`, `data:`, `file:`.
- Sanitize/escape every agent-derived field on render. Audit `href`, `dangerouslySetInnerHTML`,
  any HTML-email preview. Never render raw HTML email bodies in job-radar.
- Validate URL scheme server-side at write time (defense in depth).

### C3 — `SECRET_KEY` is a single point of total compromise
**Phase:** JR-1 / job-radar hardening (do first)
All per-user secrets are Fernet-encrypted with a key derived (SHA-256) from `SECRET_KEY`. We are
about to add **email credentials / Gmail refresh tokens** to the same vault. If `SECRET_KEY` is
default/weak/leaked, every user's LLM keys AND mailbox credentials are decryptable at once. A
Gmail refresh token = long-lived `gmail.modify` access to the whole mailbox.

**Current state (from recon):** default exists only as a code placeholder; `k8s/README.md`
documents manual creation via `secrets.token_hex(32)`. Production is *likely* strong — **must be
confirmed against the live cluster secret.**

**Mitigations:**
- Fail-closed startup guard: refuse to boot in production if `SECRET_KEY` is default/short.
  (Cover `admin_password` default too.)
- Store via k8s Secret created manually (never git/GitHub), ideally sourced from AWS SSM
  SecureString / Secrets Manager (EC2 IAM role already available).
- Build `MultiFernet` rotation (`ENCRYPTION_KEY` + `ENCRYPTION_KEY_OLD`) + a re-encrypt command.
- Separate key for email creds (key segregation); consider KMS envelope encryption.
- **If key was ever default/committed:** treat stored secrets as compromised — rotate key,
  re-encrypt, and have users rotate their actual provider API keys (past exposure is unfixable
  by re-encryption). No email creds in prod yet, so nothing to migrate there.

### C3b — `SECRET_KEY` reused for two crypto purposes
**Phase:** JR-1
Same key signs JWTs AND encrypts credentials. Rotating the JWT key (e.g., after token leak) would
break decryption of all stored creds — turning a token incident into data loss.
**Mitigation:** split into dedicated `ENCRYPTION_KEY` (Fernet) vs `SECRET_KEY` (JWT); migrate
during the re-encryption work.

### C4 — Public HITL Slack callback = unauthenticated write path
**Phase:** JR-2 (endpoint), A-5 (HITL)
`POST /agent/hitl/callback` must be internet-facing for Slack. Without signature verification,
anyone can forge `{hitl_id, choice}` to mis-route status updates, poison/resume checkpoints, or
flood. Chains with C1.

**Mitigations:**
- Verify Slack signing secret (`X-Slack-Signature` + timestamp, reject >5 min skew) on every call.
- Treat `hitl_id`/`choice` as untrusted; server-side validate the chosen `review_id` belongs to
  the same user as the checkpoint (see H1).
- Rate-limit the endpoint.

---

## 🟠 HIGH

### H1 — Cross-tenant IDOR
**Phase:** JR-2, JR-3
Early endpoint sketches took `user_id` as a parameter (e.g. `/agent/reviews?user_id=`); the existing
`ai-review?user_id=` still does. If the agent API key doesn't derive+enforce the user, one key reads/
writes every user's data. **The contract now derives user from the key (no `user_id` param);** this
finding remains as the rationale and must be enforced in implementation (incl. the existing endpoint).
**Rule:** API key maps to exactly one user; `user_id` is NEVER trusted from the request. Validate
ownership of every `review_id` / `hitl_id` / `inbox_id` at use time.

### H2 — "Email never leaves your machine" is false; Langfuse worsens it
**Phase:** A-4, design messaging
- Email bodies go to the LLM API (third party) — for BYOK it's the user's own account, but must be
  disclosed honestly.
- Langfuse traces capture prompt inputs = the email body → raw email lands in Langfuse cloud,
  contradicting "only structured data leaves." Admin Slack trace links expose the same to anyone
  in the admin channel.
**Mitigations:** deliberately choose — redact/truncate email bodies before spans, OR document that
Langfuse holds email content. Control admin-channel membership. Fix the README privacy claims.

### H3 — Internal no-auth endpoints + flat cluster network
**Phase:** JR-3, JR-5
job-radar has `include_in_schema=False` no-auth internal endpoints; MCP Writer → tracker-api adds
more. In a flat k3s network, a compromised pod or SSRF anywhere = direct unauthenticated writes.
**Mitigations:** NetworkPolicies restricting reachability of internal routes; a shared internal
service token even for "internal" calls; ensure Cloudflare→origin is **Full (Strict)** TLS, not
Flexible (else the public hop is plaintext).

### H4 — Cost-based DoS on the user's BYOK key
**Phase:** A-3
Flooding the monitored folder burns the user's LLM balance and hits rate limits. 30-posting cap is
per-email only; nothing caps emails/run, tokens/run, or spend/day.
**Mitigations:** per-run max email count, per-run token budget, per-day spend ceiling, circuit
breaker that halts + alerts the admin channel.

### H5 — Gmail OAuth scope over-grant
**Phase:** A-2
`gmail.modify` (needed for mark-read + label move) also permits trashing messages; our "no delete"
guardrail is enforced only in our code, not by the token. A leaked token isn't bound by the guardrail.
**Mitigations:** request the minimum scope supporting label add/remove + mark-read; never request
full `https://mail.google.com/`. Store refresh tokens under the strongest key tier. Document residual
risk.

### H6 — Decrypted credentials in transit + in agent memory (config bundle)
**Phase:** JR-2 (endpoint), A-3 (agent handling)
The agent calls the LLM directly (for Langfuse tracing), so it needs the **plaintext** LLM key and
mailbox creds at runtime. `GET /agent/config` therefore returns **decrypted secrets** server→agent.
Two exposures the rest of the model didn't name: (1) creds transit the network; (2) creds live in the
agent process. **Cloud multi-user blast radius:** a single run can hold *many* users' decrypted keys/
tokens at once → one compromised agent = everyone's creds, not just one. (Local self-host is benign —
owner's own creds only.) The config endpoint is also a high-value single target: one authenticated
call returns everything.

**Mitigations:**
- TLS-only; the config endpoint gets the strongest auth, rate-limiting, and **audit logging** (who
  fetched whose creds, when).
- Agent holds creds **ephemerally in memory only** — never log them, never write to disk or into the
  LangGraph checkpoint state; minimize lifetime; zero/drop after use.
- **Per-user isolation** in the cloud run; prefer **fetch-per-user then discard** over loading every
  user's creds up front, so blast radius is one user, not all.
- Cannot be eliminated (provider keys are bearer secrets with no scoped-token equivalent) — contain it.

---

## 🟡 MEDIUM

### M1 — Attachments & HTML email parsing
**Phase:** A-2
Don't auto-parse attachments (malware, zip bombs, XXE via XML). Parse only `text/plain` + sanitized
`text/html`; strip remote content (no remote image fetch — tracking pixels confirm processing to the
sender). Safe HTML parser, external entity resolution disabled.

### M2 — Agent must never dereference email links
**Phase:** A-2, A-3
Extract-and-surface only. Any agent URL fetch = SSRF + malware retrieval; inside EC2 it could reach
the cloud metadata endpoint. Make "agent never visits links" an explicit, tested invariant.

### M3 — Secrets / PII in the public portfolio repo
**Phase:** A-1, ongoing
Public repo risks committing `.env`, Langfuse keys, an agent key, and especially the real example
emails (your address, recruiter names, real links).
**Mitigations:** `.gitignore` + `.env.example` only; gitleaks pre-commit hook; scrub/synthesize
example emails before they touch git; keep real fixtures local-only.

### M4 — Plaintext secrets at rest on the local machine
**Phase:** A-1, A-6
Local Proton path keeps agent API key + Bridge creds in local `.env`. Acceptable for self-host;
document it, set perms `600`. The checkpointer store (SQLite/Postgres) may persist partial email
content in checkpoint state — protect that file too.

### M5 — Slack as a third-party data sink
**Phase:** A-5
Job-hunt activity (companies, rejections, offers) flows to a third-party workspace visible to its
admins. Webhook URLs are bearer secrets. Treat as secrets; prefer scoped tokens over legacy webhooks.

---

## 🟢 LOWER / NOTED

- **L1 — Message-ID is attacker-settable.** As idempotency key it can be forged to suppress
  processing (collide) or rotated to flood. Always scope `(user_id, message_id)` + H4 caps.
- **L2 — Lockfile predictable path / PID reuse.** Co-tenant DoS or false "alive." Namespaced path,
  store start time.
- **L3 — Multi-tenant blast radius (cloud CronJob).** One poison email for user A must not abort the
  run for others. Per-user try/except isolation + per-user locks.
- **L4 — GHCR image hygiene.** No secrets baked in; pin base images; optionally cosign-sign.

---

## Top 3 to lose sleep over
1. **C2** — stranger's email → job-radar account takeover. Cheapest kill: URL allowlist + render sanitize.
2. **C3/C3b** — one key protecting the whole cred vault (soon incl. mailbox tokens), reused for JWT.
3. **C1** — prompt injection: containable, not eliminable → the real reason HITL + constrained tools matter.

---

## Cross-cutting principles to enforce everywhere
- **Untrusted by default:** email content, Slack callbacks, anything from the network.
- **Derive identity, never accept it:** API key → user; never trust `user_id`/`review_id` from caller.
- **Least privilege:** minimal OAuth scopes, minimal tool surface, minimal network reachability.
- **Secrets never in code/git/CI:** runtime injection only (k8s Secret / SSM), split by purpose, rotatable.
- **Honest privacy posture:** state exactly where email content goes (LLM API, Langfuse).
