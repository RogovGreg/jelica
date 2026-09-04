from __future__ import annotations

import pytest


@pytest.fixture
def default_resolved_alignment_block() -> dict[str, object]:
    return {
        "mode": "compute",
        "engine": "mafft",
        "construction": "joint",
        "mafft": {
            "strategy": "auto",
            "direction_adjustment": "none",
            "memory_mode": "auto",
            "threads": 1,
            "gap_open_penalty": None,
            "offset": None,
            "progressive_threads": "auto",
            "iterative_threads": "auto",
        },
    }


@pytest.fixture
def default_resolved_comparative_analysis_block() -> dict[str, object]:
    return {
        "enabled": True,
        "statistics": {"enabled": True},
        "sequence_differences": {
            "enabled": True,
            "substitutions": True,
            "insertions": True,
            "deletions": True,
            "symbol_policy": {"uracil_thymine_equivalent": False},
        },
        "reference": {"mode": "auto"},
        "pairwise": {
            "enabled": False,
            "all": False,
            "pairs_orientation": "directed",
            "groups": [],
            "pairs": [],
        },
    }


@pytest.fixture
def default_resolved_distance_matrix_block() -> dict[str, object]:
    return {
        "enabled": True,
        "model": "p_distance",
    }


@pytest.fixture
def default_resolved_phylogenetic_tree_block() -> dict[str, object]:
    return {
        "enabled": True,
        "method": "neighbor_joining",
        "rooting": "midpoint",
    }


@pytest.fixture
def default_resolved_clade_detection_block() -> dict[str, object]:
    return {
        "enabled": False,
        "method": "max_pairwise_distance",
        "max_within_clade_distance": None,
    }
