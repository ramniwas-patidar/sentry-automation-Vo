import json
import logging
import os
import subprocess
from dataclasses import dataclass

from models.schemas import SentryIssue, TestResult

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
) -> TestResult:
    """Build a TestResult from test execution data."""
    verified = (not pre_fix_passed) and post_fix_passed
    return TestResult(
        issue_id=issue.id,
        test_file=test.test_file_path,
        test_description=test.description,
        pre_fix_passed=pre_fix_passed,
        pre_fix_output=pre_fix_output[-500:],
        post_fix_passed=post_fix_passed,
        post_fix_output=post_fix_output[-500:],
        verified=verified,
    )
