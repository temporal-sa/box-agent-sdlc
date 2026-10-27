"""Adapters for external systems. Importing these modules never creates clients."""

from .github import GitHubClient, GitHubError, HeadChanged, RetryableGitHubError
from .workspaces import GitWorkspaceManager, Workspace

__all__ = [
    "GitHubClient",
    "GitHubError",
    "GitWorkspaceManager",
    "HeadChanged",
    "RetryableGitHubError",
    "Workspace",
]
