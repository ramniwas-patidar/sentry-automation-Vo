from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Shared secrets and defaults — loaded from .env once at startup.
    Project-specific config is passed per-request via ProjectConfig.
    """
    # OpenAI (shared across all projects)
    OPENAI_API_KEY: str
    OPENAI_MODEL: str = "gpt-4o-mini"

    # Default tokens (used if not overridden per-project)
    SENTRY_TOKEN: str = ""
    GITHUB_TOKEN: str = ""
    SENTRY_BASE_URL: str = "https://sentry.io/api/0"

    # Default Jira creds (used if not overridden per-project)
    JIRA_DOMAIN: str = ""
    JIRA_EMAIL: str = ""
    JIRA_API_TOKEN: str = ""

    # Sentry webhook secret (for signature verification)
    SENTRY_CLIENT_SECRET: str = ""

    # Webhook settings
    WEBHOOK_COOLDOWN_SECONDS: int = 300  # 5 min debounce per project
    PROJECTS_DIR: str = "projects"  # directory containing project config JSONs

    # Logging
    LOG_DIR: str = "logs"  # directory for log files

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
