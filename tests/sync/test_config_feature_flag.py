import pytest

from observational_memory.config import Config
from observational_memory.sync.config import (
    TransportConfig,
    clear_cluster_feature_cache,
    cluster_feature_enabled,
    create_invite_token,
    initialize_cluster_config,
    load_cluster_config,
    parse_invite_token,
)


def test_cluster_paths_are_isolated(isolated_om_home):
    config = Config()

    assert config.cluster_config_path == isolated_om_home / "config" / "observational-memory" / "cluster.toml"
    assert config.cluster_keys_dir == isolated_om_home / "config" / "observational-memory" / "cluster-keys"
    assert config.clusters_dir == isolated_om_home / "data" / "observational-memory" / "clusters"


def test_cluster_feature_disabled_without_valid_config(isolated_om_home, monkeypatch):
    clear_cluster_feature_cache()
    config = Config()

    assert cluster_feature_enabled(config) is False

    monkeypatch.setenv("OM_CLUSTER_ENABLED", "1")
    assert cluster_feature_enabled(config) is False


def test_cluster_feature_requires_config_enablement(isolated_om_home, monkeypatch):
    clear_cluster_feature_cache()
    config = Config()
    initialize_cluster_config(
        config,
        name="Test",
        node_alias="node-a",
        transports=[TransportConfig(type="filesystem", path=str(isolated_om_home / "shared"))],
    )

    assert cluster_feature_enabled(config) is True

    monkeypatch.setenv("OM_CLUSTER_ENABLED", "0")
    assert cluster_feature_enabled(config) is False

    loaded = load_cluster_config(config)
    assert loaded is not None
    assert loaded.node_alias == "node-a"


def test_cluster_feature_cache_reuses_unchanged_key_state(isolated_om_home, monkeypatch):
    clear_cluster_feature_cache()
    config = Config()
    initialize_cluster_config(
        config,
        name="Test",
        node_alias="node-a",
        transports=[TransportConfig(type="filesystem", path=str(isolated_om_home / "shared"))],
    )
    calls = {"node": 0, "secret": 0}
    import observational_memory.sync.config as sync_config

    original_load_node = sync_config.load_node_keypair
    original_load_secret = sync_config.load_cluster_secret

    def counted_load_node(*args, **kwargs):
        calls["node"] += 1
        return original_load_node(*args, **kwargs)

    def counted_load_secret(*args, **kwargs):
        calls["secret"] += 1
        return original_load_secret(*args, **kwargs)

    monkeypatch.setattr(sync_config, "load_node_keypair", counted_load_node)
    monkeypatch.setattr(sync_config, "load_cluster_secret", counted_load_secret)

    assert cluster_feature_enabled(config) is True
    assert cluster_feature_enabled(config) is True
    assert calls == {"node": 1, "secret": 1}

    monkeypatch.setenv("OM_CLUSTER_ENABLED", "0")
    assert cluster_feature_enabled(config) is False
    assert calls == {"node": 1, "secret": 1}


def test_cluster_feature_cache_invalidates_when_key_file_changes(isolated_om_home, monkeypatch):
    clear_cluster_feature_cache()
    config = Config()
    cluster_config = initialize_cluster_config(
        config,
        name="Test",
        node_alias="node-a",
        transports=[TransportConfig(type="filesystem", path=str(isolated_om_home / "shared"))],
    )
    assert cluster_feature_enabled(config) is True

    (config.cluster_keys_dir / cluster_config.id / "cluster.key").unlink()

    assert cluster_feature_enabled(config) is False


def test_cluster_key_directories_are_owner_only(isolated_om_home):
    config = Config()
    cluster_config = initialize_cluster_config(config, name="Test", node_alias="node-a")

    assert oct(config.cluster_keys_dir.stat().st_mode & 0o777) == "0o700"
    assert oct((config.cluster_keys_dir / cluster_config.id).stat().st_mode & 0o777) == "0o700"


def test_invite_expiration_is_enforced(isolated_om_home):
    config = Config()
    cluster_config = initialize_cluster_config(config, name="Test", node_alias="node-a")
    token = create_invite_token(config, cluster_config, expires="-1", mode="trusted-direct")

    with pytest.raises(ValueError, match="expired"):
        parse_invite_token(token)
