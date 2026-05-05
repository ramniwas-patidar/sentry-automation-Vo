import json
import logging
import os
from typing import Optional

from models.schemas import IssueFixResult, PatchResult, SentryIssue
from services.github_service import GitHubService
from services.llm_service import get_llm
from pipeline.test_generator import (
    BehavioralRun,
    build_behavioral_test,
    build_test_from_patch,
    build_test_result,
    is_test_setup_error,
    repair_behavioral_test,
    run_issue_test,
    write_test_file,
)

logger = logging.getLogger(__name__)

PATCH_SYSTEM_PROMPT = """You are a senior software engineer. Your job is to fix bugs based on Sentry error reports.

You will receive:
- The error title and message
- The stacktrace showing where the error occurred
- The relevant source code file(s) from the repository
- The project file structure

You must return a JSON object with these exact keys:
{
  "file_edits": [
    {
      "filepath": "relative/path/to/file.tsx",
      "original": "the exact original code snippet to find",
      "replacement": "the replacement code"
    }
  ],
  "commit_message": "A conventional commit message (e.g., fix: handle null check in UserService)",
  "pr_title": "Short PR title under 70 chars",
  "pr_description": "Markdown PR body explaining root cause and fix",
  "confidence": 0.0 to 1.0
}

Rules:
- file_edits must contain at least one edit
- "filepath" must be a real file path relative to the repo root
- "original" must be an EXACT substring of the current file content (copy it precisely, including whitespace)
- "replacement" is what replaces the original snippet
- Only change what's necessary to fix the bug
- Do not add unrelated changes
- If you're unsure, set confidence low
- Return ONLY valid JSON, no markdown code fences"""


