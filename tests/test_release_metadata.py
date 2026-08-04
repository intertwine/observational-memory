import json
import tomllib
from pathlib import Path

from observational_memory import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_release_metadata_versions_match():
    """Keep package and Cowork plugin release metadata on one version."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    cowork_dir = REPO_ROOT / "src" / "observational_memory" / "cowork_plugin"
    cowork_version = json.loads((cowork_dir / "version.json").read_text())
    cowork_manifest = json.loads((cowork_dir / ".claude-plugin" / "plugin.json").read_text())

    assert {
        pyproject["project"]["version"],
        __version__,
        cowork_version["version"],
        cowork_manifest["version"],
    } == {__version__}
