import os


REPOSITORY_DIRECTORY = "repository"


def repository_path(workspace_path: str) -> str:
    """Return the Git checkout path for an issue-level workspace."""
    return os.path.join(os.path.abspath(workspace_path), REPOSITORY_DIRECTORY)
