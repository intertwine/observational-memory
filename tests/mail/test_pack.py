"""Context-pack tests. The leak test is the point: ``scope=local`` reflection
entries must never appear in a pack, and opening verifies the manifest
all-or-nothing before any byte hits disk."""

from __future__ import annotations

import copy

import pytest

from observational_memory.config import Config
from observational_memory.mail.pack import PACK_FILES, PackError, build_context_pack, open_context_pack
from observational_memory.sync.crypto import sha256_id

SECRET = "SECRET-LOCAL-ONLY-FACT"
SHARED = "shared-cluster-fact"
UNSCOPED = "unscoped-profile-prose"


def _node_config(tmp_path) -> Config:
    config = Config(
        memory_dir=tmp_path / "memory",
        env_file=tmp_path / "config" / "env",
    )
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    return config


def _seed_memory(config: Config) -> None:
    config.profile_path.write_text(f"# Profile\n\n## Core Identity\n- {UNSCOPED}\n")
    config.active_path.write_text("# Active\n\n## Projects\n- working on om-mail\n")
    config.reflections_path.write_text(
        "# Reflections\n\n"
        "## Preferences & Opinions\n"
        f"- {SHARED} <!--om: scope=cluster-->\n"
        f"- {SECRET} <!--om: scope=local-->\n"
        f"  - {SECRET} nested continuation\n"
        "- unscoped reflection rides along\n"
    )


def test_pack_strips_scope_local_and_keeps_shareable(tmp_path):
    config = _node_config(tmp_path)
    _seed_memory(config)
    pack = build_context_pack(config, host_alias="node-a")

    blob = "\n".join(pack["files"].values())
    assert SECRET not in blob
    assert SHARED in pack["files"]["reflections.md"]
    assert "unscoped reflection rides along" in pack["files"]["reflections.md"]
    assert UNSCOPED in pack["files"]["profile.md"]
    assert pack["host_alias"] == "node-a"
    assert set(pack["files"]) == set(PACK_FILES)


def test_pack_manifest_hashes_match_filtered_content(tmp_path):
    config = _node_config(tmp_path)
    _seed_memory(config)
    pack = build_context_pack(config)

    assert set(pack["manifest"]) == set(pack["files"])
    for filename, text in pack["files"].items():
        assert pack["manifest"][filename] == sha256_id(text.encode("utf-8"))
        # Manifest is over the FILTERED bytes, not the raw on-disk file.
        assert SECRET not in text


def test_missing_files_are_omitted(tmp_path):
    config = _node_config(tmp_path)
    config.reflections_path.write_text("# Reflections\n\n## Notes\n- only file present\n")
    pack = build_context_pack(config)
    assert list(pack["files"]) == ["reflections.md"]


def test_empty_pack_fails_closed(tmp_path):
    config = _node_config(tmp_path)
    with pytest.raises(PackError):
        build_context_pack(config)


def test_unknown_include_name_rejected(tmp_path):
    config = _node_config(tmp_path)
    _seed_memory(config)
    with pytest.raises(PackError):
        build_context_pack(config, include=("../escape.md",))


def test_open_context_pack_round_trip(tmp_path):
    config = _node_config(tmp_path)
    _seed_memory(config)
    pack = build_context_pack(config)

    dest = tmp_path / "opened" / "pack-1"
    written = open_context_pack(pack, dest)
    assert sorted(path.name for path in written) == sorted(pack["files"])
    for path in written:
        assert path.parent == dest
        assert path.read_text() == pack["files"][path.name]
        assert SECRET not in path.read_text()


def test_tampered_file_fails_closed_and_writes_nothing(tmp_path):
    config = _node_config(tmp_path)
    _seed_memory(config)
    pack = build_context_pack(config)

    tampered = copy.deepcopy(pack)
    tampered["files"]["reflections.md"] += "- injected line\n"
    dest = tmp_path / "opened" / "tampered"
    with pytest.raises(PackError):
        open_context_pack(tampered, dest)
    assert not dest.exists()


def test_manifest_file_set_mismatch_fails_closed(tmp_path):
    config = _node_config(tmp_path)
    _seed_memory(config)
    pack = build_context_pack(config)

    extra = copy.deepcopy(pack)
    extra["files"]["evil.md"] = "surprise"
    dest = tmp_path / "opened" / "mismatch"
    with pytest.raises(PackError):
        open_context_pack(extra, dest)
    assert not dest.exists()


def test_unsafe_filename_fails_closed(tmp_path):
    payload = {
        "manifest": {"../escape.md": sha256_id(b"x")},
        "files": {"../escape.md": "x"},
    }
    dest = tmp_path / "opened" / "unsafe"
    with pytest.raises(PackError):
        open_context_pack(payload, dest)
    assert not dest.exists()
    assert not (tmp_path / "opened" / "escape.md").exists()
