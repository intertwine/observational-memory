# OM Cluster P2P Evaluation

OM Cluster direct P2P uses explicit HTTP peer endpoints for the first implementation.

## Selected Approach

- Use local HTTP peer endpoints with the same opaque artifact contract as relay.
- Configure peers explicitly with `p2p:http://host:port[,http://host2:port]`.
- Treat Tailscale, LAN routing, or operator-managed tunnels as the supported reachability layer.
- Keep discovery out of trust. A reachable peer is only a transport endpoint; membership still comes from signed cluster records and approval state.

## Alternatives Considered

- mDNS/DNS-SD discovery: useful for LAN hints, but easy to confuse with trust. This should remain optional discovery metadata after the explicit transport is stable.
- QUIC or Iroh/libp2p-style transports: attractive for NAT traversal, but the dependency and packaging footprint is too high for the base OM package today.
- Hosted relay only: simpler operationally, but it does not cover direct trusted-network use cases.

## Security Model

Direct P2P endpoints move only the same objects as filesystem and relay transports: encrypted/signed records, heads, public node metadata, join requests, and join approvals. They do not receive node private keys, cluster data keys, provider env files, generated Markdown, or plaintext memory.

All authorization remains local. OM verifies signatures, membership, revocation, tombstones, key epochs, payload hashes, and join approval state after fetching bytes.

## Platform And Dependency Footprint

The client uses stdlib HTTP through the existing relay client adapter. Base installation has no P2P dependency. A production serving command or packaged peer daemon can be added later without changing the record or trust model.

## Current Limitations

- No automatic NAT traversal.
- No mDNS discovery yet.
- No packaged long-running peer server yet; tests use loopback HTTP fixtures.
- Peer availability failures are sync diagnostics only and must not block local observe, reflect, context, or search.
