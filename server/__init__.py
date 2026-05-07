import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, HTTPException, Request

from config import settings

# ── Logging setup: console + rotating file ────────────────
os.makedirs(settings.LOG_DIR, exist_ok=True)
log_file = os.path.join(settings.LOG_DIR, "sentry-automation.log")

log_format = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# File handler — 10 MB per file, keep 5 backups
file_handler = RotatingFileHandler(
    log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(log_format)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(log_format)

# Apply to root logger so ALL modules are captured
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)
from models.schemas import (
    IssueFixResult,
    PipelineRequest,
    PipelineResponse,
    ProjectConfig,
    StepResult,
)
from project_store import find_project_by_sentry_slug, load_all_projects
from services.sentry_service import SentryService
from services.github_service import GitHubService, GitOperationError
from services.jira_service import JiraService
from services import automation_state
from pipeline.issue_fetcher import fetch_all_issues
from pipeline.issue_processor import process_issue
from pipeline.pr_creator import commit_push_and_create_pr
from pipeline.jira_creator import create_jira_tickets

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Sentry Automation API",
    description="Automated bug-fixing pipeline: Sentry → OpenAI → Git → GitHub PR → Jira",
    version="7.0.0",
)

# ── Security: block vulnerability scanners ────────────────
BLOCKED_EXTENSIONS = (".php", ".env", ".git", ".asp", ".aspx", ".jsp", ".cgi")
BLOCKED_PATHS = ("/containers/", "/vendor/", "/phpunit/", "/.well-known/", "/wp-")

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


class ScannerBlockMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path.lower()
        if any(path.endswith(ext) for ext in BLOCKED_EXTENSIONS) or \
           any(blocked in path for blocked in BLOCKED_PATHS):
            logger.warning(f"[SECURITY] Blocked scanner request: {request.client.host} → {path}")
            return Response(status_code=404)
        return await call_next(request)


app.add_middleware(ScannerBlockMiddleware)

# Per-repo locks — prevents concurrent runs on the same repo
_repo_locks: dict[str, threading.Lock] = {}
_lock_manager = threading.Lock()

# Webhook debounce — tracks last trigger time per project
_last_webhook_trigger: dict[str, float] = {}


def _get_repo_lock(repo_path: str) -> threading.Lock:
    with _lock_manager:
        if repo_path not in _repo_locks:
            _repo_locks[repo_path] = threading.Lock()
        return _repo_locks[repo_path]


def build_services(project: ProjectConfig) -> tuple[SentryService, GitHubService, JiraService]:
    """Build service instances from project config, falling back to .env defaults."""
    sentry = SentryService(
        token=project.sentry_token or settings.SENTRY_TOKEN,
        org=project.sentry_org,
        project=project.sentry_project,
        base_url=settings.SENTRY_BASE_URL,
    )
    github = GitHubService(
        repo_path=project.repo_path or "",
        base_branch=project.base_branch,
        github_token=project.github_token or settings.GITHUB_TOKEN,
        github_repo=project.github_repo,
        test_command=project.test_command,
    )
    jira = JiraService(
        domain=project.jira_domain or settings.JIRA_DOMAIN,
        email=project.jira_email or settings.JIRA_EMAIL,
        api_token=project.jira_api_token or settings.JIRA_API_TOKEN,
        project_key=project.jira_project_key or "",
        issue_type=project.jira_issue_type,
    )
    return sentry, github, jira


# ── Pipeline endpoint ──────────────────────────────────

@app.post("/pipeline/run", response_model=PipelineResponse)
def run_pipeline(req: PipelineRequest):
    """Run the full pipeline: fetch → fix → test → PR → Jira."""
    lock = _get_repo_lock(req.project.github_repo)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A pipeline run is already in progress for this repo")

    try:
        return _execute_pipeline(req)
    finally:
        lock.release()


# ── Webhook endpoint ───────────────────────────────────

