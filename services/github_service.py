import logging
import os
import shutil
import subprocess
import tempfile
import time

from github import Github, GithubException

logger = logging.getLogger(__name__)


class GitOperationError(Exception):
    pass


class GitHubService:
    """Handles all git operations (local) and GitHub API (remote PR creation)."""

    def __init__(self, repo_path: str, base_branch: str, github_token: str, github_repo: str, test_command: str = ""):
        self.repo_path = repo_path
        self.base_branch = base_branch
        self.github_token = github_token
        self.github_repo = github_repo
        self.test_command = test_command

    # ── Local git operations ──────────────────────────────

    def _run_git(self, args: list[str]) -> str:
        cmd = ["git"] + args
        logger.info(f"[GIT] Running: {' '.join(cmd)} (cwd={self.repo_path})")
        result = subprocess.run(
            cmd, cwd=self.repo_path, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.error(f"[GIT] FAILED (exit={result.returncode}): {result.stderr.strip()}")
            raise GitOperationError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        if result.stdout.strip():
            logger.info(f"[GIT] Output: {result.stdout.strip()[:300]}")
        return result.stdout.strip()

    def prepare_branch(self, label: str) -> str:
        logger.info(f"[GIT] ── Preparing branch for: {label} ──")
        self._run_git(["fetch", "origin"])
        self._run_git(["checkout", self.base_branch])
        self._run_git(["pull", "origin", self.base_branch])

        timestamp = int(time.time())
        branch_name = f"fix/sentry-{label}-{timestamp}"
        logger.info(f"[GIT] Creating branch: {branch_name}")
        self._run_git(["checkout", "-b", branch_name])
        return branch_name

    def commit_and_push(self, branch_name: str, commit_message: str) -> None:
        logger.info("[GIT] ── Committing and pushing ──")
        self._run_git(["add", "-A"])
        status = self._run_git(["status", "--short"])
        logger.info(f"[GIT] Staged changes:\n{status}")
        self._run_git(["commit", "-m", commit_message])
        self._run_git(["push", "-u", "origin", branch_name])
        logger.info(f"[GIT] Pushed branch {branch_name} to origin")

    def cleanup(self, branch_name: str) -> None:
        logger.info(f"[GIT] ── Cleanup: switching to {self.base_branch}, deleting {branch_name} ──")
        try:
            self._run_git(["checkout", "--", "."])
            self._run_git(["checkout", self.base_branch])
            self._run_git(["branch", "-D", branch_name])
            logger.info("[GIT] Cleanup done")
        except GitOperationError as e:
            logger.warning(f"[GIT] Cleanup warning: {e}")

    # ── Temp clone operations ─────────────────────────────

    def clone_repo(self) -> str:
        """Clone the repo into a temp directory using HTTPS + token auth.
        Sets self.repo_path to the clone directory. Returns the temp dir path."""
        tmpdir = tempfile.mkdtemp(prefix="sentry-auto-")
        clone_url = f"https://x-access-token:{self.github_token}@github.com/{self.github_repo}.git"
        logger.info(f"[GIT] Cloning {self.github_repo} into {tmpdir}")
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", "--single-branch",
                 "--branch", self.base_branch, clone_url, tmpdir],
                capture_output=True, text=True, timeout=120, check=True,
            )
        except subprocess.CalledProcessError as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise GitOperationError(f"Clone failed: {e.stderr.strip()}")
        except subprocess.TimeoutExpired:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise GitOperationError("Clone timed out after 120s")
        self.repo_path = tmpdir
        logger.info(f"[GIT] Clone complete: {tmpdir}")
        return tmpdir

    @staticmethod
    def cleanup_clone(tmpdir: str) -> None:
        """Remove a temp clone directory. Only deletes paths inside the system temp dir."""
        if tmpdir and os.path.isdir(tmpdir) and tmpdir.startswith(tempfile.gettempdir()):
            logger.info(f"[GIT] Removing temp clone: {tmpdir}")
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ── GitHub API operations ─────────────────────────────

    def create_pull_request(self, branch_name: str, pr_title: str, pr_description: str) -> str:
        logger.info(f"[GITHUB] ── Creating Pull Request ──")
        logger.info(f"[GITHUB] Repo: {self.github_repo}")
        logger.info(f"[GITHUB] Branch: {branch_name} → {self.base_branch}")

        g = Github(self.github_token)
        repo = g.get_repo(self.github_repo)

        try:
            pr = repo.create_pull(
                title=pr_title, body=pr_description,
                head=branch_name, base=self.base_branch,
            )
            logger.info(f"[GITHUB] PR created: {pr.html_url}")
            try:
                pr.add_to_labels("auto-fix", "sentry")
            except GithubException:
                pass
            return pr.html_url

        except GithubException as e:
            logger.error(f"[GITHUB] PR creation failed: status={e.status} data={e.data}")
            if e.status == 422:
                pulls = repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch_name}")
                for existing_pr in pulls:
                    return existing_pr.html_url
            raise ValueError(f"Failed to create PR: {e}")

    # ── File operations on target repo ────────────────────

    def get_file_tree(self) -> str:
        if not self.repo_path:
            return ""
        try:
            result = subprocess.run(
                ["find", ".", "-type", "f",
                 "-not", "-path", "./.git/*",
                 "-not", "-path", "./node_modules/*",
                 "-not", "-path", "./.next/*",
                 "-not", "-path", "./dist/*",
                 "-not", "-path", "./__pycache__/*"],
                cwd=self.repo_path,
                capture_output=True, text=True, timeout=10,
            )
            files = result.stdout.strip().split("\n")
            relevant = [f for f in files if f.endswith(
                (".ts", ".tsx", ".js", ".jsx", ".py", ".json", ".css", ".html")
            )]
            if len(relevant) > 100:
                relevant = relevant[:100]
            return "\n".join(relevant)
        except Exception as e:
            logger.error(f"[GIT] File tree error: {e}")
            return ""

    def read_file(self, filepath: str) -> str:
        filepath = filepath.lstrip("./")
        full_path = os.path.join(self.repo_path, filepath)

        if not os.path.isfile(full_path):
            for prefix in ["src/", "app/", "lib/", ""]:
                candidate = os.path.join(self.repo_path, prefix, filepath)
                if os.path.isfile(candidate):
                    full_path = candidate
                    filepath = os.path.join(prefix, filepath)
                    break
            else:
                return ""

        try:
            with open(full_path, "r") as f:
                content = f.read()
            if len(content) > 4000:
                content = content[:4000] + "\n... (truncated)"
            return f"File: {filepath}\n```\n{content}\n```"
        except Exception:
            return ""

    def find_related_files(self, culprit: str) -> list[tuple[str, str]]:
        if not self.repo_path:
            return []
        try:
            result = subprocess.run(
                ["grep", "-rl", "--include=*.tsx", "--include=*.ts",
                 "--include=*.jsx", "--include=*.js", "--include=*.py",
                 "--exclude-dir=node_modules", "--exclude-dir=.git",
                 "--exclude-dir=.next",
                 culprit],
                cwd=self.repo_path,
                capture_output=True, text=True, timeout=10,
            )
            files = [f for f in result.stdout.strip().split("\n") if f]
            results = []
            for filepath in files[:3]:
                full_path = os.path.join(self.repo_path, filepath)
                with open(full_path, "r") as f:
                    content = f.read()
                if len(content) > 4000:
                    content = content[:4000] + "\n... (truncated)"
                results.append((filepath, content))
            return results
        except Exception:
            return []

    def run_tests(self) -> tuple[bool, str]:
        if not self.test_command:
            logger.info("[TESTS] Skipped — no test_command configured")
            return True, "Tests skipped — no TEST_COMMAND configured"

        logger.info(f"[TESTS] Running: {self.test_command}")
        try:
            result = subprocess.run(
                self.test_command, shell=True,
                cwd=self.repo_path,
                capture_output=True, text=True, timeout=300,
            )
            output = result.stdout + "\n" + result.stderr
            passed = result.returncode == 0
            logger.info(f"[TESTS] {'PASSED' if passed else 'FAILED'}")
            return passed, output.strip()
        except subprocess.TimeoutExpired:
            return False, "Tests timed out after 300s"
        except Exception as e:
            return False, f"Test runner error: {e}"
