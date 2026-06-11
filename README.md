# Job Radar Email Agent

[![CI](https://github.com/duaneoca/job-radar-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/duaneoca/job-radar-agent/actions/workflows/ci.yml)

An agentic email-processing pipeline that reads job-related email, classifies and extracts
structured data with an LLM, self-validates through a Classifier→Critic loop, escalates ambiguous
cases to a human via interactive Slack, and writes results into [Job Radar](https://job-radar.net)'s
Inbox through a custom MCP server.

Built to demonstrate enterprise agentic patterns end-to-end: **LangGraph** orchestration with
validation loops and durable human-in-the-loop, **Langfuse** observability and prompt management,
**Model Context Protocol** (consuming *and* publishing servers), multi-tenant BYOK deployment, and a
documented threat model.

> Status: in development. See `INTEGRATION_SPEC.md` for the contract with Job Radar, `CLAUDE.md` for
> architecture and conventions, and `SECURITY.md` for the threat model.

## Architecture

```
Email (Proton Bridge IMAP | Gmail API)
  → MCP Server 1 (Email Reader, stdio)
  → LangGraph agent  ⇄  Langfuse (traces + versioned prompts)
  → MCP Server 2 (Job Radar Writer, HTTPS + per-user API key)
  → Job Radar: Inbox page + Ops dashboard
Notifications: Slack / Telegram / Discord.  HITL: Slack buttons + pull-model resume.
```

## Repository layout
See `CLAUDE.md`.

## Setup
TBD (Docker Compose for local self-host; k8s for cloud). `.env.example` documents all variables.
