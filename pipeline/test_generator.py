import json
import logging
import os
import subprocess
from dataclasses import dataclass

from models.schemas import SentryIssue, TestResult
from services.github_service import GitHubService
from services.llm_service import get_llm

logger = logging.getLogger(__name__)

TEST_GEN_SYSTEM_PROMPT = """You are a senior QA engineer. Your job is to write a SOURCE VERIFICATION test that checks whether a specific bug has been fixed in the actual source file.

You will receive:
- The error title and message
- The stacktrace showing where the error occurred
- The relevant source code file(s) with their paths
- The project file structure

## How the test works:
The test uses `fs.readFileSync` to read the ACTUAL source file and checks:
1. The buggy code pattern should NOT exist in the file (test FAILS before fix, PASSES after fix)
2. Optionally, a fix-related pattern SHOULD exist

This approach ensures the test verifies the REAL source file, not an inline copy.

## Example test:
```javascript
const fs = require('fs');
const path = require('path');

describe('Sentry Fix Verification: #12345', () => {
  const filePath = path.resolve(__dirname, '../../src/lib/hooks/useAddToCart.ts');
  let sourceCode;

  beforeAll(() => {
    sourceCode = fs.readFileSync(filePath, 'utf-8');
  });

  test('buggy pattern should be removed from source', () => {
    // This exact buggy code should NOT exist after the fix
    expect(sourceCode).not.toContain('throw new Error("some buggy code")');
  });

  test('source file should contain error handling', () => {
    // After fix, the file should have proper handling
    expect(sourceCode).toMatch(/if\\s*\\(.*\\)\\s*\\{/);  // some guard check
  });
});
```

## Key rules:
- Use `fs.readFileSync` with `path.resolve(__dirname, '../../<relative-path>')` to read the actual source file
- The `__dirname` path should navigate from `__tests__/sentry-fix/` to the repo root
- Test that the BUGGY code pattern does NOT exist (use `not.toContain` or `not.toMatch`)
- The buggy pattern should be an EXACT string or regex from the source code that causes the error
- Identify the specific line(s) of code that cause the bug from the stacktrace and source code
- Use .js extension, no TypeScript, no imports from project
- One describe block, 1-3 test cases

IMPORTANT: The test file path MUST be unique per issue. Use the issue ID in the filename.

Return a JSON object with these exact keys:
{
  "test_file_path": "__tests__/sentry-fix/issue-<ISSUE_ID>.test.js",
  "test_content": "full test file content as a string",
  "run_command": "npx jest __tests__/sentry-fix/issue-<ISSUE_ID>.test.js --no-coverage",
  "description": "one-line description of what the test verifies",
  "source_file": "relative path to the source file being verified"
}

Return ONLY valid JSON, no markdown code fences."""


@dataclass
class GeneratedTest:
    test_file_path: str
    test_content: str
    run_command: str
    description: str


def generate_test(issue: SentryIssue, github: GitHubService) -> GeneratedTest:
    """Use LLM to generate a test case that reproduces the bug."""
    logger.info(f"[TEST_GEN] Generating test for #{issue.id}: {issue.title[:80]}")

    llm = get_llm()
    source_context = _get_source_for_test(issue, github)
    file_tree = github.get_file_tree()
    user_message = _build_test_prompt(issue, source_context, file_tree)

    data = llm.chat_json(
        system_prompt=TEST_GEN_SYSTEM_PROMPT,
        user_message=user_message,
    )

    test = GeneratedTest(
        test_file_path=data.get("test_file_path", ""),
        test_content=data.get("test_content", ""),
        run_command=data.get("run_command", ""),
        description=data.get("description", ""),
    )

    if not test.test_file_path or not test.test_content:
        raise ValueError("LLM returned empty test file path or content")

    logger.info(f"[TEST_GEN] ✓ Generated: {test.test_file_path}")
    logger.info(f"[TEST_GEN]   Description: {test.description}")
    logger.info(f"[TEST_GEN]   Run command: {test.run_command}")
    logger.info(f"[TEST_GEN]   Test content:\n{test.test_content}")
    return test


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
        return passed, output[-1000:]  # Keep last 1000 chars
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


def _get_source_for_test(issue: SentryIssue, github: GitHubService) -> str:
    """Get source code context for test generation."""
    parts = []
    if issue.filename:
        content = github.read_file(issue.filename)
        if content:
            parts.append(content)

    if issue.culprit and issue.culprit != "/":
        for filepath, content in github.find_related_files(issue.culprit)[:2]:
            parts.append(f"File: {filepath}\n```\n{content}\n```")

    return "\n\n".join(parts) if parts else "(No source files found)"


def _build_test_prompt(
    issue: SentryIssue,
    source_context: str,
    file_tree: str,
) -> str:
    """Build the user message for test generation."""
    parts = [
        f"## Bug to Reproduce: Sentry Issue #{issue.id}",
        f"**Error:** {issue.title}",
    ]

    if issue.culprit:
        parts.append(f"**Location:** {issue.culprit}")
    if issue.level:
        parts.append(f"**Severity:** {issue.level}")
    if issue.stacktrace:
        parts.append(f"\n**Stacktrace:**\n```\n{issue.stacktrace}\n```")
    if source_context:
        parts.append(f"\n**Source Code (with file paths):**\n{source_context}")
    if file_tree:
        parts.append(f"\n**Project Files:**\n```\n{file_tree}\n```")

    # Extract source file paths from source_context for the LLM
    import re
    source_files = re.findall(r'File: (.+?)$', source_context, re.MULTILINE) if source_context else []
    if source_files:
        parts.append(f"\n**Source files to verify (use these paths with fs.readFileSync):**")
        for sf in source_files:
            parts.append(f"- {sf}")

    parts.append("""
Write a SOURCE VERIFICATION test using fs.readFileSync that:
1. Reads the ACTUAL source file listed above
2. Checks that the EXACT buggy code pattern exists (test FAILS before fix = bug confirmed)
3. After fix, the buggy pattern is removed so test PASSES

Use: const sourceCode = fs.readFileSync(path.resolve(__dirname, '../../<filepath>'), 'utf-8');
Then: expect(sourceCode).not.toContain('<exact buggy code>');

The buggy code pattern must be an EXACT substring from the source code shown above that causes the error.""")
    return "\n".join(parts)
