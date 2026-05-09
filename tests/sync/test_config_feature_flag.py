import pytest

from observational_memory.config import Config
from observational_memory.sync.config import (
    TransportConfig,
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
    config = Config()

    assert cluster_feature_enabled(config) is False

    monkeypatch.setenv("OM_CLUSTER_ENABLED", "1")
    assert cluster_feature_enabled(config) is False


def test_cluster_feature_requires_config_enablement(isolated_om_home, monkeypatch):
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


def test_cluster_key_directories_are_owner_only(isolated_om_home):
    config = Config()
    cluster_config = initialize_cluster_config(config, name="Test", node_alias="node-a")

    assert oct(config.cluster_keys_dir.stat().st_mode & 0o777) == "0o700"
    assert oct((config.cluster_keys_dir / cluster_config.id).stat().st_mode & 0o777) == "0o700"


def test_invite_expiration_is_enforced(isolated_om_home):
    config = Config()
    cluster_config = initialize_cluster_config(config, name="Test", node_alias="node-a")
    token = create_invite_token(config, cluster_config, expires="-1")

    with pytest.raises(ValueError, match="expired"):
        parse_invite_token(token)
