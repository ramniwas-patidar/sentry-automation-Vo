# 03 — Server Lifecycle: What Happens When the Server is Running

This page narrates what is *physically* happening on your computer (or on the deployment host) from the moment you press Enter on `python main.py` until you press `Ctrl+C` to stop it.

If you've never run a long-lived server process before, this is the page for you.

---

## The big idea

A "server" is just a Python program that **never exits on its own**. It opens a network port, listens for incoming connections, and handles them. It keeps running until something kills it.

Think of it like a shop: the moment you "open" the shop, it sits there waiting. Customers come in, you serve them, they leave, you wait again. That's the whole job.

---

## Phase 1 — Boot (≈ 0–2 seconds)

You run:

```bash
python main.py
```

Here's what happens, in order:

### 1.1 Python starts
The Python interpreter loads. It opens `main.py` and starts executing top-to-bottom.

### 1.2 `main.py` runs `logging.basicConfig(...)`
This sets up the most basic console logger so any *very* early errors (before our own logging is configured) still appear.

### 1.3 The `if __name__ == "__main__":` block fires
This is the entry-point guard from [Python Basics §12](./01-python-basics.md). It calls:

```python
uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
```

### 1.4 uvicorn imports `server`
To find the `app` variable, uvicorn must import the `server` package. That triggers `server/__init__.py` to run **top to bottom**.

This is when the heavy lifting happens. In order:

1. **Logging is upgraded.** Console + rotating file handler attached to the root logger. From now on every log line lands in `logs/sentry-automation.log`.
2. **All sub-modules are imported** — `models.schemas`, `project_store`, every `services/*` file, every `pipeline/*` file. Each one runs its own top-level code (mostly `import` statements and class definitions — no real work yet).
3. **The FastAPI `app` object is created** with `app = FastAPI(...)`.
4. **The scanner-block middleware is registered** (`app.add_middleware(ScannerBlockMiddleware)`).
5. **Endpoint decorators (`@app.post(...)`, `@app.get(...)`) execute** — each registers a URL → function mapping inside `app`'s internal routing table. **The endpoint functions themselves do not run yet.**
6. Module-level dictionaries are created: `_repo_locks`, `_last_webhook_trigger`. These are empty and live in the process's RAM as long as the server runs.

### 1.5 uvicorn binds the port

Now uvicorn opens TCP port `8000` on `127.0.0.1` and starts a network listener. If port 8000 is already in use you'll get an error here ("Address already in use") and the process exits.

You see something like:
```
INFO: Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

The shop is open.

---

## Phase 2 — Idle (the 99% of the time)

When no requests are coming in, the server is **doing literally nothing**. Specifically:

- The Python process is alive but blocked inside the OS `epoll`/`kqueue` syscall waiting for socket activity.
- CPU usage ≈ 0%.
- RAM usage ≈ stable (whatever was allocated during boot — typically 80–150 MB).
- The two module-level dicts (`_repo_locks`, `_last_webhook_trigger`) keep their state across requests. **This is in-process state — if you restart the server, it's wiped.**

This is normal. Servers spend most of their life idle. The cost of "running 24/7" is negligible until traffic arrives.

---

## Phase 3 — A request arrives

Two URLs matter in practice: `/webhook/sentry` (the automatic one) and `/pipeline/run` (the manual one). Both do the same work but with different timing models.

### 3.1 The TCP layer (handled for you by uvicorn)

A TCP connection comes in on port 8000. uvicorn:
- accepts the connection,
- reads the HTTP request bytes,
- parses them into a `Request` object,
- looks at the URL and method (e.g. `POST /webhook/sentry`),
- looks up which Python function is registered for that URL.

You don't write any of this — it's all uvicorn + FastAPI plumbing.

### 3.2 Middleware runs

Before your endpoint sees the request, the `ScannerBlockMiddleware` from `server/__init__.py` runs:

```python
class ScannerBlockMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path.lower()
        if any(path.endswith(ext) for ext in BLOCKED_EXTENSIONS) or \
           any(blocked in path for blocked in BLOCKED_PATHS):
            return Response(status_code=404)
        return await call_next(request)
