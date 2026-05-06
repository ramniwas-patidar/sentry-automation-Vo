# 05 — External Services: Sentry, OpenAI, GitHub, Jira

This server is a **glue** between four third-party systems. To make sense of the code you need a one-paragraph mental model of each. This page gives you exactly that, plus an explanation of how API authentication works.

---

## What is an API token, in 60 seconds

Most third-party APIs are HTTPS endpoints (URLs) that you call with a special header that proves who you are. That header is usually:

```
Authorization: Bearer ghp_AbCdEf1234...
```

The long string is a **token**. It's like a password, but:
- It's tied to a *machine identity* (a bot user or service account), not your personal login.
- It can be scoped (e.g. "read-only", or "can only access repo X").
- It can be revoked instantly without changing your password.
- It must NEVER be committed to git.

In this project, all four tokens live in `.env` (which is gitignored). Code reads them via `from config import settings` → `settings.GITHUB_TOKEN`.

If a token leaks (committed by accident, posted in Slack), **rotate it immediately** in the provider's dashboard.

---

## 1. Sentry — `services/sentry_service.py`

**What it is.** Sentry is an *error monitoring* service. Frontend and backend apps send Sentry every uncaught exception they hit in production, with stacktrace + breadcrumbs + user context. Engineers see a single dashboard of "what's broken in production right now".

**Why we call it.**
- *Pull issues* — we fetch the list of unresolved errors so the pipeline knows what to fix. Endpoint: `GET /api/0/projects/{org}/{project}/issues/`.
- *Get full stacktrace* — the issue list endpoint gives summaries; we need a per-issue call to get the actual stacktrace frames. Endpoint: `GET /api/0/issues/{issue_id}/`.
- *Mark as resolved* — when the LLM filter rejects an issue as "not a real bug", we resolve it on Sentry so it doesn't keep notifying us. Endpoint: `PUT /api/0/issues/{issue_id}/`.
- *Receive webhooks* — Sentry can call our `POST /webhook/sentry` whenever a new issue or alert fires. We didn't write this; we configured it in the Sentry UI.

**Token.** `SENTRY_TOKEN` in `.env`. Get one from Sentry → Settings → Auth Tokens.

**Webhook signing secret.** `SENTRY_CLIENT_SECRET`. Used to verify that an incoming webhook actually came from Sentry (HMAC-SHA256 over the request body). Optional but recommended for production.

---

## 2. OpenAI — `llm/claude.py` (yes, the file is misnamed)

**What it is.** OpenAI hosts large language models (GPT-4o, GPT-4o-mini, etc.) behind a paid API. You send a prompt, you get text back. You pay per token (≈ per word).

**Why we call it.**
- *Filter step* — classify which Sentry issues are real application bugs vs noise.
- *Patch generation* — given an error + stacktrace + relevant source code, produce a JSON list of file edits that fix the bug.
- *Behavioral test generation* — write a Jest+RTL test that exercises the broken user flow.
- *Test repair* — if a generated test fails to compile, ask the LLM to fix the test itself.

**The class is named `ClaudeLLM` because** an earlier version of the project pointed at Anthropic's Claude. The class kept its name even after we switched to OpenAI's SDK; the OpenAI SDK is what's actually used today. The `OPENAI_BASE_URL` setting lets you point it at any OpenAI-compatible endpoint (e.g. Azure OpenAI, a local Ollama server) without changing code.

**Two methods:**
- `chat(system, user)` → raw string. For freeform prose.
- `chat_json(system, user)` → parsed Python dict. Uses OpenAI's `response_format: json_object` mode, which forces the model to return valid JSON.

**Token.** `OPENAI_API_KEY` in `.env`. Format: `sk-...`.

**Cost note.** Every pipeline run makes multiple LLM calls. At `gpt-4o-mini` prices it's cents per run, but a misconfigured loop can cost real money fast. The `max_retries` and `max_issues` per-project caps exist to bound the worst case.

---

## 3. GitHub — `services/github_service.py`

**What it is.** GitHub hosts our git repositories and offers a REST API for things like creating PRs.

**Why we call it.**
This service does **two different things** that just happen to live in one file:

### 3.1 The `git` CLI (subprocess)
Branching, committing, pushing — these use the `git` command-line tool installed on the server, invoked via Python's `subprocess` module. Not an HTTP API. The `git` binary itself authenticates over HTTPS using the token embedded in the remote URL.

```
git clone https://x-access-token:{TOKEN}@github.com/owner/repo.git
```

That's why the token has to be passed in — the OS-level git command needs it for `git push`.

### 3.2 The GitHub REST API (PyGithub)
The PR creation step uses **PyGithub**, a Python client for the GitHub API. It's purely HTTPS — no shell. Used to call `POST /repos/{owner}/{repo}/pulls` with the title and body.

**Token.** `GITHUB_TOKEN` in `.env`. Format: `ghp_...` (classic) or `github_pat_...` (fine-grained). It needs `repo` scope to push branches and open PRs.

---

## 4. Jira — `services/jira_service.py`

**What it is.** Atlassian Jira is a project management / issue tracker. Engineers track work as "issues" (tickets) inside a "project". Each ticket has a key like `CSPI-2014`.

**Why we call it.**
- *Create one ticket per fixed Sentry issue* — so QA / product / engineering have a record of what was auto-fixed and can review the PR. Endpoint: `POST /rest/api/3/issue`.

**Auth.** Two values together act as the password: `JIRA_EMAIL` (your account email) and `JIRA_API_TOKEN` (a personal access token from Atlassian). They're combined with HTTP Basic auth.

**Domain.** `JIRA_DOMAIN` (e.g. `yourcompany.atlassian.net`) — the Jira tenant URL.

**Project key.** Per-project: `jira_project_key` in `projects/<name>.json` (e.g. `CSPI`).

If any of these are missing, the Jira step is skipped silently — the rest of the pipeline still runs.

---

## How requests look on the wire

For each external system, the actual HTTP requests look like:

| System | Method + URL pattern | Auth header |
|---|---|---|
| Sentry | `GET https://sentry.io/api/0/projects/{org}/{slug}/issues/` | `Authorization: Bearer {SENTRY_TOKEN}` |
| OpenAI | `POST https://api.openai.com/v1/chat/completions` | `Authorization: Bearer {OPENAI_API_KEY}` |
| GitHub | `POST https://api.github.com/repos/{owner}/{repo}/pulls` | `Authorization: Bearer {GITHUB_TOKEN}` |
| Jira | `POST https://{domain}/rest/api/3/issue` | `Authorization: Basic base64({email}:{token})` |

You don't write these manually — the SDKs (`requests`, `openai`, `PyGithub`, `atlassian-python-api`) handle the formatting. But knowing what the wire actually carries makes debugging a 401 much easier.

---

## Why this design (services, not "just call requests inline")

A "service" wrapper exists for one reason: **the pipeline shouldn't know HTTP**. Look at this line in `pipeline/issue_fetcher.py`:

```python
issues = sentry.get_issues(query=query)
```

Compare the alternative:

```python
import requests
resp = requests.get(
    f"https://sentry.io/api/0/projects/{org}/{slug}/issues/",
    headers={"Authorization": f"Bearer {token}"},
    params={"query": query},
)
resp.raise_for_status()
issues = resp.json()
```

The second version puts URL formatting, auth headers, error handling, JSON parsing, and pagination logic into every place that calls Sentry. A bug in any of them is fixed in one place, not ten. The wrapper keeps the pipeline readable.

This is called **separation of concerns**. It's the single most important architectural idea in the codebase.

---

Next: [06-glossary.md](./06-glossary.md) — every jargon term defined in plain English.
