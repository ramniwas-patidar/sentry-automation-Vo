import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

from models.schemas import SentryIssue, TestResult
from services.llm_service import get_llm

logger = logging.getLogger(__name__)


@dataclass
class GeneratedTest:
    test_file_path: str
    test_content: str
    run_command: str
    description: str


def build_test_from_patch(issue_id: str, file_edits_json: str) -> GeneratedTest:
    """Build a deterministic test from the patch data. No LLM needed.

    The test reads the actual source file and verifies:
    - Pre-fix: the 'original' buggy code EXISTS in file → test FAILS (not.toContain fails)
    - Post-fix: the 'original' buggy code is GONE → test PASSES (not.toContain passes)
    """
    edits = json.loads(file_edits_json)
    if not edits:
        raise ValueError("No file edits to build test from")

    test_file_path = f"__tests__/sentry-fix/issue-{issue_id}.test.js"
    run_command = f"npx jest {test_file_path} --no-coverage"

    # Build test cases for each file edit
    test_blocks = []
    descriptions = []

    for edit in edits:
        filepath = edit.get("filepath", "")
        original = edit.get("original", "")
        replacement = edit.get("replacement", "")

        if not filepath or not original:
            continue

        # Escape special chars for JavaScript string
        original_escaped = _escape_js_string(original)
        replacement_escaped = _escape_js_string(replacement)

        test_blocks.append(f"""
  test('buggy code should be removed from {filepath}', () => {{
    const filePath = path.resolve(__dirname, '../../{filepath}');
    const sourceCode = fs.readFileSync(filePath, 'utf-8');

    // The original buggy code should NOT exist after the fix
    expect(sourceCode).not.toContain({original_escaped});
  }});

  test('fix should be present in {filepath}', () => {{
    const filePath = path.resolve(__dirname, '../../{filepath}');
    const sourceCode = fs.readFileSync(filePath, 'utf-8');

    // The replacement code should exist after the fix
    expect(sourceCode).toContain({replacement_escaped});
  }});""")

        descriptions.append(f"verify fix in {filepath}")

    if not test_blocks:
        raise ValueError("No valid edits to build test from")

    test_content = f"""const fs = require('fs');
const path = require('path');

describe('Sentry Fix Verification: #{issue_id}', () => {{
{"".join(test_blocks)}
}});
"""

    description = "; ".join(descriptions)
    logger.info(f"[TEST_GEN] ✓ Built deterministic test: {test_file_path}")
    logger.info(f"[TEST_GEN]   Description: {description}")
    logger.info(f"[TEST_GEN]   Edits covered: {len(test_blocks)}")

    return GeneratedTest(
        test_file_path=test_file_path,
        test_content=test_content,
        run_command=run_command,
        description=description,
    )


def _escape_js_string(s: str) -> str:
    """Escape a string for use in a JavaScript test as a template literal."""
    # Use backtick template literals to handle multi-line strings and quotes
    escaped = s.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')
    return f'`{escaped}`'


