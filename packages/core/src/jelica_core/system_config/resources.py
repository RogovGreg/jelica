from __future__ import annotations

import os
from pathlib import Path

_CGROUP_V2_CPU_MAX_PATH = Path("/sys/fs/cgroup/cpu.max")
_CGROUP_V1_CPU_QUOTA_PATH = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
_CGROUP_V1_CPU_PERIOD_PATH = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")


def detect_available_logical_cpu_count() -> int:
    """Return a conservative logical CPU count available to this process."""

    candidates: list[int] = []

    hardware_count = os.cpu_count()
    if hardware_count is not None and hardware_count > 0:
        candidates.append(hardware_count)

    affinity_count = _detect_process_affinity_cpu_count()
    if affinity_count is not None:
        candidates.append(affinity_count)

    cgroup_quota_count = _detect_cgroup_cpu_quota_count()
    if cgroup_quota_count is not None:
        candidates.append(cgroup_quota_count)

    return max(1, min(candidates, default=1))


def _detect_process_affinity_cpu_count() -> int | None:
    affinity_getter = getattr(os, "sched_getaffinity", None)
    if not callable(affinity_getter):
        return None

    try:
        affinity_count = len(affinity_getter(0))
    except (OSError, NotImplementedError):
        return None
    return affinity_count if affinity_count > 0 else None


def _detect_cgroup_cpu_quota_count() -> int | None:
    v2_count = _read_cgroup_v2_cpu_quota_count()
    if v2_count is not None:
        return v2_count
    return _read_cgroup_v1_cpu_quota_count()


def _read_cgroup_v2_cpu_quota_count() -> int | None:
    try:
        raw_value = _CGROUP_V2_CPU_MAX_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    components = raw_value.split()
    if len(components) != 2 or components[0] == "max":
        return None
    return _quota_to_cpu_count(quota=components[0], period=components[1])


def _read_cgroup_v1_cpu_quota_count() -> int | None:
    try:
        quota = _CGROUP_V1_CPU_QUOTA_PATH.read_text(encoding="utf-8").strip()
        period = _CGROUP_V1_CPU_PERIOD_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return _quota_to_cpu_count(quota=quota, period=period)


def _quota_to_cpu_count(*, quota: str, period: str) -> int | None:
    try:
        quota_value = int(quota)
        period_value = int(period)
    except ValueError:
        return None
    if quota_value <= 0 or period_value <= 0:
        return None

    return max(1, quota_value // period_value)
