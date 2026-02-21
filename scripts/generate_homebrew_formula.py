#!/usr/bin/env python3
"""Generate a Homebrew formula for observational-memory from PyPI wheel artifacts."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen


@dataclass(frozen=True)
class Artifact:
    """Represents a downloadable package artifact."""

    name: str
    version: str
    url: str
    sha256: str


def normalize_name(name: str) -> str:
    """Normalize package names per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def ruby_class_name(formula_name: str) -> str:
    """Convert formula name to a valid Ruby class name."""
    parts = re.split(r"[^A-Za-z0-9]+", formula_name)
    class_name = "".join(part.capitalize() for part in parts if part)
    if not class_name:
        raise ValueError(f"Invalid formula name: {formula_name!r}")
    if class_name[0].isdigit():
        class_name = f"Formula{class_name}"
    return class_name


def load_project_metadata(pyproject_path: Path) -> dict[str, str]:
    """Load minimal metadata from pyproject.toml."""
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    project = data.get("project", {})
    urls = project.get("urls", {})
    return {
        "name": project["name"],
        "version": project["version"],
        "description": project["description"],
        "homepage": urls.get("Homepage") or urls.get("Repository") or "",
        "license": project.get("license", "Unknown"),
    }


def python_abi_tag(python_version: str) -> str:
    """Convert dotted Python version (3.13) to CP ABI tag (cp313)."""
    parts = python_version.split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError(f"Invalid --python-version: {python_version!r}. Expected like 3.13")
    return f"cp{parts[0]}{parts[1]}"


def resolve_pip_report(
    package_name: str,
    package_version: str,
    *,
    platform_tag: str,
    python_version: str,
) -> list[dict]:
    """Resolve package and transitive dependencies using pip's JSON report."""
    from tempfile import TemporaryDirectory

    requirement = f"{package_name}=={package_version}"
    abi = python_abi_tag(python_version)

    with TemporaryDirectory(prefix="homebrew-formula-") as tmpdir:
        report_path = Path(tmpdir) / "report.json"
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--dry-run",
            "--ignore-installed",
            "--only-binary=:all:",
            "--platform",
            platform_tag,
            "--implementation",
            "cp",
            "--python-version",
            python_version,
            "--abi",
            abi,
            "--report",
            str(report_path),
            requirement,
        ]

        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            raise RuntimeError(
                f"Failed to resolve dependencies for platform {platform_tag}. "
                "Run this script with pip available, e.g.:\n"
                "  uv run --with pip python scripts/generate_homebrew_formula.py\n"
                f"\nOriginal error:\n{stderr}"
            )

        with report_path.open("r", encoding="utf-8") as f:
            report = json.load(f)

    install_items = report.get("install", [])
    if not install_items:
        raise RuntimeError(f"pip report had no install entries for platform {platform_tag}")
    return install_items


def extract_artifacts(install_items: list[dict], package_name: str) -> tuple[Artifact, dict[str, Artifact]]:
    """Extract root artifact and dependency artifacts from a pip report."""
    root_key = normalize_name(package_name)
    root: Artifact | None = None
    resources: dict[str, Artifact] = {}

    for item in install_items:
        metadata = item.get("metadata", {})
        download_info = item.get("download_info", {})
        archive_info = download_info.get("archive_info", {})

        name = metadata.get("name")
        version = metadata.get("version")
        url = download_info.get("url")
        hash_value = archive_info.get("hash", "")

        if not name or not version or not url:
            continue
        if not hash_value.startswith("sha256="):
            raise RuntimeError(f"Missing sha256 hash for {name} ({url}) in pip report")

        artifact = Artifact(name=name, version=version, url=url, sha256=hash_value.split("=", 1)[1])
        key = normalize_name(name)

        if key == root_key:
            root = artifact
            continue

        existing = resources.get(key)
        if existing and existing != artifact:
            raise RuntimeError(f"Multiple artifacts resolved for dependency {name}: {existing.url} vs {artifact.url}.")
        resources[key] = artifact

    if root is None:
        raise RuntimeError(f"Could not find root package artifact for {package_name}")

    return root, resources


