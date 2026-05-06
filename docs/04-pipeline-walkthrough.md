# 04 — Pipeline Walkthrough: A Webhook's Journey

This page traces a single Sentry webhook from arrival to "PR opened on GitHub". It complements the [server lifecycle doc](./03-server-lifecycle.md) by zooming into the 7 pipeline steps.

---

## Setup: a typical scenario

- A user on the `wellversed` website triggers a JavaScript error: `TypeError: Cannot read properties of undefined (reading 'name')` somewhere in `components/UserCard.tsx`.
- Sentry's frontend SDK reports the error to the Sentry backend.
- A Sentry alert rule fires.
- Sentry sends a webhook to our server.

What the server has on disk:
- `projects/wellversed.json` — this project is registered.
- A **repo URL** in the project config (`primathontech/wellversed`). The repo is **not yet cloned locally** — that happens during the run.

---

## Stage 0 — webhook arrives (server layer)

Already covered in [03-server-lifecycle.md §3.3](./03-server-lifecycle.md#33-the-endpoint-function-runs). Outcome: a background thread is now running `_execute_pipeline(req)` in `server/__init__.py`.

We pick up the story from there.

---

## Stage 1 — Auto-clone (if needed)

```python
if not req.project.repo_path:
    temp_clone_dir = github.clone_repo()
```

Because `wellversed.json` did NOT specify a local `repo_path`, the pipeline clones the repo into a temp directory. After this step:

- A fresh copy of the repo is on disk somewhere like `/tmp/sentry-automation-XXXX/wellversed`.
- The `GitHubService` instance now knows where to run git commands.

Logged as `[GIT]`. This step is skipped if you pre-cloned the repo and put its path in `repo_path`.

---

## Stage 2 — Step 1: Fetch issues from Sentry

File: `pipeline/issue_fetcher.py`.

```python
all_issues = fetch_all_issues(sentry, query=req.query, issue_id=req.issue_id)
```

What it does:
1. Calls `SentryService.get_issues(query="is:unresolved")` — paginated HTTPS GET to `https://sentry.io/api/0/projects/...`.
2. Deduplicates issues with the same title (keeps the one with the highest event count). Different stacktrace fingerprints can map to the same root cause; we don't want to fix the same bug five times.
3. For each surviving issue, makes a follow-up call to enrich it with the full **stacktrace** (Sentry's list endpoint only gives a summary — you need a per-issue call for the frames).

After this step you have a `list[SentryIssue]` in memory, each with `id`, `title`, `culprit`, `event_count`, `stacktrace`, etc. — see `models/schemas.py`.

> **Test-mode flag:** `TEST_MODE_FETCH_ONE = True` in `issue_fetcher.py` currently caps this to a single issue. Flip to `False` for full pagination.

> **No filtering step.** Earlier versions of the project ran an LLM triage step here to drop "noise" issues (browser extensions, bot traffic, etc.). That step has been removed — every fetched issue now flows straight into the fix loop. If you need to suppress an issue, mute or resolve it in Sentry directly.

---

## Stage 3 — Step 2: Create the git branch

```python
branch_name = github.prepare_branch(f"batch-{N}issues")
```

Inside `services/github_service.py`, this runs (as subprocess shell calls):

```
git fetch origin
git checkout {base_branch}
git pull origin {base_branch}
git checkout -b fix/sentry-{N}issues-{timestamp}
```

The branch name embeds a timestamp so retries never collide.

After this step the local repo is sitting on a fresh branch off the latest `main` (or whatever `base_branch` is — e.g. `wellversed-master`).

---

## Stage 4 — Step 3: The TDD fix loop (the heart of the project)

File: `pipeline/issue_processor.py`. For each issue, we loop up to `max_retries` times. Inside one attempt:

### 5.1 Generate a patch

```python
patch_result = _generate_patch(issue, github, retry_context)
```

The function reads the project's file tree, reads the source files mentioned in the stacktrace, and stuffs all of that — plus the error title and stacktrace — into a giant prompt for GPT. The prompt (`PATCH_SYSTEM_PROMPT` in the file) demands a JSON response of this exact shape:

```json
{
  "file_edits": [
    { "filepath": "...", "original": "...", "replacement": "..." }
  ],
  "commit_message": "...",
  "pr_title": "...",
  "pr_description": "...",
  "confidence": 0.0
}
```

`original` must be an **exact substring** of the current file content — that's how the patch is applied (find/replace, no fuzzy diff).

### 5.2 Build a deterministic test

This is a sanity check, not a real test. It writes a tiny script that asserts: "after the fix, the file at `filepath` should contain the `replacement` string." Trivial, but catches *"the patch failed to apply"* errors.

### 5.3 Build a behavioral test

Now we ask GPT (again) to write a Jest + React Testing Library test that exercises the actual user flow that triggered the bug. Something like:
*"render `<UserCard user={null} />` and assert the component does not crash."*

This is the real verification.

### 5.4 Pre-fix run — both tests should FAIL

We run both tests **before** applying the patch. They should both fail. If they pass, the bug isn't where GPT thinks it is — we've misdiagnosed.

### 5.5 Apply the patch

For each `file_edit`, find the `original` snippet in the file and replace it with `replacement`. Done as a literal string operation. If any `original` isn't found, the attempt fails and we retry.

### 5.6 Post-fix run — both tests should PASS

Same tests, run again. They should now pass. If they do, the issue is `verified=True`. If only the deterministic test passes (the behavioral test still fails), it's `fixed but unverified`.

### 5.7 Repair loop

If the behavioral test fails because of a setup error (compile error, missing import, wrong RTL setup) rather than an actual logic failure, GPT gets a chance to repair the test (up to 2 attempts). Test infrastructure is fiddly; we don't want to throw away a real fix just because Jest couldn't bootstrap.

### Outcome

For each issue we get one of:
- `status="fixed"` + `verified=True` — best case.
- `status="fixed"` + `verified=False` — patch applied, behavioral check inconclusive. Still goes into the PR.
- `status="failed"` — gave up after `max_retries`. Not included in the PR.

---

## Stage 5 — Step 4: Run the project's own test suite

```python
tests_passed, test_output = github.run_tests()
```

If the project config sets `test_command` (e.g. `npm test -- --watchAll=false`), we run it inside the repo. If it fails, we abort, **delete the temp branch**, and return `failed`. Better to bail than to push a broken branch.

If `test_command` is empty, this step is skipped.

---

## Stage 6 — Step 5: Commit, push, open the PR

File: `pipeline/pr_creator.py`.

1. `git add -A && git commit -m "<aggregated commit message>"`. The commit message is built by listing every successfully fixed issue.
2. `git push origin {branch_name}`.
3. Call the GitHub REST API (via PyGithub) to create the PR. The PR body is a markdown table summarising every issue, its confidence score, the verification status, and the original Sentry link.

You get back a PR URL like `https://github.com/primathontech/wellversed/pull/812`.

---

## Stage 7 — Step 6: File Jira tickets

File: `pipeline/jira_creator.py`.

If `jira_project_key` is set in the project config, we create one Jira ticket per fixed issue. Each ticket includes:
- The Sentry error title.
- Stacktrace excerpt.
- List of files changed.
- LLM confidence score.
- Test verification status.
- A link to the PR.

Skipped if Jira credentials are missing.

---

## Stage 8 — Cleanup and response

The pipeline returns a `PipelineResponse` Pydantic model with everything that happened — total issues, fixed count, PR URL, Jira ticket URLs, and per-step statuses. The thread function logs the result and exits. The lock is released. The temp clone directory is deleted.

If anything failed unexpectedly along the way, the `try/except/finally` block in `_execute_pipeline()` ensures:
- the temp branch is deleted (`github.cleanup(branch_name)`),
- the temp clone dir is removed,
- the lock is released.

The server returns to idle, ready for the next webhook.

---

## What the logs look like during a real run

A trimmed example from `logs/sentry-automation.log`:

```
[WEBHOOK] Received: resource=event_alert, action=triggered
[WEBHOOK] New event_alert: #5429318420 in project 'wellversed-prod'
[WEBHOOK] Triggering pipeline for 'wellversed-prod' in background...
[GIT] Cloning primathontech/wellversed into /tmp/sentry-XXXX...
[PIPELINE] Step 1: Fetching issues...
[FETCHER] Fetched 1 issue (TEST_MODE_FETCH_ONE)
[PIPELINE] ✓ Fetched 1 issues
[PIPELINE] Step 2: Creating git branch...
[GIT] Created branch fix/sentry-1issues-20260506-130801
[PIPELINE] Step 3: TDD fixing 1 issues...
[PROCESSOR] Attempt 1/3 for #5429318420
[LLM] Calling gpt-4o-mini for patch generation
[PROCESSOR] ✓ Patch generated (confidence: 0.82)
[PROCESSOR] Pre-fix tests: deterministic FAIL, behavioral FAIL ✓
[PROCESSOR] Applying 1 file edits...
[PROCESSOR] Post-fix tests: deterministic PASS, behavioral PASS ✓
[PIPELINE]   Test: __tests__/UserCard.test.tsx | Pre-fix: FAIL | Post-fix: PASS | VERIFIED
[PIPELINE] Step 4: Running tests...
[GIT] $ npm test -- --watchAll=false
[GIT] Tests passed
[PIPELINE] Step 5: Creating PR...
[GIT] Pushed fix/sentry-1issues-20260506-130801 to origin
[GITHUB] PR opened: https://github.com/primathontech/wellversed/pull/812
[PIPELINE] Step 6: Creating Jira tickets...
[JIRA] Created CSPI-2014
[PIPELINE] Done! 1 fixed (1 verified), 0 failed
[WEBHOOK] Background pipeline done: status=success, fixed=1
```

That's the whole story.

---

Next: [05-external-services.md](./05-external-services.md) — what each of the four external systems is, why we call it, and how API tokens work.
