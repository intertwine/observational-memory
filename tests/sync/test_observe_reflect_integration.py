from unittest.mock import patch

from observational_memory.config import Config
from observational_memory.observe import run_observer
from observational_memory.reflect import run_reflector
from observational_memory.sync.config import TransportConfig, initialize_cluster_config
from observational_memory.sync.store import ClusterStore
from observational_memory.transcripts import Message


def _messages():
    return [
        Message("user", "Please remember this cluster fact.", "2026-05-08T12:00:00Z", "codex"),
        Message("assistant", "I will.", "2026-05-08T12:00:01Z", "codex"),
        Message("user", "It relates to sync.", "2026-05-08T12:00:02Z", "codex"),
        Message("assistant", "Got it.", "2026-05-08T12:00:03Z", "codex"),
        Message("user", "Use filesystem transport.", "2026-05-08T12:00:04Z", "codex"),
    ]


def _init_cluster(tmp_path):
    config = Config(
        memory_dir=tmp_path / "memory",
        env_file=tmp_path / "config" / "env",
        min_messages=3,
        observation_retention_days=36500,
    )
    initialize_cluster_config(
        config,
        name="Test",
        node_alias="node-a",
        transports=[TransportConfig(type="filesystem", path=str(tmp_path / "shared"))],
    )
    store = ClusterStore.from_config(config)
    store.ensure_layout()
    store.append_record(
        kind="node_membership",
        namespace=store.cluster_config.default_namespace,
        source={"agent": "test"},
        payload={
            "operation": "add",
            "node_id": store.cluster_config.node_id,
            "alias": store.cluster_config.node_alias,
            "signing_public_key": store.keypair.signing_public_key_b64,
            "encryption_public_key": store.keypair.encryption_public_key_b64,
            "created_at": "2026-05-08T12:00:00Z",
        },
    )
    return config


@patch("observational_memory.observe.compress")
def test_cluster_enabled_observe_writes_record_and_materializes(mock_compress, tmp_path):
    mock_compress.return_value = "# Observations\n\n## 2026-05-08\n\n- cluster observe"
    config = _init_cluster(tmp_path)

    result = run_observer(_messages(), config, dry_run=False)

    assert result
    store = ClusterStore.from_config(config)
    assert len(store.list_records(kind="observation")) == 1
    assert "cluster observe" in config.observations_path.read_text()


@patch("observational_memory.observe.compress")
def test_cluster_enabled_observe_dry_run_writes_nothing(mock_compress, tmp_path):
    mock_compress.return_value = "# Observations\n\n## 2026-05-08\n\n- dry run"
    config = _init_cluster(tmp_path)

    run_observer(_messages(), config, dry_run=True)

    assert not ClusterStore.from_config(config).list_records(kind="observation")


@patch("observational_memory.reflect.compress")
@patch("observational_memory.observe.compress")
def test_cluster_reflector_writes_snapshot(mock_observe_compress, mock_reflect_compress, tmp_path):
    mock_observe_compress.return_value = "# Observations\n\n## 2026-05-08\n\n- reflect me"
    mock_reflect_compress.return_value = "# Reflections\n\n## Core Identity\n- Reflected"
    config = _init_cluster(tmp_path)
    run_observer(_messages(), config, dry_run=False)

    result = run_reflector(config, dry_run=False)

    assert result
    store = ClusterStore.from_config(config)
    assert len(store.list_records(kind="reflection_snapshot")) == 1
    assert "Reflected" in config.reflections_path.read_text()


@patch("observational_memory.reflect.compress")
@patch("observational_memory.observe.compress")
def test_cluster_reflector_stays_legacy_even_under_sectioned_strategy(
    mock_observe_compress, mock_reflect_compress, tmp_path, monkeypatch
):
    # Cluster mode builds its own cross-machine merge system prompt and is pinned
    # to the LEGACY chunked path regardless of OM_REFLECTOR_STRATEGY. If it routed
    # through sectioned, the sectioned prompt would replace the merge prompt and
    # the model would never see the merge guidance. Force a large merged corpus so
    # the chunked branch runs, set strategy=sectioned, and assert the merge prompt
    # (not the sectioned section-patch prompt) reached compress.
    monkeypatch.setenv("OM_REFLECTOR_STRATEGY", "sectioned")
    monkeypatch.setenv("OM_REFLECTOR_MAX_INPUT_TOKENS", "2000")  # tiny budget -> chunked
    mock_observe_compress.return_value = "# Observations\n\n## 2026-05-08\n\n- " + ("x " * 5000)
    captured: list[str] = []

    def fake_reflect(system_prompt, user_content, config, **kwargs):
        captured.append(system_prompt)
        # A legacy whole-document rewrite is expected; return a valid doc.
        return "# Reflections\n\n## Core Identity\n- Reflected once\n"

    mock_reflect_compress.side_effect = fake_reflect
    config = _init_cluster(tmp_path)
    run_observer(_messages(), config, dry_run=False)

    run_reflector(config, dry_run=False)

    assert captured, "cluster reflector never called compress"
    # The legacy whole-document reflector prompt must be in force (so a merge
    # system prompt can be appended to it), and the sectioned section-patch
    # envelope prompt must NOT have replaced it.
    assert any("condense accumulated observations" in p for p in captured)
    assert not any("SECTION_HANDLE" in p for p in captured)
