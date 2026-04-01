"""Tests for Homebrew formula generation."""

from scripts.generate_homebrew_formula import Artifact, render_formula


def test_render_formula_uses_virtualenv_install_with_resources_for_sdist_root():
    expected_root_url = (
        "https://files.pythonhosted.org/packages/source/o/observational_memory/observational_memory-0.3.1.tar.gz"
    )
    formula = render_formula(
        class_name="ObservationalMemory",
        desc="Cross-agent observational memory",
        homepage="https://github.com/intertwine/observational-memory",
        root=Artifact(
            name="observational-memory",
            version="0.3.1",
            url=expected_root_url,
            sha256="sdistsha",
        ),
        license_name="MIT",
        python_dep="python@3.13",
        common_resources=[],
        arm_resources=[],
        intel_resources=[],
    )

    assert 'resource "observational-memory"' not in formula
    assert f'url "{expected_root_url}"' in formula
    assert 'venv = virtualenv_create(libexec, "python3.13")' in formula
    assert "wheel = buildpath/File.basename(resource.url)" in formula
    assert "cp resource.cached_download, wheel" in formula
    assert "venv.pip_install_and_link(buildpath)" in formula
    assert "root_wheel = buildpath/File.basename(cached_download)" not in formula
    assert "cp cached_download, root_wheel" not in formula
