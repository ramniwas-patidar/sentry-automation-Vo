# 01 — Python Basics You Need to Read This Codebase

You don't need to be a Python expert. You need to recognise about a dozen patterns. This page explains each one with examples taken straight from this project.

---

## 1. Modules and imports

A `.py` file is a **module**. A folder with an `__init__.py` file inside it is a **package** (a folder of modules). Importing is how one file uses code from another.

```python
# server/__init__.py
from config import settings                     # imports the `settings` object from config.py
from services.sentry_service import SentryService  # imports the SentryService class from services/sentry_service.py
```

Why does `server/__init__.py` exist? Because importing the *folder* `server` actually runs the code in `server/__init__.py`. That's the convention Python uses to make a folder behave like a single module.

In `main.py` you'll see:

```python
uvicorn.run("server:app", ...)
```

That string `"server:app"` means: *"go find the `server` package, run its `__init__.py`, and use the variable named `app` from it"*. So `app` (our FastAPI server) lives in `server/__init__.py`.

---

## 2. Virtual environments (`.venv` / `venv`)

Python lets you install libraries (`pip install something`). To avoid polluting your global system, you create an isolated folder called a **virtual environment** that holds project-specific libraries.

You'll see two of them in the repo: `.venv/` and `venv/`. They're just sandbox folders — never edit anything inside. They are listed in `.gitignore` for a reason: they're machine-specific, not part of the code.

```bash
python -m venv .venv          # create it
source .venv/bin/activate     # "step into" it — pip now installs locally
pip install -r requirements.txt
```

`requirements.txt` is just a text list of libraries this project needs.

---

## 3. Type hints

Python is dynamically typed but supports **optional annotations** to document what a variable should be. They are **not enforced at runtime** — they're documentation + tooling hints.

```python
def _get_repo_lock(repo_path: str) -> threading.Lock:
    ...
```

Read this as: "this function takes a `str` and returns a `threading.Lock`". If you pass a number, Python won't crash — but your editor will warn you.

You'll also see:

```python
_repo_locks: dict[str, threading.Lock] = {}
```

= "a dictionary whose keys are strings and whose values are Locks". Again, just documentation.

---

## 4. Decorators (the `@something` lines)

A decorator is a function that wraps another function. The `@` syntax is sugar for "wrap this".

```python
@app.post("/webhook/sentry")
async def sentry_webhook(request: Request):
    ...
```

The decorator `@app.post("/webhook/sentry")` tells FastAPI: "*when an HTTP POST request arrives at `/webhook/sentry`, call this function*." Without the decorator, the function is just a normal function nobody calls.

Decorators are how FastAPI maps URLs to Python functions.

---

## 5. `async def` and `await`

Some functions are declared `async def`. These are **coroutines** — special functions that can pause and let other work happen while waiting on slow I/O (network, file).

```python
async def sentry_webhook(request: Request):
    body = await request.body()   # pause here while we read the request body
```

You only need `await` inside an `async def` function. FastAPI handles the rest.

Most of this codebase is **synchronous** (regular `def`). Only the webhook handler is async, because reading the HTTP request body is async.

---

## 6. Threads

When the webhook runs the pipeline, we don't want to make Sentry wait 5 minutes for a reply. We start the work in the **background** using a thread.

```python
def _run_in_background():
    ...

thread = threading.Thread(target=_run_in_background, daemon=True)
thread.start()
return {"status": "triggered"}   # respond to Sentry immediately
```

A **thread** is an extra worker that runs alongside the main program. `daemon=True` means: "if the main program exits, kill this thread too — don't keep the process alive just for it."

### Locks
Threads can step on each other if they touch the same data. A **Lock** is a flag that says "only one thread at a time inside this block":

```python
lock = _get_repo_lock(req.project.github_repo)
if not lock.acquire(blocking=False):
    raise HTTPException(409, "A pipeline run is already in progress for this repo")
try:
    ...do work...
finally:
    lock.release()
```

This prevents two pipelines from corrupting the same git repo at the same time.

---

## 7. `try` / `except` / `finally` — error handling

```python
try:
    risky_thing()
except SomeError as e:
    handle(e)
finally:
    cleanup()    # runs whether or not an error happened
```

You'll see this everywhere in `_execute_pipeline()` — every step is wrapped so that one failure doesn't crash the whole server.

---

## 8. Pydantic models (`models/schemas.py`)

Pydantic is a library that turns Python classes into **validated data shapes**.

```python
class ProjectConfig(BaseModel):
    sentry_org: str
    sentry_project: str
    base_branch: str = "main"  # default value
    max_retries: int = 3
```

When FastAPI receives a JSON request body, it automatically parses it into one of these models. If a required field is missing or the wrong type, FastAPI rejects the request with a 422 error before your code runs.

This is why endpoints look so clean:

```python
def run_pipeline(req: PipelineRequest):
    # req.project is already a fully-validated ProjectConfig — no manual parsing needed
```

---

## 9. f-strings

```python
logger.info(f"[PIPELINE] Issue {idx+1}/{len(relevant_issues)}: #{issue.id}")
```

The `f"..."` prefix lets you embed expressions inside `{}`. Very common everywhere.

---

## 10. Context managers (`with` blocks)

```python
with _lock_manager:
    ...
```

`with` automatically acquires a resource at the start of the block and releases it at the end (even on error). Used here to safely take and release a lock.

---

## 11. List comprehensions

```python
files = [e.get("filepath", "") for e in edits]
```

Reads as: "make a new list, where each element is `e.get("filepath", "")` for each `e` in `edits`". A compact way to write loops.

---

## 12. The `if __name__ == "__main__":` guard

```python
if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
```

This block only runs when you execute the file directly (`python main.py`). It's skipped if some other module imports this file. Standard Python idiom for "this is the entry point".

---

## 13. Logging

We never use `print()` for diagnostics. We use the **logging** module:

```python
logger = logging.getLogger(__name__)
logger.info("[PIPELINE] Starting...")
logger.error("[PIPELINE] Something broke")
logger.exception("Unexpected error")  # logs the traceback automatically
```

Why? Because logging output goes to BOTH the console AND `logs/sentry-automation.log`, gets timestamped, and gets rotated when files grow large. `print()` doesn't.

---

## What you can safely skip for now

- `hmac`, `hashlib` — these are only used to verify webhook signatures (cryptography). Treat the verification block as a black box.
- `re` (regex) — only used in one place to parse a URL.
- `__pycache__/` folder — auto-generated bytecode cache. Ignore.

---

Once you're comfortable with these patterns, head to [02-architecture.md](./02-architecture.md) to see how the files fit together.