```

This is a **bouncer at the door**. Vulnerability scanners on the public internet constantly probe URLs like `/wp-admin.php`, `/.env`, `/vendor/phpunit/`. Returning 404 immediately for those paths saves CPU and avoids logging noise. Legitimate URLs pass straight through to `call_next(request)`.

### 3.3 The endpoint function runs

For `/webhook/sentry`, that's `sentry_webhook(request)`. The function:

1. Reads the raw request body.
2. Verifies the HMAC signature against `SENTRY_CLIENT_SECRET` (if set).
3. Parses the JSON.
4. Decides what kind of event it is (`issue.created`, `error.created`, `event_alert.triggered`).
5. Looks up the project config by Sentry slug.
6. Checks the **debounce** clock — if this project was triggered in the last 30 seconds, it returns `"debounced"` and exits.
7. Checks the **per-repo lock** — if a pipeline is already running for this repo, returns `"skipped"`.
8. Spawns a **background thread** running `_run_in_background()`.
9. Returns `{"status": "triggered"}` to Sentry **immediately** — within ~50ms.

Sentry is happy: it got a 200 OK fast. Meanwhile, the actual work has just begun in a different thread.

### 3.4 The pipeline runs (in a thread)

The thread runs `_execute_pipeline(req)`, which is the 7-step orchestrator. This typically takes **30 seconds to 5 minutes** depending on how many issues there are and how many LLM calls happen.

See [04-pipeline-walkthrough.md](./04-pipeline-walkthrough.md) for an exhaustive trace of one run.

---

## Phase 4 — Concurrency: what if two requests arrive at once?

This actually happens — Sentry can fire several webhooks within a second when an error storms.

The server has **two safety nets**:

### 4.1 The debounce dictionary

`_last_webhook_trigger` records the wall-clock time of the last accepted webhook *per project*. If the next webhook for the same project arrives within `WEBHOOK_COOLDOWN_SECONDS` (default 30s), it's discarded with status `"debounced"`. **No pipeline run, no thread, no LLM cost.**

### 4.2 The per-repo lock

`_repo_locks` is a dictionary `{repo_url → threading.Lock}`. Before any pipeline starts, we try to `lock.acquire(blocking=False)`. If the lock is already held (because a previous pipeline is still mid-run), we bail out with `"skipped"`.

This protects the local git repo: two concurrent pipelines could create conflicting branches, half-commits, push races, etc. The lock makes it impossible.

Two webhooks for **different projects** run in parallel just fine — different repos, different locks.

---

## Phase 5 — Long-running pipeline, what's happening on the host?

While the pipeline thread runs, the server is **still serving other requests**. The webhook handler returned 200 already, and FastAPI is happy to accept more requests on the main thread.

Meanwhile in the background thread, you'll see lots of activity:

| What | Where you see it |
|---|---|
| HTTPS requests to Sentry | `[SENTRY]` log lines, network traffic |
| HTTPS requests to OpenAI (patch + test generation) | `[LLM]` log lines, possibly slow (OpenAI can take 5–30s per call) |
| `git fetch`, `git checkout`, `git commit`, `git push` | `[GIT]` log lines, plus the local repo on disk changes |
| `npm test` (if `test_command` is set) | `[GIT]` log line plus the test runner's output appended |
| HTTPS requests to GitHub (PR creation) | `[GITHUB]` log line |
| HTTPS requests to Jira | `[JIRA]` log line |

All of this is interleaved with `[PIPELINE]` step markers. Tail the log file in another terminal:

```bash
tail -f logs/sentry-automation.log
```

…and you can watch the whole thing happen in real time.

---

## Phase 6 — Reload (development only)

Notice this in `main.py`:

```python
uvicorn.run("server:app", ..., reload=True)
```

`reload=True` means uvicorn watches every `.py` file in the project and **restarts itself automatically** when you save a change. Great for development. **In production you would set this to `False`** because (a) it watches files for nothing and (b) it would interrupt in-flight pipeline runs on every save.

---

## Phase 7 — Shutdown

Press `Ctrl+C`. uvicorn intercepts the signal, stops accepting new connections, and exits the Python process. Any **in-flight pipeline thread is killed mid-run** because it was a daemon thread — the OS reclaims its memory and any open file handles.

That's why we never store important state in process memory: a restart wipes `_repo_locks` and `_last_webhook_trigger`. The next webhook starts with a clean slate. (The debounce loss is harmless — at worst you process the same issue twice.)

The local git repo on disk persists across restarts. The only cleanup the pipeline does on a crash is `github.cleanup(branch_name)` in the `except` block — which deletes the temp branch so a half-finished run doesn't leave junk behind.

---

## Diagram — one webhook, end to end

```
Sentry servers          your machine                          OpenAI / GitHub / Jira
     │                       │
     │  POST /webhook/sentry │
     ├──────────────────────►│  uvicorn accepts
     │                       │   ↓
     │                       │  ScannerBlockMiddleware (passes)
     │                       │   ↓
     │                       │  sentry_webhook()
     │                       │   ↓
     │                       │  verify HMAC, parse JSON
     │                       │   ↓
     │                       │  debounce check (skip if recent)
     │                       │   ↓
     │                       │  per-repo lock check
     │                       │   ↓
     │                       │  spawn thread ──────┐
     │   200 {"triggered"}   │                     │
     │◄──────────────────────│                     │
     │                       │                     ▼
     │                       │            _execute_pipeline()
     │                       │                     │
     │                       │   Step 1 — fetch  ──┼──────► Sentry API
     │                       │   Step 2 — branch ──┘
     │                       │   Step 3 — TDD fix loop ───► OpenAI (×N)
     │                       │   Step 4 — run tests
     │                       │   Step 5 — commit/push/PR ─► GitHub
     │                       │   Step 6 — create tickets ─► Jira
     │                       │   ↓
     │                       │  thread exits, lock released
     │                       │  server returns to idle
```

---

Next: [04-pipeline-walkthrough.md](./04-pipeline-walkthrough.md) — go inside the 7 steps.