@app.post("/webhook/sentry")
async def sentry_webhook(request: Request):
    """Receive Sentry webhook, look up project config, trigger pipeline in background.

    Configure in Sentry → Settings → Developer Settings → Internal Integration → Webhooks.
    Enable 'issue' and/or 'error' events and point to: https://your-server/webhook/sentry
    """
    body = await request.body()

    # Step 1: Verify signature (if secret is configured)
    if settings.SENTRY_CLIENT_SECRET:
        signature = request.headers.get("Sentry-Hook-Signature", "")
        if signature:
            expected = hmac.new(
                key=settings.SENTRY_CLIENT_SECRET.encode("utf-8"),
                msg=body,
                digestmod=hashlib.sha256,
            ).hexdigest()

            if not hmac.compare_digest(signature, expected):
                logger.warning("[WEBHOOK] Signature mismatch — proceeding anyway (verify SENTRY_CLIENT_SECRET)")
        else:
            logger.warning("[WEBHOOK] No signature header present — skipping verification")

    # Step 2: Parse payload
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    action = payload.get("action", "")
    resource = request.headers.get("Sentry-Hook-Resource", "")

    logger.info(f"[WEBHOOK] Received: resource={resource}, action={action}")

    # Accepted webhook events:
    #   issue.created    — first-ever occurrence of a new issue
    #   issue.unresolved — old issue regressed (was resolved, came back)
    #   error.created   — every error event for an existing issue (firehose)
    allowed_events = {
        ("issue", "created"),
        ("issue", "unresolved"),
        ("error", "created"),
    }
    if (resource, action) not in allowed_events:
        logger.info(f"[WEBHOOK] Ignoring: resource={resource}, action={action}")
        return {"status": "ignored", "reason": f"Unhandled event {resource}.{action}"}

    # Step 3: Strict per-resource extraction. issue_id MUST be the issue id
    # (never an event id), or get_issue_details() will 404.
    if resource == "issue":
        issue_data = payload.get("data", {}).get("issue", {})
        project_data = issue_data.get("project", {})
        sentry_project_slug = project_data.get("slug", "")
        issue_id = str(issue_data.get("id", ""))
        issue_title = issue_data.get("title", "")
    else:  # resource == "error"
        error_data = payload.get("data", {}).get("error", {})
        project_data = error_data.get("project", payload.get("data", {}).get("project", {}))
        sentry_project_slug = project_data.get("slug", "")
        issue_id = str(error_data.get("issue_id", ""))   # never fall back to id (event_id)
        issue_title = error_data.get("title", error_data.get("message", ""))

    logger.info(f"[WEBHOOK] {resource}.{action}: issue=#{issue_id} project='{sentry_project_slug}'")
    logger.info(f"[WEBHOOK] Title: {issue_title[:100]}")

    if not sentry_project_slug:
        logger.warning("[WEBHOOK] No project slug in payload")
        raise HTTPException(status_code=400, detail="Missing project slug in webhook payload")
    if not issue_id:
        logger.warning("[WEBHOOK] No issue_id in payload")
        raise HTTPException(status_code=400, detail="Missing issue_id in webhook payload")

    # Step 4: Look up project config
    project = find_project_by_sentry_slug(sentry_project_slug, settings.PROJECTS_DIR)
    if not project:
        logger.warning(f"[WEBHOOK] No project config found for '{sentry_project_slug}'")
        return {
            "status": "skipped",
            "reason": f"No project config found for Sentry project '{sentry_project_slug}'. "
                       f"Create a JSON file in {settings.PROJECTS_DIR}/ with sentry_project='{sentry_project_slug}'",
        }

    sentry, _, _ = build_services(project)

    # Step 5: Regression — write a fresh 'regression' comment that supersedes any
    # prior 'fixed' state, then clear our cache. The comment-state check below
    # will see the new newest comment and decide_action returns 'process'.
    if action == "unresolved":
        automation_state.cache_clear(sentry_project_slug, issue_id)
        try:
            automation_state.write_state(sentry, issue_id, "regression")
        except Exception as e:
            logger.warning(f"[WEBHOOK] could not write regression marker for #{issue_id}: {e}")
        logger.info(f"[WEBHOOK] issue.unresolved → reset state for #{issue_id} (regression)")

    # Step 6: Comment-based dedup. Fast path: in-memory cache.
    if automation_state.cached_terminal(sentry_project_slug, issue_id) == "fixed":
        logger.info(f"[WEBHOOK] Skip #{issue_id}: cached state=fixed")
        return {"status": "skipped", "reason": "already fixed (cache)"}

    # Slow path: ask Sentry for the latest state comment.
    state = automation_state.get_latest_state(sentry, issue_id)
    decision = automation_state.decide_action(state)
    if decision == "skip":
        if state and state.name == "fixed":
            automation_state.cache_set(sentry_project_slug, issue_id, "fixed")
        logger.info(f"[WEBHOOK] Skip #{issue_id}: state={state.name if state else 'none'}")
        return {
            "status": "skipped",
            "reason": f"sentry comment state={state.name}",
            "pr_url": state.pr if state else None,
        }

    # Step 7: Per-issue debounce (so two different bugs don't suppress each other).
    debounce_key = f"{sentry_project_slug}:{issue_id}"
    now = time.time()
    last_trigger = _last_webhook_trigger.get(debounce_key, 0)
    cooldown = settings.WEBHOOK_COOLDOWN_SECONDS

    if now - last_trigger < cooldown:
        remaining = int(cooldown - (now - last_trigger))
        logger.info(f"[WEBHOOK] Debounced #{issue_id}: triggered {int(now - last_trigger)}s ago ({remaining}s remaining)")
        return {
            "status": "debounced",
            "reason": f"issue triggered {int(now - last_trigger)}s ago, cooldown {cooldown}s ({remaining}s remaining)",
        }

    _last_webhook_trigger[debounce_key] = now

    # Opportunistic prune: any entry older than 2× cooldown can never debounce
    # again, so it is dead weight. Drop it. Keeps memory bounded by recent traffic.
    expiry = now - (cooldown * 2)
    stale_keys = [k for k, ts in _last_webhook_trigger.items() if ts <= expiry]
    for k in stale_keys:
        _last_webhook_trigger.pop(k, None)

    # Step 8: Per-repo lock — git safety across concurrent issues in the same repo.
    lock = _get_repo_lock(project.github_repo)
    if lock.locked():
        logger.info(f"[WEBHOOK] Pipeline already running for '{sentry_project_slug}'")
        return {"status": "skipped", "reason": "Pipeline already in progress for this repo"}

    # Step 9: Trigger single-issue pipeline in background thread
    logger.info(f"[WEBHOOK] Triggering pipeline for #{issue_id} in background...")

    def _run_in_background():
        if not lock.acquire(blocking=False):
            logger.warning(f"[WEBHOOK] Could not acquire lock for background run")
            return
        try:
            req = PipelineRequest(project=project, query="is:unresolved", issue_id=issue_id)
            result = _execute_pipeline(req)
            logger.info(f"[WEBHOOK] Background pipeline done: status={result.status}, fixed={result.issues_fixed}")
        except Exception as e:
            logger.exception(f"[WEBHOOK] Background pipeline failed: {e}")
        finally:
            lock.release()

    thread = threading.Thread(target=_run_in_background, daemon=True)
    thread.start()

    return {
        "status": "triggered",
        "project": sentry_project_slug,
        "issue_id": issue_id,
        "issue_title": issue_title,
        "message": f"Pipeline triggered in background for project '{sentry_project_slug}'",
    }


