"""Vector-clock frontier helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal


def frontier_from_records(records: Iterable[object]) -> dict[str, int]:
    frontier: dict[str, int] = {}
    for record in records:
        node_id = getattr(record, "node_id")
        node_seq = getattr(record, "node_seq")
        frontier[node_id] = max(frontier.get(node_id, 0), node_seq)
    return frontier


def frontier_covers(a: dict[str, int], b: dict[str, int]) -> bool:
    return all(a.get(node, 0) >= seq for node, seq in b.items())


def frontier_join(*frontiers: dict[str, int]) -> dict[str, int]:
    joined: dict[str, int] = {}
    for frontier in frontiers:
        for node, seq in frontier.items():
            joined[node] = max(joined.get(node, 0), seq)
    return joined


def frontier_compare(
    a: dict[str, int],
    b: dict[str, int],
) -> Literal["covers", "covered_by", "equal", "incomparable"]:
    a_covers_b = frontier_covers(a, b)
    b_covers_a = frontier_covers(b, a)
    if a_covers_b and b_covers_a:
        return "equal"
    if a_covers_b:
        return "covers"
    if b_covers_a:
        return "covered_by"
    return "incomparable"