def process_issue(
    issue: SentryIssue,
    github: GitHubService,
    dry_run: bool = False,
    max_retries: int = 3,
) -> IssueFixResult:
    """Fix issue, then verify with deterministic test built from the fix itself."""
    retry_context = []
    test_result = None

    for attempt in range(1, max_retries + 1):
        logger.info(f"[PROCESSOR] Attempt {attempt}/{max_retries} for #{issue.id}")

        # ── Step 1: Generate fix ──────────────────────────
        try:
            patch_result = _generate_patch(issue, github, retry_context)
            logger.info(f"[PROCESSOR] ✓ Patch generated (confidence: {patch_result.confidence})")
        except Exception as e:
            logger.error(f"[PROCESSOR] ✗ Patch generation failed: {e}")
            retry_context.append({"diff": "", "error": f"Generation failed: {e}"})
            continue

        try:
            edits = json.loads(patch_result.diff)
            files = [e.get("filepath", "") for e in edits]
        except json.JSONDecodeError:
            files = []

        if dry_run:
            return IssueFixResult(
                issue_id=issue.id, title=issue.title,
                status="fixed", confidence=patch_result.confidence,
                files_changed=files,
            )

        # ── Step 2: Build deterministic test from fix data ──
        generated_test = None
        try:
            generated_test = build_test_from_patch(issue.id, patch_result.diff)
            write_test_file(generated_test, github.repo_path)
        except Exception as e:
            logger.warning(f"[PROCESSOR] Det test build failed for #{issue.id}: {e} — proceeding without")

        # ── Step 3: Build behavioral test (LLM, before fix is applied) ──
        beh_test = None
        try:
            source_context = _get_source_context(issue, github)
            beh_test = build_behavioral_test(issue, patch_result.diff, source_context)
            if beh_test:
                write_test_file(beh_test, github.repo_path)
        except Exception as e:
            logger.warning(f"[PROCESSOR] Behavioral test gen failed for #{issue.id}: {e} — proceeding without")

        # ── Step 4: Run pre-fix tests (expect FAIL on both) ──
        pre_fix_passed = False
        pre_fix_output = ""
        if generated_test:
            pre_fix_passed, pre_fix_output = run_issue_test(generated_test, github.repo_path)
            logger.info(f"[PROCESSOR] Det pre-fix: {'PASS' if pre_fix_passed else 'FAIL'} (FAIL is expected)")

        beh_pre_passed = False
        beh_pre_output = ""
        if beh_test:
            beh_pre_passed, beh_pre_output = run_issue_test(beh_test, github.repo_path)
            logger.info(f"[PROCESSOR] Beh pre-fix: {'PASS' if beh_pre_passed else 'FAIL'} (FAIL is expected)")

        # ── Step 5: Apply fix ─────────────────────────────
        applied, apply_error = _apply_file_edits(patch_result.diff, github.repo_path)
        if not applied:
            logger.error(f"[PROCESSOR] ✗ Apply failed: {apply_error}")
            for t in (generated_test, beh_test):
                if t:
                    test_path = os.path.join(github.repo_path, t.test_file_path)
                    if os.path.isfile(test_path):
                        os.remove(test_path)
            retry_context.append({"diff": patch_result.diff, "error": apply_error})
            continue

        logger.info(f"[PROCESSOR] ✓ Applied: {files}")

        # ── Step 6: Run post-fix tests (expect PASS on both) ──
        post_fix_passed = False
        post_fix_output = ""
        if generated_test:
            post_fix_passed, post_fix_output = run_issue_test(generated_test, github.repo_path)
            logger.info(f"[PROCESSOR] Det post-fix: {'PASS' if post_fix_passed else 'FAIL'}")

        beh_post_passed = False
        beh_post_output = ""
        repair_attempts = 0
        if beh_test:
            beh_post_passed, beh_post_output = run_issue_test(beh_test, github.repo_path)
            logger.info(f"[PROCESSOR] Beh post-fix: {'PASS' if beh_post_passed else 'FAIL'}")

            # Repair loop: only on test setup/compile errors, max 2 attempts
            while (
                repair_attempts < 2
                and not beh_post_passed
                and is_test_setup_error(beh_post_output)
            ):
                repair_attempts += 1
                logger.info(f"[PROCESSOR] Behavioral test setup error, repair attempt {repair_attempts}/2")
                repaired = repair_behavioral_test(beh_test, beh_post_output)
                if not repaired:
                    break
                beh_test = repaired
                write_test_file(beh_test, github.repo_path)
                beh_post_passed, beh_post_output = run_issue_test(beh_test, github.repo_path)
                logger.info(f"[PROCESSOR] Beh post-fix (after repair {repair_attempts}): {'PASS' if beh_post_passed else 'FAIL'}")

            # If we repaired, re-run pre-fix with the repaired test for an accurate verdict
            if repair_attempts > 0:
                rerun = _safely_rerun_behavioral_pre_fix(
                    beh_test, patch_result.diff, github.repo_path,
                )
                if rerun is not None:
                    beh_pre_passed, beh_pre_output = rerun
                    logger.info(f"[PROCESSOR] Beh pre-fix (repaired test): {'PASS' if beh_pre_passed else 'FAIL'}")

        if generated_test:
            test_result = build_test_result(
                issue, generated_test,
                pre_fix_passed, pre_fix_output,
                post_fix_passed, post_fix_output,
                behavioral=BehavioralRun(
                    test=beh_test,
                    pre_fix_passed=beh_pre_passed,
                    pre_fix_output=beh_pre_output,
                    post_fix_passed=beh_post_passed,
                    post_fix_output=beh_post_output,
                    repair_attempts=repair_attempts,
                ) if beh_test else None,
            )

            if test_result.verified:
                logger.info(f"[PROCESSOR] ✓ Behavioral VERIFIED for #{issue.id}")
            elif test_result.deterministic_verified:
                logger.info(f"[PROCESSOR] ~ Deterministic verified, behavioral unverified for #{issue.id}")
            else:
                logger.warning(f"[PROCESSOR] ✗ Unverified for #{issue.id}, fix accepted")

        return IssueFixResult(
            issue_id=issue.id, title=issue.title,
            status="fixed", confidence=patch_result.confidence,
            files_changed=files, test_result=test_result,
        )

    return IssueFixResult(
        issue_id=issue.id, title=issue.title,
        status="failed", error=f"Failed after {max_retries} attempts",
        test_result=test_result,
    )