def write_test_file(test: GeneratedTest, repo_path: str) -> None:
    """Write the generated test file to the repo."""
    full_path = os.path.join(repo_path, test.test_file_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    with open(full_path, "w") as f:
        f.write(test.test_content)

    logger.info(f"[TEST_GEN] ✓ Wrote test file: {test.test_file_path}")


def run_issue_test(test: GeneratedTest, repo_path: str, timeout: int = 120) -> tuple[bool, str]:
    """Run the specific test. Returns (passed, output)."""
    if not test.run_command:
        return False, "No run command specified"

    logger.info(f"[TEST_GEN] Running: {test.run_command}")
    try:
        result = subprocess.run(
            test.run_command, shell=True,
            cwd=repo_path,
            capture_output=True, text=True, timeout=timeout,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        passed = result.returncode == 0
        logger.info(f"[TEST_GEN] Test {'PASSED' if passed else 'FAILED'} (exit={result.returncode})")
        if not passed:
            logger.info(f"[TEST_GEN] Test output:\n{output[-500:]}")
        return passed, output[-1000:]
    except subprocess.TimeoutExpired:
        logger.warning(f"[TEST_GEN] Test timed out after {timeout}s")
        return False, f"Test timed out after {timeout}s"
    except Exception as e:
        logger.error(f"[TEST_GEN] Test runner error: {e}")
        return False, f"Test runner error: {e}"


def build_test_result(
    issue: SentryIssue,
    test: GeneratedTest,
    pre_fix_passed: bool,
    pre_fix_output: str,
    post_fix_passed: bool = False,
    post_fix_output: str = "",
    behavioral: Optional["BehavioralRun"] = None,
) -> TestResult:
    """Build a TestResult from execution data for both layers."""
    deterministic_verified = (not pre_fix_passed) and post_fix_passed

    if behavioral and behavioral.test:
        beh_pre = behavioral.pre_fix_passed
        beh_post = behavioral.post_fix_passed
        behavioral_verified = (not beh_pre) and beh_post
        return TestResult(
            issue_id=issue.id,
            test_file=test.test_file_path,
            test_description=test.description,
            pre_fix_passed=pre_fix_passed,
            pre_fix_output=pre_fix_output[-500:],
            post_fix_passed=post_fix_passed,
            post_fix_output=post_fix_output[-500:],
            deterministic_verified=deterministic_verified,
            behavioral_test_file=behavioral.test.test_file_path,
            behavioral_test_code=behavioral.test.test_content,
            behavioral_test_description=behavioral.test.description,
            behavioral_pre_fix_passed=beh_pre,
            behavioral_pre_fix_output=behavioral.pre_fix_output[-500:],
            behavioral_post_fix_passed=beh_post,
            behavioral_post_fix_output=behavioral.post_fix_output[-500:],
            behavioral_repair_attempts=behavioral.repair_attempts,
            behavioral_verified=behavioral_verified,
            verified=behavioral_verified,
        )

    return TestResult(
        issue_id=issue.id,
        test_file=test.test_file_path,
        test_description=test.description,
        pre_fix_passed=pre_fix_passed,
        pre_fix_output=pre_fix_output[-500:],
        post_fix_passed=post_fix_passed,
        post_fix_output=post_fix_output[-500:],
        deterministic_verified=deterministic_verified,
        verified=False,
    )


# ── Behavioral test layer (LLM-generated) ────────────────────────────

@dataclass
class BehavioralRun:
    """Aggregated behavioral test execution state."""
    test: Optional[GeneratedTest]
    pre_fix_passed: bool = False
    pre_fix_output: str = ""
    post_fix_passed: bool = False
    post_fix_output: str = ""
    repair_attempts: int = 0


BEHAVIORAL_SYSTEM_PROMPT = """You write Jest + React Testing Library regression tests for a fixed bug.

You receive:
- The Sentry error (title, level, stacktrace).
- The buggy source files.
- The fix that was applied (file_edits with original/replacement snippets).

Write a Jest test that exercises the user flow which triggered the crash. The test must:
- FAIL when run against the original (buggy) source: the crash is reproduced or an assertion fails.
- PASS when run against the fixed source.
- Cover every step of the user flow leading to the bug (render → interact → assert).

Return STRICT JSON, no markdown fences:
{
  "feasible": true | false,
  "reason_if_infeasible": "string (only when feasible=false)",
  "test_code": "full Jest test source",
  "description": "one-line plain description of the user flow covered",
  "uses_react_testing_library": true | false
}

Rules for the test code:
- Use CommonJS require(), not ES import.
- Test file will be saved at __tests__/sentry-fix/issue-<id>.behavioral.test.js. Repo root is two levels up: path.resolve(__dirname, '../../<filepath>').
- For React components use @testing-library/react: render + fireEvent or userEvent.
- Wrap risky calls in try/catch only when the test is meant to assert a thrown error.
- Mock network calls (global.fetch / axios) inline — never hit a real network.
- No snapshots, no describe nesting deeper than one level.
- Set feasible=false if the bug is in non-UI code without an obvious reproducible flow (e.g., backend, build config) and leave test_code empty."""


REPAIR_SYSTEM_PROMPT = """A Jest test you wrote failed to load (compile/import/syntax error). Repair it.

You receive the previous test source and the Jest output. Return STRICT JSON, no markdown fences:
{
  "test_code": "full corrected Jest test source",
  "description": "same one-line description as before"
}

Rules:
- Keep the same describe/test structure and assertion intent.
- Fix only what's preventing the file from loading (missing require, wrong path, syntax slip).
- Do not weaken assertions to make the test pass artificially."""


def build_behavioral_test(
    issue: SentryIssue,
    file_edits_json: str,
    source_context: str,
) -> Optional[GeneratedTest]:
    """Generate a behavioral Jest+RTL test via LLM. Returns None if infeasible."""
    test_file_path = f"__tests__/sentry-fix/issue-{issue.id}.behavioral.test.js"
    run_command = f"npx jest {test_file_path} --no-coverage"

    user_message = _build_behavioral_user_message(issue, file_edits_json, source_context)
    llm = get_llm()
    try:
        data = llm.chat_json(
            system_prompt=BEHAVIORAL_SYSTEM_PROMPT,
            user_message=user_message,
        )
    except Exception as e:
        logger.warning(f"[BEH_TEST] LLM call failed for #{issue.id}: {e}")
        return None

    if not data.get("feasible", False):
        logger.info(f"[BEH_TEST] Skipped (infeasible): {data.get('reason_if_infeasible', 'no reason')}")
        return None

    test_code = data.get("test_code", "").strip()
    description = data.get("description", "").strip() or f"behavioral regression test for #{issue.id}"
    if not test_code:
        logger.warning(f"[BEH_TEST] LLM returned empty test_code for #{issue.id}")
        return None

    logger.info(f"[BEH_TEST] ✓ Generated behavioral test ({len(test_code)} chars): {description}")
    return GeneratedTest(
        test_file_path=test_file_path,
        test_content=test_code,
        run_command=run_command,
        description=description,
    )


def repair_behavioral_test(
    test: GeneratedTest,
    error_output: str,
) -> Optional[GeneratedTest]:
    """Ask LLM to fix a test that failed to load (compile/syntax error)."""
    user_message = (
        f"Previous test source:\n```js\n{test.test_content}\n```\n\n"
        f"Jest output (last 1500 chars):\n```\n{error_output[-1500:]}\n```\n\n"
        f"Return repaired test_code that loads cleanly."
    )
    llm = get_llm()
    try:
        data = llm.chat_json(
            system_prompt=REPAIR_SYSTEM_PROMPT,
            user_message=user_message,
        )
    except Exception as e:
        logger.warning(f"[BEH_TEST] Repair LLM call failed: {e}")
        return None

    test_code = data.get("test_code", "").strip()
    if not test_code:
        return None

    description = data.get("description", "").strip() or test.description
    logger.info(f"[BEH_TEST] ✓ Repaired test ({len(test_code)} chars)")
    return GeneratedTest(
        test_file_path=test.test_file_path,
        test_content=test_code,
        run_command=test.run_command,
        description=description,
    )


_TEST_SETUP_ERROR_MARKERS = (
    "Test suite failed to run",
    "SyntaxError:",
    "Cannot find module",
    "Jest encountered an unexpected token",
    "ReferenceError:",
    "Module not found",
)


def is_test_setup_error(output: str) -> bool:
    """Heuristic: did Jest fail to even load the test file?"""
    if not output:
        return False
    for marker in _TEST_SETUP_ERROR_MARKERS:
        if marker in output:
            return True
    return False


def _build_behavioral_user_message(
    issue: SentryIssue,
    file_edits_json: str,
    source_context: str,
) -> str:
    parts = [
        f"## Sentry Issue #{issue.id}",
        f"**Title:** {issue.title}",
    ]
    if issue.level:
        parts.append(f"**Level:** {issue.level}")
    if issue.culprit:
        parts.append(f"**Culprit:** {issue.culprit}")
    if issue.stacktrace:
        parts.append(f"\n**Stacktrace:**\n```\n{issue.stacktrace[:3000]}\n```")
    if source_context:
        parts.append(f"\n**Buggy source (pre-fix):**\n{source_context[:6000]}")
    parts.append(f"\n**Fix applied (file_edits):**\n```json\n{file_edits_json[:3000]}\n```")
    parts.append("\nGenerate the JSON described in the system prompt.")
    return "\n".join(parts)
