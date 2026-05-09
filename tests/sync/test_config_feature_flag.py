from observational_memory.config import Config
from observational_memory.sync.config import (
    TransportConfig,
    cluster_feature_enabled,
    initialize_cluster_config,
    load_cluster_config,
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
