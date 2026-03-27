import json
import logging
import os

from models.schemas import IssueFixResult, PatchResult, SentryIssue
from services.github_service import GitHubService
from services.llm_service import get_llm

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
    """Generate and apply a fix for a single issue. Returns the result."""
    retry_context = []

    for attempt in range(1, max_retries + 1):
        logger.info(f"[PROCESSOR] Attempt {attempt}/{max_retries} for #{issue.id}")

        try:
            patch_result = _generate_patch(issue, github, retry_context)
            logger.info(f"[PROCESSOR] ✓ Patch generated (confidence: {patch_result.confidence})")
        except Exception as e:
            logger.error(f"[PROCESSOR] ✗ Patch generation failed: {e}")
            retry_context.append({"diff": "", "error": f"Generation failed: {e}"})
            continue

        if dry_run:
            try:
                edits = json.loads(patch_result.diff)
                files = [e.get("filepath", "") for e in edits]
            except json.JSONDecodeError:
                files = []
            return IssueFixResult(
                issue_id=issue.id, title=issue.title,
                status="fixed", confidence=patch_result.confidence,
                files_changed=files,
            )

        applied, apply_error = _apply_file_edits(patch_result.diff, github.repo_path)
        if not applied:
            logger.error(f"[PROCESSOR] ✗ Apply failed: {apply_error}")
            retry_context.append({"diff": patch_result.diff, "error": apply_error})
            continue

        try:
            edits = json.loads(patch_result.diff)
            files = [e.get("filepath", "") for e in edits]
        except json.JSONDecodeError:
            files = []
        logger.info(f"[PROCESSOR] ✓ Applied: {files}")

        return IssueFixResult(
            issue_id=issue.id, title=issue.title,
            status="fixed", confidence=patch_result.confidence,
            files_changed=files,
        )

    return IssueFixResult(
        issue_id=issue.id, title=issue.title,
        status="failed", error=f"Failed after {max_retries} attempts",
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


def _get_source_context(issue: SentryIssue, github: GitHubService) -> str:
    parts = []

    if issue.filename:
        content = github.read_file(issue.filename)
        if content:
            parts.append(content)

    if issue.culprit and issue.culprit != "/":
        for filepath, content in github.find_related_files(issue.culprit)[:3]:
            parts.append(f"File: {filepath}\n```\n{content}\n```")

    if not parts:
        for entry in ["src/app/page.tsx", "src/app/layout.tsx", "app/page.tsx",
                       "app/layout.tsx", "pages/index.tsx", "pages/_app.tsx"]:
            content = github.read_file(entry)
            if content:
                parts.append(content)
                if len(parts) >= 3:
                    break

    return "\n\n".join(parts) if parts else "(No source files found)"


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
