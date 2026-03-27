"""Loads project configs from the projects/ directory.

Each project is a JSON file. Lookup is by sentry_project slug.
"""
import json
import logging
import os
from typing import Optional

from models.schemas import ProjectConfig

logger = logging.getLogger(__name__)


def load_all_projects(projects_dir: str = "projects") -> list[ProjectConfig]:
    """Load all project configs from JSON files in the projects directory."""
    configs = []
    if not os.path.isdir(projects_dir):
        logger.warning(f"[PROJECTS] Directory not found: {projects_dir}")
        return configs

    for filename in os.listdir(projects_dir):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(projects_dir, filename)
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            configs.append(ProjectConfig(**data))
            logger.info(f"[PROJECTS] Loaded: {filename} → {data.get('sentry_org')}/{data.get('sentry_project')}")
        except Exception as e:
            logger.error(f"[PROJECTS] Failed to load {filename}: {e}")

    return configs


def find_project_by_sentry_slug(sentry_project: str, projects_dir: str = "projects") -> Optional[ProjectConfig]:
    """Find a project config by Sentry project slug."""
    configs = load_all_projects(projects_dir)
    for config in configs:
        if config.sentry_project == sentry_project:
            return config
    return None


def find_project_by_sentry_org_and_slug(
    sentry_org: str, sentry_project: str, projects_dir: str = "projects"
) -> Optional[ProjectConfig]:
    """Find a project config by both org and project slug (more precise)."""
    configs = load_all_projects(projects_dir)
    for config in configs:
        if config.sentry_org == sentry_org and config.sentry_project == sentry_project:
            return config
    return None
