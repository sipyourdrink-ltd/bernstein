"""Environment digest computation for plan governance."""

import hashlib
import os
import subprocess


class DigestMismatchError(Exception):
    """Raised when environment digest mismatch is detected."""

    def __init__(self, expected: str, actual: str, details: str):
        self.expected = expected
        self.actual = actual
        self.details = details
        super().__init__(f"Environment digest mismatch. Expected: {expected}, Actual: {actual}. Details: {details}")


def _hash_file(filepath: str) -> str:
    """Return the SHA256 hash of a file's content as a hex string."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _get_git_head(repo_root: str) -> str:
    """Get the current git HEAD commit SHA."""
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, check=True, text=True)
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fallback to reading HEAD file directly
        head_path = os.path.join(repo_root, ".git", "HEAD")
        if not os.path.exists(head_path):
            return "HEAD_UNKNOWN"
        with open(head_path) as f:
            ref = f.read().strip()
        if ref.startswith("ref: "):
            ref_path = os.path.join(repo_root, ".git", ref[5:])
            if os.path.exists(ref_path):
                with open(ref_path) as f:
                    return f.read().strip()
        return "HEAD_UNKNOWN"


def compute_environment_digest(repo_root: str, plan) -> str:
    """
    Compute environment digest that captures the current state of files/config the plan depends on.

    Args:
        repo_root: Root directory of the git repository
        plan: Plan object containing touched_files and config_files attributes

    Returns:
        Hexadecimal digest string

    Digest includes:
        - git HEAD sha
        - Tracked file hashes for files the plan touches
        - Relevant config file contents
    """
    # Get git HEAD SHA
    git_head = _get_git_head(repo_root)

    # Get touched files and config files from plan
    touched_files = getattr(plan, "touched_files", [])
    config_files = getattr(plan, "config_files", [])

    # Compute hashes for touched files
    touched_hashes = []
    for file_path in sorted(touched_files):
        full_path = os.path.join(repo_root, file_path)
        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Touched file not found: {full_path}")
        touched_hashes.append(_hash_file(full_path))

    # Compute hashes for config files
    config_hashes = []
    for file_path in sorted(config_files):
        full_path = os.path.join(repo_root, file_path)
        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Config file not found: {full_path}")
        config_hashes.append(_hash_file(full_path))

    # Combine all components deterministically
    components = [git_head, *touched_hashes, *config_hashes]
    digest_input = "\n".join(components)
    return hashlib.sha256(digest_input.encode()).hexdigest()


def compare_digests(expected: str, actual: str) -> bool:
    """
    Compare two environment digests for equality.

    Args:
        expected: Expected digest string
        actual: Actual digest string

    Returns:
        True if digests match, False otherwise
    """
    return expected == actual
