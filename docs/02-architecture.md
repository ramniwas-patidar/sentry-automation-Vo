# 02 — Architecture: How the Files Fit Together

This page maps the code. After reading it you should be able to open any file in the repo and know roughly what role it plays.

---

## The 5-second mental model

```
                          ┌──────────────────┐
                          │   main.py        │  ← you start this
                          └────────┬─────────┘
                                   │ launches
                                   ▼
                          ┌──────────────────┐
                          │   uvicorn        │  ← a generic web server
                          └────────┬─────────┘
                                   │ runs
                                   ▼
                          ┌──────────────────┐
                          │  server/         │  ← FastAPI app — defines URLs
                          │  __init__.py     │
                          └────────┬─────────┘
                                   │ on a request, calls
                                   ▼
                          ┌──────────────────┐
                          │  pipeline/*      │  ← business logic, 7 steps
                          └────────┬─────────┘
                                   │ uses
                                   ▼
                          ┌──────────────────┐
                          │  services/*      │  ← talks to Sentry, GitHub, Jira
                          │  llm/claude.py   │     and to the LLM
                          └──────────────────┘
```

Top of the stack = closest to the network. Bottom = closest to the outside world (third-party APIs).

---

## Layer-by-layer tour

### Layer 0 — Entry points

| File | Purpose |
|---|---|
| `main.py` | The 12-line launcher. Configures basic logging and calls `uvicorn.run("server:app", ...)`. This is what you run with `python main.py`. |
| `run.py` | An alternative launcher with extra environment validation and prettier startup output. Same end result. |

### Layer 1 — Configuration

| File | Purpose |
|---|---|
| `.env` | Secrets (API tokens). **Never commit this file.** |
| `config.py` | Reads `.env` into a `Settings` object using Pydantic. Anywhere in the code you can just `from config import settings` and read `settings.OPENAI_API_KEY`. |
| `projects/*.json` | One file per project we automate. Tells the pipeline what Sentry org / GitHub repo / Jira project to use. |
| `project_store.py` | Helper that loads + parses every JSON file in `projects/` into `ProjectConfig` objects. |

The split is deliberate: `.env` holds **secrets** (one set per machine); `projects/*.json` holds **per-project config** (committed to git, shared across the team).

### Layer 2 — The web layer

| File | Purpose |
|---|---|
| `server/__init__.py` | Defines the FastAPI `app`, every URL endpoint, the webhook security middleware, the per-repo lock dictionary, the debounce dictionary, and the master `_execute_pipeline()` function that orchestrates the 7 pipeline steps. |

This is the busiest file in the repo. It does three jobs:

1. **Receive HTTP requests** (via `@app.post(...)` decorators).
2. **Validate the request** (Pydantic models).
3. **Dispatch to the pipeline** (`_execute_pipeline()`).

### Layer 3 — Pipeline (the 7 steps)

Each pipeline step is a separate file under `pipeline/`. They are pure functions — they take inputs, return outputs, never store state in module-level variables.

| File | Step it implements |
|---|---|
| `issue_fetcher.py` | Step 1 — Pull issues from Sentry. |
| `issue_filter.py` | Step 2 — Ask the LLM "is this a real bug?". |
| `issue_processor.py` | Step 4 — The TDD fix loop (the brain of the project). Calls `test_generator.py` to build tests. |
| `test_generator.py` | Builds two kinds of tests: a deterministic file-content check, and a Jest+RTL behavioral test. |
| `pr_creator.py` | Step 6 — Commit, push, open PR. |
| `jira_creator.py` | Step 7 — Create Jira tickets. |

(Steps 3 and 5 — git branch creation and test runs — live inside `services/github_service.py` because they're thin git operations.)

### Layer 4 — Services (one per external system)

A "service" is a thin wrapper around someone else's API. It hides the messy HTTP/auth details from the pipeline.

| File | Wraps |
|---|---|
| `services/sentry_service.py` | Sentry REST API — fetch issues, get stacktraces, mark resolved. |
| `services/github_service.py` | The `git` CLI (subprocess calls) **and** the GitHub REST API (via PyGithub). |
| `services/jira_service.py` | Jira REST API — create tickets, attach metadata. |
| `services/llm_service.py` | A factory that returns a singleton LLM client. |
| `llm/claude.py` | The actual LLM client. **Despite the name, it uses the OpenAI SDK** (because we point it at OpenAI's `gpt-4o-mini` by default). The class is called `ClaudeLLM` for historical reasons. |

### Layer 5 — Data shapes

| File | Purpose |
|---|---|
| `models/schemas.py` | **Every** structured data object the project uses: `ProjectConfig`, `SentryIssue`, `IssueFixResult`, `PipelineRequest`, `PipelineResponse`, `StepResult`, `TestResult`, `PatchResult`, etc. All Pydantic models. |

When you're confused about what fields an object has, this file is your dictionary.

---

## Why split it this way?

This is a **layered architecture**. The rules are:

1. A higher layer can call a lower layer. **Never the reverse.**
2. The web layer (`server/`) does NOT know how to talk to GitHub. It calls `pipeline/`, which calls `services/`, which calls GitHub.
3. A service knows nothing about the pipeline or the web. It just knows about its one external system.

Why bother? Because if Sentry changes their API, you only edit `services/sentry_service.py`. If the pipeline order changes, you only edit `server/__init__.py`. Each file has a single reason to change.

---

## Things that look weird until you know

- **`__init__.py` files that are empty.** They exist to mark a folder as a Python package. Empty is fine.
- **`logs/sentry-automation.log` keeps reappearing.** It's auto-created on startup. Listed in `.gitignore`.
- **`__pycache__/` folders everywhere.** Python's bytecode cache. Auto-generated, gitignored, safe to delete.
- **Two virtualenvs (`.venv` and `venv`).** Historical — only one is needed. Either works.
- **`projects/wellversed.json` and `aqualogica.json` are committed.** Yes — they contain *configuration*, not secrets. The actual Sentry/GitHub tokens come from `.env`.

---

Next: [03-server-lifecycle.md](./03-server-lifecycle.md) — what literally happens, second by second, when you run `python main.py`.
