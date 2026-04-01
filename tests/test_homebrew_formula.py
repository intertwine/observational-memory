"""Tests for Homebrew formula generation."""

from scripts.generate_homebrew_formula import Artifact, render_formula


def test_render_formula_preserves_real_root_wheel_filename():
    formula = render_formula(
        class_name="ObservationalMemory",
        desc="Cross-agent observational memory",
        homepage="https://github.com/intertwine/observational-memory",
        root=Artifact(
            name="observational-memory",
            version="0.3.1",
            url="https://files.pythonhosted.org/packages/wheel/o/observational_memory-0.3.1-py3-none-any.whl",
            sha256="wheelsha",
        ),
        license_name="MIT",
        python_dep="python@3.13",
        common_resources=[],
        arm_resources=[],
        intel_resources=[],
    )

    assert 'resource "observational-memory"' not in formula
    assert "root_wheel = buildpath/File.basename(cached_download)" in formula
    assert "cp cached_download, root_wheel" in formula
    assert 'root_wheel = buildpath/"observational-memory-wheel.whl"' not in formula
