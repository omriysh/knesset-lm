"""
test_rrf_fuse.py

Tests for rrf_fuse (Reciprocal Rank Fusion).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from retrieval.hybrid import rrf_fuse


class TestBasicFusion:
    def test_basic_fusion(self):
        """Items appearing in multiple lists rank higher; 'b' and 'c' are in both."""
        result = rrf_fuse([["a", "b", "c"], ["b", "c", "d"]], k=60, top_k=3)
        assert len(result) <= 3
        # 'b' is rank-1 or rank-2 in each list → should have highest fused score
        assert result[0] == "b"

    def test_both_lists_contribute(self):
        """Items in both lists appear before items in only one."""
        result = rrf_fuse([["x", "b", "c"], ["b", "c", "d"]], k=60, top_k=4)
        # b and c appear in both lists → should lead the merged ranking
        assert "b" in result[:2]
        assert "c" in result[:2]

    def test_first_ranked_in_single_list_present(self):
        """Even items only in one list appear in the result."""
        result = rrf_fuse([["a", "b"], ["c", "d"]], k=60, top_k=4)
        all_ids = {"a", "b", "c", "d"}
        assert set(result).issubset(all_ids)
        assert len(result) == 4


class TestSingleList:
    def test_single_list_order_preserved(self):
        """With one ranking list, RRF returns items in the same order."""
        ranking = ["first", "second", "third", "fourth", "fifth"]
        result = rrf_fuse([ranking], k=60, top_k=5)
        assert result == ranking

    def test_single_list_top_k_slice(self):
        ranking = ["a", "b", "c", "d", "e"]
        result = rrf_fuse([ranking], k=60, top_k=3)
        assert result == ["a", "b", "c"]

    def test_single_list_returns_list(self):
        result = rrf_fuse([["x"]], k=60, top_k=5)
        assert isinstance(result, list)


class TestEmptyLists:
    def test_empty_rankings_returns_empty(self):
        result = rrf_fuse([], k=60, top_k=10)
        assert result == []

    def test_empty_inner_lists(self):
        result = rrf_fuse([[], []], k=60, top_k=10)
        assert result == []

    def test_one_empty_one_real(self):
        result = rrf_fuse([[], ["a", "b", "c"]], k=60, top_k=3)
        assert result == ["a", "b", "c"]

    def test_top_k_zero_returns_empty(self):
        result = rrf_fuse([["a", "b"]], k=60, top_k=0)
        assert result == []


class TestTopKRespected:
    def test_top_k_limits_result_length(self):
        result = rrf_fuse([["a", "b", "c", "d", "e"]], k=60, top_k=3)
        assert len(result) <= 3

    def test_top_k_1(self):
        result = rrf_fuse([["a", "b", "c"]], k=60, top_k=1)
        assert len(result) == 1
        assert result[0] == "a"

    def test_top_k_larger_than_items(self):
        result = rrf_fuse([["a", "b"]], k=60, top_k=100)
        assert len(result) == 2

    def test_result_is_always_list(self):
        result = rrf_fuse([["a"], ["b"]], k=60, top_k=10)
        assert isinstance(result, list)


class TestDuplicateHandling:
    def test_duplicates_within_single_list_not_compounded(self):
        """Duplicates in a single list should only count the first occurrence."""
        result_with_dup = rrf_fuse([["a", "a", "b"]], k=60, top_k=3)
        result_clean = rrf_fuse([["a", "b"]], k=60, top_k=3)
        # 'a' should not appear twice in the result
        assert result_with_dup.count("a") == 1
        # Scores should be the same — only first occurrence counts
        assert result_with_dup[0] == result_clean[0]

    def test_duplicates_across_lists_fused(self):
        """Same id in both lists gets a higher fused score."""
        # 'shared' appears at rank 1 in both lists
        result = rrf_fuse([["shared", "only_a"], ["shared", "only_b"]], k=60, top_k=3)
        assert result[0] == "shared"


class TestScoringMath:
    def test_higher_rank_gives_higher_score(self):
        """Item at rank 1 should beat item at rank 3."""
        result = rrf_fuse([["rank1", "rank2", "rank3"]], k=60, top_k=3)
        assert result[0] == "rank1"
        assert result[1] == "rank2"
        assert result[2] == "rank3"

    def test_k_parameter_respected(self):
        """Different k values produce valid results (not testing exact scores)."""
        r1 = rrf_fuse([["a", "b", "c"]], k=1, top_k=3)
        r2 = rrf_fuse([["a", "b", "c"]], k=1000, top_k=3)
        # Both should still return valid orderings
        assert set(r1) == {"a", "b", "c"}
        assert set(r2) == {"a", "b", "c"}