def _generate_patch(
    issue: SentryIssue,
    github: GitHubService,
    retry_context: list[dict],
) -> PatchResult:
    llm = get_llm()

    source_context = _get_source_context(issue, github)
    file_tree = github.get_file_tree()
    user_message = _build_user_message(issue, source_context, file_tree, retry_context)

    data = llm.chat_json(
        system_prompt=PATCH_SYSTEM_PROMPT,
        user_message=user_message,
    )

    file_edits = data.get("file_edits", [])
    if not file_edits:
        raise ValueError("LLM returned no file edits")

    logger.info(f"[PROCESSOR] File edits count: {len(file_edits)}")
    for i, edit in enumerate(file_edits):
        logger.info(f"[PROCESSOR]   Edit {i+1}: file={edit.get('filepath')}")

    diff_text = json.dumps(file_edits, indent=2)

    return PatchResult(
        diff=diff_text,
        commit_message=data.get("commit_message", f"fix: resolve {issue.title}"),
        pr_title=data.get("pr_title", f"fix: {issue.title[:60]}"),
        pr_description=data.get("pr_description", f"Fixes Sentry issue {issue.id}"),
        confidence=float(data.get("confidence", 0.5)),
    )


def _apply_file_edits(edits_json: str, repo_path: str) -> tuple[bool, str]:
    """Apply file edits from LLM output. Returns (success, error_message)."""
    logger.info(f"[PROCESSOR] Applying file edits to repo: {repo_path}")

    try:
        edits = json.loads(edits_json)
    except json.JSONDecodeError as e:
        return False, f"Invalid edits JSON: {e}"

    for i, edit in enumerate(edits):
        filepath = edit.get("filepath", "")
        original = edit.get("original", "")
        replacement = edit.get("replacement", "")

        full_path = os.path.join(repo_path, filepath)
        logger.info(f"[PROCESSOR] Edit {i+1}: {filepath} (exists={os.path.isfile(full_path)})")

        if not os.path.isfile(full_path):
            return False, f"File not found: {filepath}"

        with open(full_path, "r") as f:
            content = f.read()

        if original not in content:
            logger.error(f"[PROCESSOR] Original snippet not found in {filepath}")
            return False, f"Original snippet not found in {filepath}"

        new_content = content.replace(original, replacement, 1)

        with open(full_path, "w") as f:
            f.write(new_content)

        logger.info(f"[PROCESSOR] ✓ Applied edit to {filepath}")

    logger.info(f"[PROCESSOR] All {len(edits)} edit(s) applied")
    return True, ""


def _safely_rerun_behavioral_pre_fix(
    beh_test,
    edits_json: str,
    repo_path: str,
) -> Optional[tuple]:
    """Revert source, run behavioral test, re-apply source. Returns (passed, output) or None.

    Used after the test was repaired post-fix — the original pre-fix output was
    against an unrepaired test, so the verdict isn't comparable. We need to run
    the repaired test against the buggy source to know if it actually reproduces
    the bug.
    """
    try:
        _revert_file_edits(edits_json, repo_path)
    except Exception as e:
        logger.warning(f"[PROCESSOR] Pre-fix re-run: revert failed ({e}) — keeping original verdict")
        return None

    try:
        result = run_issue_test(beh_test, repo_path)
    finally:
        # Always re-apply, even if the run errored, to leave the repo in a
        # consistent post-fix state.
        applied, err = _apply_file_edits(edits_json, repo_path)
        if not applied:
            logger.error(f"[PROCESSOR] Pre-fix re-run: re-apply FAILED ({err}) — repo state may be inconsistent")

    return result


def _revert_file_edits(edits_json: str, repo_path: str) -> None:
    """Revert file edits by swapping replacement back to original."""
    logger.info("[PROCESSOR] Reverting file edits...")
    try:
        edits = json.loads(edits_json)
    except json.JSONDecodeError:
        return

    for edit in edits:
        filepath = edit.get("filepath", "")
        original = edit.get("original", "")
        replacement = edit.get("replacement", "")
        full_path = os.path.join(repo_path, filepath)

        if not os.path.isfile(full_path):
            continue

        with open(full_path, "r") as f:
            content = f.read()

        if replacement in content:
            new_content = content.replace(replacement, original, 1)
            with open(full_path, "w") as f:
                f.write(new_content)
            logger.info(f"[PROCESSOR] ✓ Reverted: {filepath}")


