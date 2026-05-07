"""Build the LLM system prompt for patch generation.

Three layers, concatenated:
  1. The shared sentry-error-resolver policy (prompts/sentry-error-resolver.md)
     — classification rules, minimalistic-change discipline, scoped-suppression
     guidance. Same for every project.
  2. (Optional) per-project context — codebase conventions, common pitfalls,
     team preferences. Path lives in ProjectConfig.context_file.
  3. The JSON output schema — the only part the pipeline parses. Overrides the
     skill's markdown output format because the pipeline needs structured data.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Resolver MD lives next to the project root. Resolved once at import time.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
RESOLVER_PATH = os.path.join(_REPO_ROOT, "prompts", "sentry-error-resolver.md")

JSON_OUTPUT_CONTRACT = """
---

## Output format (OVERRIDES the markdown format above)

You MUST return a single JSON object with these exact keys, and nothing else
(no markdown fences, no prose, no commentary):

{
  "classification": "third_party" | "flow" | "misc",
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
  "confidence": 0.0
}

Rules:
- "classification" reflects the triage decision from the policy above.
- file_edits must contain at least one edit (even for third-party suppression —
  in that case, edit sentry.client.config.ts to add the ignore rule).
- "filepath" must be a real file path relative to the repo root.
- "original" must be an EXACT substring of the current file content (preserve
  whitespace exactly).
- "replacement" is what replaces the original snippet.
- Honor the "minimalistic file changes" constraint from the policy.
- "confidence" is between 0.0 and 1.0 — be honest if unsure.
- Return ONLY valid JSON, no markdown code fences.
""".strip()


def _read_file_safe(path: str, label: str) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        logger.info(f"[PROMPT] Loaded {label}: {path} ({len(content)} chars)")
        return content
    except FileNotFoundError:
        logger.warning(f"[PROMPT] {label} not found at {path} — proceeding without")
        return ""
    except Exception as e:
        logger.warning(f"[PROMPT] failed to read {label} at {path}: {e}")
        return ""


def build_patch_system_prompt(context_file: Optional[str] = None) -> str:
    """Concatenate resolver + project context + JSON schema into the system prompt."""
    resolver = _read_file_safe(RESOLVER_PATH, "sentry-error-resolver")
    project_ctx = _read_file_safe(context_file or "", "project context") if context_file else ""

    parts = []
    if resolver:
        parts.append(resolver.strip())
    if project_ctx:
        parts.append("---\n\n## Project-specific context\n\n" + project_ctx.strip())
    parts.append(JSON_OUTPUT_CONTRACT)

    return "\n\n".join(parts)
