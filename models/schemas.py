from typing import Optional
from pydantic import BaseModel


class ProjectConfig(BaseModel):
    """Per-project configuration — passed at runtime, not hardcoded."""
    # Sentry
    sentry_org: str
    sentry_project: str
    sentry_token: Optional[str] = None  # falls back to .env default

    # GitHub
    github_repo: str  # "owner/repo"
    github_token: Optional[str] = None  # falls back to .env default
    base_branch: str = "main"

    # Local repo (optional — if omitted, repo is auto-cloned from github_repo)
    repo_path: Optional[str] = None

    # Jira (optional)
    jira_project_key: Optional[str] = None
    jira_domain: Optional[str] = None
    jira_email: Optional[str] = None
    jira_api_token: Optional[str] = None
    jira_issue_type: str = "Bug"

    # Pipeline
    test_command: str = ""
    max_retries: int = 3
    max_issues: int = 25  # max issues to process per pipeline run (after filtering)


class SentryIssue(BaseModel):
    id: str
    title: str
    culprit: Optional[str] = None
    level: Optional[str] = None
    status: Optional[str] = None
    count: Optional[str] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    permalink: Optional[str] = None
    stacktrace: Optional[str] = None
    filename: Optional[str] = None


class PatchResult(BaseModel):
    diff: str
    commit_message: str
    pr_title: str
    pr_description: str
    confidence: float = 0.0


class PipelineRequest(BaseModel):
    project: ProjectConfig
    query: str = "is:unresolved"
    issue_id: Optional[str] = None
    dry_run: bool = False


class StepResult(BaseModel):
    step: str
    status: str
    detail: Optional[str] = None
    attempt: Optional[int] = None


class FilteredIssue(BaseModel):
    """Result of LLM filtering for a single issue."""
    issue_id: str
    title: str
    is_relevant: bool
    reason: str
    category: Optional[str] = None


class TestResult(BaseModel):
    """Result of TDD verification for a single issue.

    Two layers run for every fix:
    - Deterministic: file-content substring check, always-on safety net.
    - Behavioral: LLM-generated Jest+RTL test that exercises the user flow.

    `verified` reflects ONLY the behavioral layer per spec: True iff behavioral
    pre-fix fails (bug reproduced) and behavioral post-fix passes.
    """
    issue_id: str

    # Deterministic layer (file-content substring check)
    test_file: str = ""
    test_description: str = ""
    pre_fix_passed: bool = False
    pre_fix_output: str = ""
    post_fix_passed: bool = False
    post_fix_output: str = ""
    deterministic_verified: bool = False

    # Behavioral layer (LLM-generated, real test execution)
    behavioral_test_file: Optional[str] = None
    behavioral_test_code: Optional[str] = None
    behavioral_test_description: Optional[str] = None
    behavioral_pre_fix_passed: bool = False
    behavioral_pre_fix_output: str = ""
    behavioral_post_fix_passed: bool = False
    behavioral_post_fix_output: str = ""
    behavioral_repair_attempts: int = 0
    behavioral_verified: bool = False

    # Headline verdict (mirrors behavioral_verified per spec)
    verified: bool = False


class IssueFixResult(BaseModel):
    """Result of fixing a single issue within the batch."""
    issue_id: str
    title: str
    status: str  # "fixed", "failed", "skipped", "filtered"
    error: Optional[str] = None
    confidence: float = 0.0
    files_changed: list[str] = []
    jira_ticket: Optional[str] = None
    test_result: Optional[TestResult] = None


class PipelineResponse(BaseModel):
    status: str  # "success", "partial", "failed", "dry_run"
    issues_total: int = 0
    issues_filtered: int = 0
    issues_fixed: int = 0
    issues_failed: int = 0
    issue_results: list[IssueFixResult] = []
    branch: Optional[str] = None
    pr_url: Optional[str] = None
    jira_tickets: list[str] = []
    error: Optional[str] = None
    steps: list[StepResult] = []