def fetch_sdist_artifact(package_name: str, package_version: str) -> Artifact:
    """Fetch source distribution artifact metadata from PyPI."""
    encoded_name = quote(package_name)
    encoded_version = quote(package_version)
    endpoint = f"https://pypi.org/pypi/{encoded_name}/{encoded_version}/json"
    try:
        with urlopen(endpoint) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"Failed to fetch {endpoint}: {e}") from e

    candidates = payload.get("urls", [])
    sdist = next((entry for entry in candidates if entry.get("packagetype") == "sdist"), None)
    if sdist is None:
        raise RuntimeError(f"No sdist found for {package_name}=={package_version}")

    url = sdist.get("url")
    sha256 = (sdist.get("digests") or {}).get("sha256")
    if not url or not sha256:
        raise RuntimeError(f"Missing sdist url/sha256 for {package_name}=={package_version}")

    return Artifact(name=package_name, version=package_version, url=url, sha256=sha256)


def partition_resources(
    arm_resources: dict[str, Artifact],
    intel_resources: dict[str, Artifact],
) -> tuple[list[Artifact], list[Artifact], list[Artifact]]:
    """Split resources into shared, arm-only URLs, and intel-only URLs."""
    arm_keys = set(arm_resources)
    intel_keys = set(intel_resources)
    if arm_keys != intel_keys:
        missing_arm = sorted(intel_keys - arm_keys)
        missing_intel = sorted(arm_keys - intel_keys)
        raise RuntimeError(
            "Resolved dependency sets differ across architectures. "
            f"Missing on arm: {missing_arm}; missing on intel: {missing_intel}"
        )

    common: list[Artifact] = []
    arm_specific: list[Artifact] = []
    intel_specific: list[Artifact] = []

    for key in sorted(arm_keys):
        arm_artifact = arm_resources[key]
        intel_artifact = intel_resources[key]
        if arm_artifact.url == intel_artifact.url and arm_artifact.sha256 == intel_artifact.sha256:
            common.append(arm_artifact)
        else:
            arm_specific.append(arm_artifact)
            intel_specific.append(intel_artifact)

    return common, arm_specific, intel_specific


def render_resource(resource: Artifact, indent: str = "  ") -> str:
    """Render a single Homebrew resource block."""
    lines = [
        f'{indent}resource "{normalize_name(resource.name)}" do',
        f'{indent}  url "{resource.url}"',
        f'{indent}  sha256 "{resource.sha256}"',
        f"{indent}end",
    ]
    return "\n".join(lines)


def render_resource_section(resources: list[Artifact], indent: str = "  ") -> str:
    """Render a sequence of resource blocks."""
    return "\n\n".join(render_resource(resource, indent=indent) for resource in resources)


def render_formula(
    *,
    class_name: str,
    desc: str,
    homepage: str,
    root: Artifact,
    root_wheel: Artifact,
    license_name: str,
    python_dep: str,
    common_resources: list[Artifact],
    arm_resources: list[Artifact],
    intel_resources: list[Artifact],
) -> str:
    """Render Homebrew formula Ruby source."""
    python_bin = python_dep
    if python_dep.startswith("python@"):
        python_bin = f"python{python_dep.split('@', 1)[1]}"

    sections: list[str] = [
        "# typed: strict",
        "# frozen_string_literal: true",
        "",
        f"# Formula for {normalize_name(root.name)}.",
        f"class {class_name} < Formula",
        "  include Language::Python::Virtualenv",
        "",
        f'  desc "{desc}"',
        f'  homepage "{homepage}"',
        f'  url "{root.url}"',
        f'  sha256 "{root.sha256}"',
        f'  license "{license_name}"',
        "",
        '  depends_on "jq"',
        f'  depends_on "{python_dep}"',
        "",
    ]

    if arm_resources:
        sections.append("  on_arm do")
        sections.append(render_resource_section(arm_resources, indent="    "))
        sections.append("  end")
        sections.append("")

    if intel_resources:
        sections.append("  on_intel do")
        sections.append(render_resource_section(intel_resources, indent="    "))
        sections.append("  end")
        sections.append("")

    sections.append("  preserve_rpath")
    sections.append("")

    if common_resources:
        sections.append(render_resource_section(common_resources))
        sections.append("")

    root_wheel_resource_name = f"{normalize_name(root.name)}-wheel"
    sections.append(
        render_resource(
            Artifact(
                name=root_wheel_resource_name,
                version=root_wheel.version,
                url=root_wheel.url,
                sha256=root_wheel.sha256,
            )
        )
    )
    sections.append("")

    sections.extend(
        [
            "  def install",
            f'    virtualenv_create(libexec, "{python_bin}")',
            f'    python = Formula["{python_dep}"].opt_bin/"{python_bin}"',
            "",
            "    resources.each do |resource|",
            f'      next if resource.name == "{root_wheel_resource_name}"',
            "",
            "      wheel = buildpath/File.basename(resource.url)",
            "      cp resource.cached_download, wheel",
            '      system python, "-m", "pip", "--python=#{libexec/"bin/python"}", "install", "--no-deps", wheel',
            "    end",
            "",
            f'    root_wheel = buildpath/"{root_wheel_resource_name}.whl"',
            f'    cp resource("{root_wheel_resource_name}").cached_download, root_wheel',
            '    system python, "-m", "pip", "--python=#{libexec/"bin/python"}", "install", "--no-deps", root_wheel',
            '    bin.install_symlink libexec/"bin/om"',
            "  end",
            "",
            "  test do",
            '    assert_match "Usage: om", shell_output("#{bin}/om --help")',
            '    assert_match "Observational Memory Status", shell_output("#{bin}/om status")',
            "  end",
            "end",
            "",
        ]
    )

    return "\n".join(sections)


