"""Optional LAN discovery seam for future OM Cluster transports."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class DiscoveryAdvertisement:
    service: str
    protocol: str
    cluster_hash: str
    node_id: str
    port: int


def build_advertisement(*, cluster_id: str, node_id: str, port: int) -> DiscoveryAdvertisement:
    return DiscoveryAdvertisement(
        service="_om-sync._tcp.local",
        protocol="om-sync/1",
        cluster_hash="sha256_" + sha256(cluster_id.encode("utf-8")).hexdigest(),
        node_id=node_id,
        port=port,
    )


def discovery_available() -> bool:
    return False
