#!/usr/bin/env python3
"""
Bump semantic version in pyproject.toml files.

Usage:
    python scripts/bump_version.py <pyproject_path> <bump_type>

Arguments:
    pyproject_path: Path to pyproject.toml file
    bump_type: One of 'patch', 'minor', 'major'

Example:
    python scripts/bump_version.py pyproject.toml patch
"""

import sys
from pathlib import Path


def parse_version(version_str: str) -> tuple[int, int, int]:
    """Parse semantic version string into (major, minor, patch) tuple."""
    parts = version_str.split(".")
    if len(parts) != 3:
        raise ValueError(f"Invalid version format: {version_str}. Expected 'major.minor.patch'")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError as e:
        raise ValueError(f"Invalid version format: {version_str}. All parts must be integers") from e


def bump_version(version_str: str, bump_type: str) -> str:
    """Bump version according to bump_type."""
    major, minor, patch = parse_version(version_str)

    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        raise ValueError(f"Invalid bump type: {bump_type}. Must be 'major', 'minor', or 'patch'")


def update_pyproject_version(pyproject_path: Path, bump_type: str) -> tuple[str, str]:
    """
    Update version in pyproject.toml file.

    Returns:
        Tuple of (old_version, new_version)
    """
    if not pyproject_path.exists():
        raise FileNotFoundError(f"pyproject.toml not found at: {pyproject_path}")

    content = pyproject_path.read_text()
    lines = content.splitlines()

    old_version = None
    new_version = None
    new_lines = []

    for line in lines:
        if line.strip().startswith("version = "):
            # Extract version string (handles both single and double quotes)
            if 'version = "' in line:
                quote = '"'
            elif "version = '" in line:
                quote = "'"
            else:
                raise ValueError(f"Unexpected version line format: {line}")

            # Extract current version
            start = line.index(quote) + 1
            end = line.index(quote, start)
            old_version = line[start:end]

            # Bump version
            new_version = bump_version(old_version, bump_type)

            # Replace version in line
            indent = line[: line.index("version")]
            new_line = f'{indent}version = "{new_version}"'
            new_lines.append(new_line)
        else:
            new_lines.append(line)

    if old_version is None:
        raise ValueError("No version field found in pyproject.toml")

    # Write updated content
    pyproject_path.write_text("\n".join(new_lines) + "\n")

    return old_version, new_version


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    pyproject_path = Path(sys.argv[1])
    bump_type = sys.argv[2].lower()

    if bump_type not in ["major", "minor", "patch"]:
        print(
            f"Error: Invalid bump type '{bump_type}'. Must be 'major', 'minor', or 'patch'",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        old_version, new_version = update_pyproject_version(pyproject_path, bump_type)
        print(f"{old_version} → {new_version}")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