def check_formula_name_conflict(formula_name: str) -> str | None:
    """Return warning text if a formula with this name already exists locally."""
    if not shutil.which("brew"):
        return None

    proc = subprocess.run(["brew", "formulae"], check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        return None

    existing_names = set(proc.stdout.splitlines())
    if formula_name in existing_names:
        return (
            f"Warning: formula name '{formula_name}' already exists in your local brew index. "
            "Use a fully-qualified install target (for example: <tap>/<formula>) to avoid ambiguity."
        )
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"), help="Path to pyproject.toml")
    parser.add_argument(
        "--formula-name",
        default="observational-memory",
        help="Homebrew formula name (used for class naming and conflict checks)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("packaging/homebrew/observational-memory.rb"),
        help="Output path for generated formula",
    )
    parser.add_argument(
        "--python-dep",
        default="python@3.13",
        help='Homebrew Python dependency name (example: "python@3.13")',
    )
    parser.add_argument(
        "--python-version",
        default="3.13",
        help='Target Python version for wheel resolution (example: "3.13")',
    )
    parser.add_argument(
        "--platform-arm",
        default="macosx_11_0_arm64",
        help="Target platform tag for Apple Silicon wheel resolution",
    )
    parser.add_argument(
        "--platform-intel",
        default="macosx_10_13_x86_64",
        help="Target platform tag for Intel macOS wheel resolution",
    )
    parser.add_argument("--package-name", default="", help="Override package name from pyproject")
    parser.add_argument("--package-version", default="", help="Override package version from pyproject")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    metadata = load_project_metadata(args.pyproject)

    package_name = args.package_name or metadata["name"]
    package_version = args.package_version or metadata["version"]

    arm_install = resolve_pip_report(
        package_name,
        package_version,
        platform_tag=args.platform_arm,
        python_version=args.python_version,
    )
    intel_install = resolve_pip_report(
        package_name,
        package_version,
        platform_tag=args.platform_intel,
        python_version=args.python_version,
    )

    arm_root, arm_resources = extract_artifacts(arm_install, package_name)
    intel_root, intel_resources = extract_artifacts(intel_install, package_name)

    if arm_root.url != intel_root.url or arm_root.sha256 != intel_root.sha256:
        raise RuntimeError(
            "Root artifact differs across arm/intel platforms. "
            "Use a platform-neutral root artifact before generating formula."
        )

    common_resources, arm_specific, intel_specific = partition_resources(arm_resources, intel_resources)
    root_sdist = fetch_sdist_artifact(package_name, package_version)

    formula_text = render_formula(
        class_name=ruby_class_name(args.formula_name),
        desc=metadata["description"],
        homepage=metadata["homepage"],
        root=root_sdist,
        root_wheel=arm_root,
        license_name=metadata["license"],
        python_dep=args.python_dep,
        common_resources=common_resources,
        arm_resources=arm_specific,
        intel_resources=intel_specific,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(formula_text, encoding="utf-8")

    print(f"Generated {args.output} for {package_name}=={package_version}")
    print(f"Root artifact: {root_sdist.url}")
    print(f"Resources: {len(common_resources)} common, {len(arm_specific)} arm-only, {len(intel_specific)} intel-only")

    warning = check_formula_name_conflict(args.formula_name)
    if warning:
        print(warning)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