def _get_source_context(issue: SentryIssue, github: GitHubService) -> str:
    parts = []

    # 1. Read the file from stacktrace (if Sentry identified it)
    if issue.filename:
        content = github.read_file(issue.filename)
        if content:
            parts.append(content)

    # 2. Search by culprit path
    if issue.culprit and issue.culprit != "/":
        for filepath, content in github.find_related_files(issue.culprit)[:3]:
            parts.append(f"File: {filepath}\n```\n{content}\n```")

    # 3. If no source found yet, extract keywords from error title and search
    if not parts:
        keywords = _extract_keywords_from_title(issue.title)
        logger.info(f"[PROCESSOR] No source from stacktrace, searching by keywords: {keywords}")
        for keyword in keywords:
            results = github.search_files_by_keyword(keyword)
            if results:
                logger.info(f"[PROCESSOR] Found {len(results)} files matching '{keyword}'")
                for filepath, content in results[:3]:
                    parts.append(f"File: {filepath}\n```\n{content}\n```")
                break  # Found files with first keyword, stop searching

    # 4. Fallback to common entry points
    if not parts:
        for entry in ["src/app/page.tsx", "src/app/layout.tsx", "app/page.tsx",
                       "app/layout.tsx", "pages/index.tsx", "pages/_app.tsx"]:
            content = github.read_file(entry)
            if content:
                parts.append(content)
                if len(parts) >= 3:
                    break

    return "\n\n".join(parts) if parts else "(No source files found)"


def _extract_keywords_from_title(title: str) -> list[str]:
    """Extract searchable function/variable names from error title."""
    import re
    keywords = []

    # Extract camelCase/PascalCase identifiers (function names like addToCart, UserService)
    identifiers = re.findall(r'\b[a-z][a-zA-Z]{4,}\b|\b[A-Z][a-zA-Z]{4,}\b', title)
    skip = {"error", "cannot", "undefined", "reading", "properties", "failed", "missing",
            "thrown", "invalid", "typeerror", "referenceerror", "rangeerror", "syntaxerror",
            "wrong", "defined", "sentry", "maximum", "stack", "exceeded", "component",
            "render", "conflict", "detected", "created", "without", "cleanup"}
    for ident in identifiers:
        if ident.lower() not in skip:
            keywords.append(ident)

    # Extract text after colon patterns like "conditions:addToCart"
    colon_parts = re.findall(r':(\w{3,})', title)
    for part in colon_parts:
        if part not in keywords:
            keywords.append(part)

    return keywords[:3]  # Return top 3 keywords


def _build_user_message(
    issue: SentryIssue,
    source_context: str,
    file_tree: str,
    retry_context: list[dict],
) -> str:
    parts = [
        f"## Sentry Issue #{issue.id}",
        f"**Title:** {issue.title}",
    ]

    if issue.culprit:
        parts.append(f"**Culprit:** {issue.culprit}")
    if issue.level:
        parts.append(f"**Level:** {issue.level}")
    if issue.stacktrace:
        parts.append(f"\n**Stacktrace:**\n```\n{issue.stacktrace}\n```")
    if source_context:
        parts.append(f"\n**Source Code:**\n{source_context}")
    if file_tree:
        parts.append(f"\n**Project Files:**\n```\n{file_tree}\n```")

    if retry_context:
        parts.append("\n**Previous failed attempts (learn from these):**")
        for i, ctx in enumerate(retry_context, 1):
            parts.append(f"\nAttempt {i}:")
            parts.append(f"Edit tried:\n```\n{ctx.get('diff', 'N/A')}\n```")
            parts.append(f"Failure reason: {ctx.get('error', 'Unknown')}")

    parts.append("\nPlease generate a fix using file_edits format.")
    return "\n".join(parts)