# ── Info endpoints ─────────────────────────────────────

@app.get("/projects")
def list_projects():
    """List all registered project configs from the projects/ directory."""
    projects = load_all_projects(settings.PROJECTS_DIR)
    return [
        {
            "sentry_org": p.sentry_org,
            "sentry_project": p.sentry_project,
            "github_repo": p.github_repo,
            "base_branch": p.base_branch,
            "jira_project_key": p.jira_project_key,
        }
        for p in projects
    ]


@app.get("/webhook/status")
def webhook_status():
    """Show webhook debounce status, keyed by 'project_slug:issue_id'."""
    now = time.time()
    cooldown = settings.WEBHOOK_COOLDOWN_SECONDS
    return {
        key: {
            "last_triggered_ago": f"{int(now - ts)}s",
            "cooldown_remaining": f"{max(0, int(cooldown - (now - ts)))}s",
            "ready": (now - ts) >= cooldown,
        }
        for key, ts in _last_webhook_trigger.items()
    }


# ── Pipeline execution ─────────────────────────────────

def _execute_pipeline(req: PipelineRequest) -> PipelineResponse:
    """Wrap the pipeline with comment-based state writes when an issue_id is set."""
    if not req.issue_id:
        # Manual /pipeline/run with no issue_id — run as-is, no state tracking.
        return _execute_pipeline_core(req)

    sentry, _, _ = build_services(req.project)
    slug = req.project.sentry_project
    issue_id = req.issue_id

    # Mark in_progress before any work starts.
    try:
        automation_state.write_state(sentry, issue_id, "in_progress")
        automation_state.cache_set(slug, issue_id, "in_progress")
    except Exception as e:
        logger.warning(f"[STATE] could not write in_progress for #{issue_id}: {e}")

    resp: PipelineResponse
    try:
        resp = _execute_pipeline_core(req)
    except Exception as e:
        try:
            automation_state.write_state(sentry, issue_id, "failed", reason="exception")
            automation_state.cache_clear(slug, issue_id)
        except Exception:
            pass
        raise

    # Record terminal outcome.
    try:
        if resp.status in ("success", "partial") and resp.pr_url:
            automation_state.write_state(
                sentry, issue_id, "fixed",
                pr=resp.pr_url,
                jira=(resp.jira_tickets[0] if resp.jira_tickets else None),
            )
            automation_state.cache_set(slug, issue_id, "fixed")
        else:
            short_reason = (resp.error or resp.status)[:80] if (resp.error or resp.status) else "unknown"
            automation_state.write_state(sentry, issue_id, "failed", reason=short_reason)
            automation_state.cache_clear(slug, issue_id)
    except Exception as e:
        logger.warning(f"[STATE] could not write final state for #{issue_id}: {e}")

    return resp


