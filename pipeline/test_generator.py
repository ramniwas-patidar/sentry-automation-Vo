import json
import logging
import os
import subprocess
from dataclasses import dataclass

from models.schemas import SentryIssue, TestResult
from services.github_service import GitHubService
from services.llm_service import get_llm

logger = logging.getLogger(__name__)

TEST_GEN_SYSTEM_PROMPT = """You are a senior QA engineer. Your job is to write a test case that REPRODUCES a specific bug reported by Sentry.

You will receive:
- The error title and message
- The stacktrace showing where the error occurred
- The relevant source code file(s)
- The project file structure

Your test must:
1. FAIL when the bug exists (before the fix)
2. PASS when the bug is fixed (after the fix)
3. Be self-contained and not depend on external services, databases, or network
4. Use the project's existing test framework (detect from file structure)
5. Import and test the actual function/component that has the bug
6. Be placed in a sentry-fix test directory

Test framework detection:
- If you see jest.config, __tests__/, *.test.ts/tsx/js → use Jest
- If you see vitest.config → use Vitest
- If you see pytest, tests/, conftest.py → use pytest
- If you see mocha, .mocharc → use Mocha
- Default for TypeScript/JavaScript: Jest
- Default for Python: pytest

Return a JSON object with these exact keys:
{
  "test_file_path": "relative/path/to/test/file",
  "test_content": "full test file content as a string",
  "run_command": "command to run just this test file (e.g., npx jest path/to/test.ts --no-coverage)",
  "description": "one-line description of what the test verifies"
}

Rules:
- The test should directly exercise the code path that causes the error
- For TypeError/ReferenceError: test with the exact input that triggers it
- For UI components: test the component logic, not rendering
- Keep tests simple and focused on the specific bug
- Use mocks/stubs for dependencies the function needs
- Return ONLY valid JSON, no markdown code fences"""


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
        parts.append(f"\n**Source Code:**\n{source_context}")
    if file_tree:
        parts.append(f"\n**Project Files:**\n```\n{file_tree}\n```")

    parts.append("\nWrite a test that FAILS when this bug exists and PASSES when fixed.")
    return "\n".join(parts)
