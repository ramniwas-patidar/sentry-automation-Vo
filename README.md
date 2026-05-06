# Sentry Automation Pipeline

An automated bug-fixing system that listens to Sentry webhooks, uses an LLM (OpenAI GPT) to generate code fixes, verifies them with tests, opens a GitHub PR, and creates a Jira ticket — all without human intervention.

---

## How It Works (Big Picture)

```
Sentry Webhook
      │
      ▼
 FastAPI Server  ──► Look up project config (projects/*.json)
      │
      ▼
 Pipeline (7 steps)
  1. Fetch Issues      ← Sentry API
  2. LLM Filter        ← GPT decides which issues are real app bugs
  3. Create Git Branch ← git checkout -b fix/sentry-...
  4. TDD Fix Loop      ← GPT generates fix + tests, verifies pre/post
  5. Run Tests         ← project's test_command (if configured)
  6. Commit + PR       ← GitHub API
  7. Jira Tickets      ← Jira REST API
```

---

## Project Structure

```
sentry-automation-Vo/
├── main.py                  # Entry point — starts uvicorn on port 8000
├── run.py                   # Alternative runner with extra config
├── config.py                # Global settings loaded from .env
├── project_store.py         # Loads per-project JSON configs from projects/
│
├── server/__init__.py       # FastAPI app, all HTTP endpoints, pipeline orchestration
│
├── pipeline/
│   ├── issue_fetcher.py     # Fetches & deduplicates issues from Sentry
│   ├── issue_filter.py      # LLM-based triage (relevant vs noise)
│   ├── issue_processor.py   # Core fix loop: generate patch → test → apply
│   ├── test_generator.py    # Builds deterministic + behavioral tests
│   ├── pr_creator.py        # Commits, pushes, creates GitHub PR
│   └── jira_creator.py      # Creates Jira tickets for fixed issues
│
├── services/
│   ├── sentry_service.py    # Sentry REST API client
│   ├── github_service.py    # Git CLI wrapper + GitHub API (PyGithub)
│   ├── jira_service.py      # Jira REST API client
│   └── llm_service.py       # LLM factory (returns ClaudeLLM singleton)
│
├── llm/
│   └── claude.py            # OpenAI client wrapper (chat + chat_json)
│
├── models/
│   └── schemas.py           # All Pydantic models (request/response shapes)
│
├── projects/
│   ├── wellversed.json      # Config for the "wellversed-prod" Sentry project
│   └── aqualogica.json      # Config for another project
│
└── logs/
    └── sentry-automation.log  # Rotating log file (10 MB × 5 backups)
```

---

## Key Concepts

### ProjectConfig (`models/schemas.py`)
Every project is described by a JSON file in `projects/`. The pipeline is entirely driven by this config — no hardcoded project details anywhere.

| Field | Required | Description |
|---|---|---|
| `sentry_org` | ✅ | Sentry organization slug |
| `sentry_project` | ✅ | Sentry project slug (e.g. `wellversed-prod`) |
| `sentry_project_id` | optional | Numeric Sentry project ID (for alert webhooks) |
| `github_repo` | ✅ | `owner/repo` on GitHub |
| `base_branch` | ✅ | Branch to create PRs against (default: `main`) |
| `repo_path` | optional | Local path to repo. If omitted, repo is auto-cloned |
| `jira_project_key` | optional | Jira project key (e.g. `CSPI`) |
| `test_command` | optional | Shell command to run tests (e.g. `npm test`) |
| `max_retries` | optional | LLM fix attempts per issue (default: 3) |
| `max_issues` | optional | Max issues to process per run (default: 25) |

Example (`projects/wellversed.json`):
```json
{
  "sentry_org": "primathon-10",
  "sentry_project": "wellversed-prod",
  "sentry_project_id": "4510900739112960",
  "github_repo": "primathontech/wellversed",
  "base_branch": "wellversed-master",
  "jira_project_key": "CSPI",
  "test_command": "",
  "max_retries": 3
}
```

---

## Setup

### 1. Install dependencies
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure `.env`
```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini

SENTRY_TOKEN=sntrys_...
GITHUB_TOKEN=ghp_...

# Optional — Jira defaults (can be overridden per project)
JIRA_DOMAIN=yourcompany.atlassian.net
JIRA_EMAIL=you@company.com
JIRA_API_TOKEN=...

# Optional — Sentry webhook signature verification
SENTRY_CLIENT_SECRET=...

# Tuning
WEBHOOK_COOLDOWN_SECONDS=30
PROJECTS_DIR=projects
LOG_DIR=logs
```

### 3. Add a project config
Create `projects/<your-project>.json` (see ProjectConfig fields above).

### 4. Start the server
```bash
python main.py
# Server runs at http://127.0.0.1:8000
```

---

## API Endpoints

### `POST /pipeline/run`
Manually trigger the full pipeline for a project.

**Request body:**
```json
{
  "project": { ...ProjectConfig fields... },
  "query": "is:unresolved",
  "issue_id": "optional-specific-issue-id",
  "dry_run": false
}
```

**Response:** `PipelineResponse` — status, counts, PR URL, Jira tickets, per-issue results.

---

### `POST /webhook/sentry`
Receives Sentry webhook events and triggers the pipeline in a background thread.

Supported events:
- `issue.created` — new Sentry issue
- `error.created` — new error event
- `event_alert.triggered` — alert rule fired

**Debounce:** Each project has a cooldown (default 30s). Repeated webhooks within the cooldown are ignored to prevent duplicate runs.

**Concurrency:** A per-repo lock prevents two pipeline runs from modifying the same repo simultaneously.

Configure in Sentry → Settings → Developer Settings → Internal Integration → Webhooks.

---

### `GET /projects`
Lists all registered project configs.