def _execute_pipeline_core(req: PipelineRequest) -> PipelineResponse:
    steps: list[StepResult] = []
    issue_results: list[IssueFixResult] = []
    branch_name = None
    temp_clone_dir = None

    sentry, github, jira = build_services(req.project)

    try:
        # Auto-clone if no local repo_path provided
        if not req.project.repo_path:
            try:
                temp_clone_dir = github.clone_repo()
                steps.append(StepResult(step="clone_repo", status="ok", detail=temp_clone_dir))
            except GitOperationError as e:
                logger.error(f"[PIPELINE] ✗ Clone step failed: {e}")
                steps.append(StepResult(step="clone_repo", status="failed", detail=str(e)))
                return PipelineResponse(status="failed", error=f"Clone failed: {e}", steps=steps)

        logger.info("[PIPELINE] ════════════════════════════════════════")
        logger.info(f"[PIPELINE] Pipeline started for {req.project.sentry_org}/{req.project.sentry_project}")
        logger.info(f"[PIPELINE] Repo: {github.repo_path}")
        logger.info(f"[PIPELINE] Query: {req.query}, dry_run: {req.dry_run}")
        logger.info("[PIPELINE] ════════════════════════════════════════")

        # ─── STEP 1: Fetch all issues ────────────────────
        logger.info("[PIPELINE] Step 1: Fetching issues...")
        try:
            all_issues = fetch_all_issues(sentry, query=req.query, issue_id=req.issue_id)
            steps.append(StepResult(step="fetch_issues", status="ok", detail=f"{len(all_issues)} issue(s)"))
            logger.info(f"[PIPELINE] ✓ Fetched {len(all_issues)} issues")
        except ValueError as e:
            logger.error(f"[PIPELINE] ✗ Fetch failed: {e}")
            steps.append(StepResult(step="fetch_issues", status="failed", detail=str(e)))
            return PipelineResponse(status="failed", error=str(e), steps=steps)

        # Apply max_issues limit
        max_issues = req.project.max_issues
        if len(all_issues) > max_issues:
            logger.info(f"[PIPELINE] Capping {len(all_issues)} issues to max_issues={max_issues}")
            all_issues = all_issues[:max_issues]

        # ─── STEP 2: Create branch ──────────────────────
        if not req.dry_run:
            logger.info("[PIPELINE] Step 2: Creating git branch...")
            try:
                branch_name = github.prepare_branch(f"batch-{len(all_issues)}issues")
                steps.append(StepResult(step="git_branch", status="ok", detail=branch_name))
            except GitOperationError as e:
                steps.append(StepResult(step="git_branch", status="failed", detail=str(e)))
                return PipelineResponse(
                    status="failed", error=str(e),
                    issues_total=len(all_issues),
                    issue_results=issue_results, steps=steps,
                )

        # ─── STEP 3: TDD fix each issue (test → fix → verify) ──
        logger.info(f"[PIPELINE] Step 3: TDD fixing {len(all_issues)} issues (generate test → verify bug → fix → verify fix)...")
        for idx, issue in enumerate(all_issues):
            logger.info(f"[PIPELINE] Issue {idx+1}/{len(all_issues)}: #{issue.id}")
            fix_result = process_issue(
                issue, github, dry_run=req.dry_run,
                max_retries=req.project.max_retries,
                context_file=req.project.context_file,
            )
            issue_results.append(fix_result)

            # Log TDD result
            if fix_result.test_result:
                tr = fix_result.test_result
                logger.info(f"[PIPELINE]   Test: {tr.test_file} | Pre-fix: {'FAIL' if not tr.pre_fix_passed else 'PASS'} | Post-fix: {'PASS' if tr.post_fix_passed else 'FAIL'} | {'VERIFIED' if tr.verified else 'UNVERIFIED'}")

        fixed_count = sum(1 for r in issue_results if r.status == "fixed")
        failed_count = sum(1 for r in issue_results if r.status == "failed")
        verified_count = sum(1 for r in issue_results if r.test_result and r.test_result.verified)
        steps.append(StepResult(
            step="tdd_fix", status="ok",
            detail=f"{fixed_count} fixed, {verified_count} verified, {failed_count} failed",
        ))

        if fixed_count == 0:
            if branch_name:
                github.cleanup(branch_name)
            return PipelineResponse(
                status="failed",
                issues_total=len(all_issues),
                issues_fixed=0, issues_failed=failed_count,
                issue_results=issue_results,
                error="No issues could be fixed", steps=steps,
            )

        if req.dry_run:
            return PipelineResponse(
                status="dry_run",
                issues_total=len(all_issues),
                issues_fixed=fixed_count, issues_failed=failed_count,
                issue_results=issue_results, steps=steps,
            )

        # ─── STEP 4: Run tests ──────────────────────────
        logger.info("[PIPELINE] Step 4: Running tests...")
        tests_passed, test_output = github.run_tests()
        if not tests_passed:
            steps.append(StepResult(step="tests", status="failed", detail=test_output[:500]))
            github.cleanup(branch_name)
            return PipelineResponse(
                status="failed",
                issues_total=len(all_issues),
                issues_fixed=fixed_count, issues_failed=failed_count,
                issue_results=issue_results,
                error=f"Tests failed: {test_output[:300]}", steps=steps,
            )
        steps.append(StepResult(step="tests", status="ok", detail=test_output[:200]))

        # ─── STEP 5: Commit + Push + PR ─────────────────
        logger.info("[PIPELINE] Step 5: Creating PR...")
        pr_url = None
        try:
            pr_url = commit_push_and_create_pr(github, branch_name, issue_results, all_issues)
            steps.append(StepResult(step="pr_creation", status="ok", detail=pr_url))
        except Exception as e:
            logger.error(f"[PIPELINE] ✗ PR failed: {e}")
            steps.append(StepResult(step="pr_creation", status="failed", detail=str(e)))

        # ─── STEP 6: Jira tickets ───────────────────────
        logger.info("[PIPELINE] Step 6: Creating Jira tickets...")
        jira_tickets = create_jira_tickets(jira, issue_results, all_issues, pr_url or "")
        if jira_tickets:
            steps.append(StepResult(step="jira_tickets", status="ok", detail=f"{len(jira_tickets)} ticket(s)"))
        else:
            steps.append(StepResult(step="jira_tickets", status="skipped", detail="No tickets created"))

        # ─── DONE ────────────────────────────────────────
        final_status = "success" if pr_url else "partial"
        logger.info("[PIPELINE] ════════════════════════════════════════")
        logger.info(f"[PIPELINE] Done! {fixed_count} fixed ({verified_count} verified), {failed_count} failed")
        logger.info("[PIPELINE] ════════════════════════════════════════")

        return PipelineResponse(
            status=final_status,
            issues_total=len(all_issues),
            issues_fixed=fixed_count,
            issues_failed=failed_count,
            issue_results=issue_results,
            branch=branch_name,
            pr_url=pr_url,
            jira_tickets=jira_tickets,
            steps=steps,
        )

    except Exception as e:
        logger.exception("Pipeline failed with unexpected error")
        if branch_name:
            github.cleanup(branch_name)
        return PipelineResponse(status="failed", error=str(e), steps=steps)
    finally:
        if temp_clone_dir:
            GitHubService.cleanup_clone(temp_clone_dir)
