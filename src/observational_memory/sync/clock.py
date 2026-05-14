"""Hybrid logical clock utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, order=True)
class HybridLogicalTimestamp:
    wall_time: datetime
    counter: int
    node_id: str

    def __post_init__(self) -> None:
        wall = self.wall_time
        if wall.tzinfo is None:
            wall = wall.replace(tzinfo=timezone.utc)
        else:
            wall = wall.astimezone(timezone.utc)
        object.__setattr__(self, "wall_time", wall)

    def __str__(self) -> str:
        wall = self.wall_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return f"{wall}-{self.counter:06d}-{self.node_id}"


def parse_hlc(value: str) -> HybridLogicalTimestamp:
    wall_raw, counter_raw, node_id = value.rsplit("-", 2)
    wall = datetime.strptime(wall_raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return HybridLogicalTimestamp(wall, int(counter_raw), node_id)


def tick(previous: HybridLogicalTimestamp | None, node_id: str, now: datetime | None = None) -> HybridLogicalTimestamp:
    now = _utc(now)
    if previous is None:
        return HybridLogicalTimestamp(now, 0, node_id)
    if now <= previous.wall_time:
        return HybridLogicalTimestamp(previous.wall_time, previous.counter + 1, node_id)
    return HybridLogicalTimestamp(now, 0, node_id)


def merge(
    previous: HybridLogicalTimestamp | None,
    remote: HybridLogicalTimestamp,
    node_id: str,
    now: datetime | None = None,
) -> HybridLogicalTimestamp:
    now = _utc(now)
    if previous is None:
        if now <= remote.wall_time:
            return HybridLogicalTimestamp(remote.wall_time, remote.counter + 1, node_id)
        return HybridLogicalTimestamp(now, 0, node_id)

    max_wall = max(now, previous.wall_time, remote.wall_time)
    if max_wall == previous.wall_time == remote.wall_time:
        counter = max(previous.counter, remote.counter) + 1
    elif max_wall == previous.wall_time:
        counter = previous.counter + 1
    elif max_wall == remote.wall_time:
        counter = remote.counter + 1
    else:
        counter = 0
    return HybridLogicalTimestamp(max_wall, counter, node_id)


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