### `GET /webhook/status`
Shows debounce state for all projects (last triggered, cooldown remaining).

---

## Pipeline Steps (Detail)

### Step 1 — Fetch Issues (`issue_fetcher.py`)
- Calls `SentryService.get_issues()` with the configured query (default: `is:unresolved`)
- Paginates through all results
- Deduplicates issues with identical titles (keeps highest event count)
- Enriches each issue with full stacktrace via `get_issue_details()`
- **TEST_MODE_FETCH_ONE = True** — currently hardcoded to fetch only 1 issue per run (flip to `False` for full pagination)

### Step 2 — LLM Filter (`issue_filter.py`)
- Sends issue summaries to GPT in batches of 20
- GPT classifies each as `relevant` or not, with a category and reason
- Categories: `application_bug`, `third_party`, `hydration`, `infrastructure`, `browser_extension`, `bot_traffic`, `stale`, `other`
- Filtered-out issues are marked as resolved in Sentry
- **DISABLE_FILTER = True** — currently hardcoded to skip filtering and treat all issues as relevant

### Step 3 — Create Git Branch
- Fetches latest from origin, checks out base branch, pulls
- Creates a new branch: `fix/sentry-{N}issues-{timestamp}`

### Step 4 — TDD Fix Loop (`issue_processor.py`)
For each issue, up to `max_retries` times:

1. **Generate patch** — GPT receives the error title, stacktrace, relevant source files, and file tree. Returns a list of `file_edits` (filepath + original snippet + replacement).
2. **Build deterministic test** — a simple file-content substring check to confirm the fix was applied.
3. **Build behavioral test** — GPT generates a Jest+RTL test that exercises the user flow affected by the bug.
4. **Pre-fix test run** — both tests should FAIL (confirming the bug exists).
5. **Apply fix** — each `file_edit` is applied by finding the exact `original` string in the file and replacing it.
6. **Post-fix test run** — both tests should PASS (confirming the fix works).
7. **Repair loop** — if the behavioral test fails due to a setup/compile error (not a logic failure), GPT attempts to repair the test up to 2 times.

The fix is accepted even if tests are unverified. `verified = True` only when the behavioral test fails pre-fix and passes post-fix.

### Step 5 — Run Tests
Runs `project.test_command` in the repo directory. If it fails, the branch is cleaned up and the pipeline returns `failed`. Skipped if `test_command` is empty.

### Step 6 — Commit + PR (`pr_creator.py`)
- Commits all changes with a message listing every fixed issue
- Pushes the branch to GitHub
- Creates a PR via PyGithub with a detailed description including test verification tables

### Step 7 — Jira Tickets (`jira_creator.py`)
- Creates one Jira ticket per fixed issue (skipped if Jira is not configured)
- Ticket includes: error title, files changed, confidence score, test verification details, stacktrace, PR link

---

## LLM Integration (`llm/claude.py`)

Despite the class name `ClaudeLLM`, this uses the **OpenAI Python SDK** pointed at the model configured in `OPENAI_MODEL` (default: `gpt-4o-mini`). It can be pointed at any OpenAI-compatible endpoint.

Two methods:
- `chat(system_prompt, user_message, ...)` → raw string response
- `chat_json(system_prompt, user_message, ...)` → parsed dict (uses `response_format: json_object`)

---

## Data Flow Diagram

```
.env + projects/*.json
        │
        ▼
   config.py (Settings)
   project_store.py (ProjectConfig)
        │
        ▼
   server/__init__.py  ◄── HTTP: /pipeline/run or /webhook/sentry
        │
        ├── SentryService    → fetch issues + stacktraces
        ├── GitHubService    → clone/branch/commit/push/PR
        ├── JiraService      → create tickets
        │
        ▼
   pipeline/
     issue_fetcher.py   → list[SentryIssue]
     issue_filter.py    → (relevant, filtered, details)
     issue_processor.py → IssueFixResult (per issue)
       └── test_generator.py → TestResult (det + behavioral)
     pr_creator.py      → PR URL
     jira_creator.py    → list[ticket URLs]
        │
        ▼
   PipelineResponse (JSON)
```

---

## Logs

All modules log to both console and `logs/sentry-automation.log` (rotating, 10 MB × 5 files).

Log prefixes by module:
| Prefix | Module |
|---|---|
| `[PIPELINE]` | server/__init__.py orchestration |
| `[WEBHOOK]` | webhook handler |
| `[FETCHER]` | issue_fetcher.py |
| `[FILTER]` | issue_filter.py |
| `[PROCESSOR]` | issue_processor.py |
| `[GIT]` | github_service.py git ops |
| `[GITHUB]` | github_service.py API ops |
| `[SENTRY]` | sentry_service.py |
| `[JIRA]` | jira_service.py |
| `[LLM]` | llm/claude.py |

---

## Current Test Mode Flags

Two flags are hardcoded in the source to limit scope during development:

| Flag | File | Effect |
|---|---|---|
| `TEST_MODE_FETCH_ONE = True` | `pipeline/issue_fetcher.py` | Only fetches 1 issue per run |
| `DISABLE_FILTER = True` | `pipeline/issue_filter.py` | Skips LLM filtering, all issues treated as relevant |

Set both to `False` for full production behaviour.

---

## Adding a New Project

1. Create `projects/myproject.json`:
```json
{
  "sentry_org": "my-org",
  "sentry_project": "my-project-slug",
  "github_repo": "myorg/myrepo",
  "base_branch": "main",
  "jira_project_key": "PROJ",
  "test_command": "npm test -- --watchAll=false",
  "max_retries": 3
}
```

2. Restart the server (or it will be picked up on the next webhook).

3. In Sentry, configure a webhook pointing to `https://your-server/webhook/sentry` with `issue` and `error` events enabled.
