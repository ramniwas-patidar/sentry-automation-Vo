#!/usr/bin/env python3
"""CLI entry point for Sentry Automation Pipeline.

Usage:
    # With a config file:
    python run.py --config projects/wellversed.json

    # With inline args:
    python run.py \
        --sentry-org primathon-10 \
        --sentry-project wellversed-dev \
        --github-repo prima-rajkumar/wellversed-dev \
        --repo-path /path/to/local/clone \
        --base-branch main \
        --jira-project-key SFR

    # Dry run (no git/PR/Jira, just generate patches):
    python run.py --config projects/wellversed.json --dry-run

    # Start API server instead:
    python run.py --server
"""
import argparse
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Sentry Automation Pipeline")

    # Mode
    parser.add_argument("--server", action="store_true", help="Start FastAPI server instead of running pipeline")
    parser.add_argument("--port", type=int, default=8000, help="Server port (default: 8000)")

    # Config file
    parser.add_argument("--config", type=str, help="Path to project config JSON file")

    # Inline project config
    parser.add_argument("--sentry-org", type=str)
    parser.add_argument("--sentry-project", type=str)
    parser.add_argument("--sentry-token", type=str, help="Override SENTRY_TOKEN from .env")
    parser.add_argument("--github-repo", type=str, help="Format: owner/repo")
    parser.add_argument("--github-token", type=str, help="Override GITHUB_TOKEN from .env")
    parser.add_argument("--repo-path", type=str, help="Absolute path to local git clone")
    parser.add_argument("--base-branch", type=str, default="main")
    parser.add_argument("--jira-project-key", type=str)
    parser.add_argument("--test-command", type=str, default="")
    parser.add_argument("--max-retries", type=int, default=3)

    # Pipeline options
    parser.add_argument("--query", type=str, default="is:unresolved", help="Sentry issue query filter")
    parser.add_argument("--issue-id", type=str, help="Fix a specific issue by ID")
    parser.add_argument("--dry-run", action="store_true", help="Generate patches without applying/pushing")

    args = parser.parse_args()

    # Server mode
    if args.server:
        import uvicorn
        logger.info(f"Starting server on port {args.port}...")
        uvicorn.run("server:app", host="127.0.0.1", port=args.port, reload=True)
        return

    # Build project config
    project_config = _resolve_project_config(args)
    if not project_config:
        parser.print_help()
        sys.exit(1)

    # Run pipeline
    from models.schemas import PipelineRequest, ProjectConfig
    from server import _execute_pipeline, build_services

    project = ProjectConfig(**project_config)
    req = PipelineRequest(
        project=project,
        query=args.query,
        issue_id=args.issue_id,
        dry_run=args.dry_run,
    )

    logger.info("=" * 60)
    logger.info(f"Sentry Automation Pipeline")
    logger.info(f"Project: {project.sentry_org}/{project.sentry_project}")
    repo_display = project.repo_path or "(auto-clone)"
    logger.info(f"Repo: {project.github_repo} ({repo_display})")
    logger.info(f"Branch: {project.base_branch}")
    logger.info(f"Dry run: {args.dry_run}")
    logger.info("=" * 60)

    result = _execute_pipeline(req)

    # Print result
    print("\n" + "=" * 60)
    print(f"Status: {result.status}")
    print(f"Issues: {result.issues_total} total, {result.issues_filtered} filtered, {result.issues_fixed} fixed, {result.issues_failed} failed")

    if result.issue_results:
        print("\nIssues:")
        for r in result.issue_results:
            icon = {"fixed": "✓", "filtered": "~", "failed": "✗"}.get(r.status, "?")
            print(f"  {icon} [{r.status}] #{r.issue_id}: {r.title[:70]}")
            if r.files_changed:
                print(f"      Files: {', '.join(r.files_changed)}")
            if r.jira_ticket:
                print(f"      Jira: {r.jira_ticket}")

    if result.pr_url:
        print(f"\nPR: {result.pr_url}")
    if result.jira_tickets:
        print(f"Jira tickets: {len(result.jira_tickets)}")
    if result.error:
        print(f"\nError: {result.error}")
    print("=" * 60)

    # Exit with error code if pipeline failed
    sys.exit(0 if result.status in ("success", "dry_run") else 1)


def _resolve_project_config(args) -> dict:
    """Build project config dict from --config file or inline args."""

    # From config file
    if args.config:
        try:
            with open(args.config, "r") as f:
                config = json.load(f)
            logger.info(f"Loaded config from {args.config}")

            # CLI args override config file values
            if args.sentry_org:
                config["sentry_org"] = args.sentry_org
            if args.sentry_project:
                config["sentry_project"] = args.sentry_project
            if args.sentry_token:
                config["sentry_token"] = args.sentry_token
            if args.github_repo:
                config["github_repo"] = args.github_repo
            if args.github_token:
                config["github_token"] = args.github_token
            if args.repo_path:
                config["repo_path"] = args.repo_path
            if args.base_branch != "main":
                config["base_branch"] = args.base_branch
            if args.jira_project_key:
                config["jira_project_key"] = args.jira_project_key

            return config

        except FileNotFoundError:
            logger.error(f"Config file not found: {args.config}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config file: {e}")
            sys.exit(1)

    # From inline args
    if args.sentry_org and args.sentry_project and args.github_repo:
        config = {
            "sentry_org": args.sentry_org,
            "sentry_project": args.sentry_project,
            "sentry_token": args.sentry_token,
            "github_repo": args.github_repo,
            "github_token": args.github_token,
            "base_branch": args.base_branch,
            "jira_project_key": args.jira_project_key,
            "test_command": args.test_command,
            "max_retries": args.max_retries,
        }
        if args.repo_path:
            config["repo_path"] = args.repo_path
        return config

    logger.error("Provide either --config <file.json> or required args (--sentry-org, --sentry-project, --github-repo)")
    return None


if __name__ == "__main__":
    main()
