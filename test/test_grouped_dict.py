from __future__ import annotations

import copy

import pytest

from urllib3._collections import GroupedDict, ReverseKeysView


class TestGroupedDictDictParity:
    """The class must remain a fully-functional dict subclass."""

    def test_init_empty(self) -> None:
        d: GroupedDict[str, int] = GroupedDict()
        assert len(d) == 0
        assert list(d) == []

    def test_init_from_mapping(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1, "b": 2})
        assert d["a"] == 1
        assert d["b"] == 2
        assert len(d) == 2

    def test_init_from_iterable_of_pairs(self) -> None:
        d: GroupedDict[str, int] = GroupedDict([("a", 1), ("b", 2)])
        assert d["a"] == 1
        assert d["b"] == 2

    def test_init_from_kwargs(self) -> None:
        d: GroupedDict[str, int] = GroupedDict(a=1, b=2)
        assert d["a"] == 1
        assert d["b"] == 2

    def test_setitem_and_getitem(self) -> None:
        d: GroupedDict[str, int] = GroupedDict()
        d["x"] = 10
        assert d["x"] == 10
        assert "x" in d

    def test_delitem(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        del d["a"]
        assert "a" not in d
        with pytest.raises(KeyError):
            del d["a"]

    def test_pop_existing(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1, "b": 2})
        assert d.pop("a") == 1
        assert "a" not in d

    def test_pop_missing_raises(self) -> None:
        d: GroupedDict[str, int] = GroupedDict()
        with pytest.raises(KeyError):
            d.pop("absent")

    def test_pop_missing_with_default(self) -> None:
        d: GroupedDict[str, int] = GroupedDict()
        assert d.pop("absent", -1) == -1

    def test_pop_too_many_args(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        with pytest.raises(TypeError):
            d.pop("a", 1, 2)

    def test_popitem(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        k, v = d.popitem()
        assert k == "a" and v == 1
        assert len(d) == 0

    def test_popitem_empty_raises(self) -> None:
        d: GroupedDict[str, int] = GroupedDict()
        with pytest.raises(KeyError):
            d.popitem()

    def test_clear(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1, "b": 1})
        d.clear()
        assert len(d) == 0
        assert len(d.keys_for(1)) == 0

    def test_setdefault_existing(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        assert d.setdefault("a", 99) == 1
        assert d["a"] == 1

    def test_setdefault_missing(self) -> None:
        d: GroupedDict[str, int] = GroupedDict()
        assert d.setdefault("a", 42) == 42
        assert d["a"] == 42
        # reverse index must reflect the inserted default
        assert "a" in d.keys_for(42)

    def test_update_with_mapping(self) -> None:
        d: GroupedDict[str, int] = GroupedDict()
        d.update({"a": 1, "b": 2})
        assert d == {"a": 1, "b": 2}
        assert "a" in d.keys_for(1)
        assert "b" in d.keys_for(2)

    def test_update_with_iterable_of_pairs(self) -> None:
        d: GroupedDict[str, int] = GroupedDict()
        d.update([("a", 1), ("b", 2)])
        assert d["a"] == 1
        assert "b" in d.keys_for(2)

    def test_update_with_kwargs(self) -> None:
        d: GroupedDict[str, int] = GroupedDict()
        d.update(a=1, b=2)
        assert d["a"] == 1
        assert "b" in d.keys_for(2)

    def test_update_rejects_too_many_positional(self) -> None:
        d: GroupedDict[str, int] = GroupedDict()
        with pytest.raises(TypeError):
            d.update({"a": 1}, {"b": 2})

    def test_fromkeys(self) -> None:
        d = GroupedDict.fromkeys(["a", "b", "c"], 0)
        assert d["a"] == 0
        assert d["b"] == 0
        # All three collapse into the same reverse bucket
        assert set(d.keys_for(0)) == {"a", "b", "c"}

    def test_iter_and_in(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1, "b": 2})
        assert set(iter(d)) == {"a", "b"}
        assert "a" in d
        assert "z" not in d


class TestKeysFor:
    def test_absent_value_returns_empty_view(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        view = d.keys_for(999)
        assert isinstance(view, ReverseKeysView)
        assert len(view) == 0
        assert list(view) == []
        assert "a" not in view

    def test_present_value_returns_keys(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1, "b": 1, "c": 2})
        view = d.keys_for(1)
        assert set(view) == {"a", "b"}
        assert len(view) == 2

    def test_view_is_live(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        view = d.keys_for(1)
        assert set(view) == {"a"}
        d["b"] = 1
        # Live: subsequent reads see the new key.
        assert set(view) == {"a", "b"}
        del d["a"]
        assert set(view) == {"b"}

    def test_view_is_readonly(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        view = d.keys_for(1)
        # KeysView is not a MutableSet — no add/discard/pop methods.
        assert not hasattr(view, "add")
        assert not hasattr(view, "discard")
        assert not hasattr(view, "remove")

    def test_view_set_algebra(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1, "b": 1, "c": 2})
        view = d.keys_for(1)
        assert view & {"a", "z"} == {"a"}
        assert view | {"z"} == {"a", "b", "z"}
        assert view - {"a"} == {"b"}
        assert view ^ {"b", "z"} == {"a", "z"}
        assert view.isdisjoint({"x", "y"}) is True
        assert view.isdisjoint({"a"}) is False
        assert view <= {"a", "b", "c"}
        assert view >= {"a"}
        assert set(view) == {"a", "b"}

    def test_view_membership(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1, "b": 1})
        view = d.keys_for(1)
        assert "a" in view
        assert "z" not in view

    def test_view_repr(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        view = d.keys_for(1)
        # Just make sure repr does not blow up and includes the key.
        r = repr(view)
        assert "a" in r
        empty = d.keys_for(999)
        assert "ReverseKeysView" in repr(empty)


class TestBucketHygiene:
    def test_empty_bucket_is_pruned_on_delete(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        del d["a"]
        # The internal bucket should be gone, not left as an empty set.
        assert 1 not in d._index

    def test_empty_bucket_is_pruned_on_pop(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        d.pop("a")
        assert 1 not in d._index

    def test_empty_bucket_is_pruned_on_popitem(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        d.popitem()
        assert 1 not in d._index

    def test_non_empty_bucket_is_retained(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1, "b": 1})
        del d["a"]
        assert 1 in d._index
        assert d._index[1] == {"b"}

    def test_value_change_moves_key_between_buckets(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        d["a"] = 2
        assert 1 not in d._index
        assert "a" in d.keys_for(2)

    def test_value_unchanged_does_not_touch_buckets(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        before = d._index[1]
        d["a"] = 1  # same value — fast path
        assert d._index[1] is before  # bucket object identity preserved


class TestKeyFn:
    def test_default_uses_identity_via_equality(self) -> None:
        # Default key_fn uses the value itself. For ints, equal values
        # collapse into the same bucket.
        d: GroupedDict[str, int] = GroupedDict({"a": 1, "b": 1})
        assert set(d.keys_for(1)) == {"a", "b"}

    def test_id_key_fn_keeps_equal_distinct(self) -> None:
        # Two distinct list instances are __eq__ but not the same object.
        # With key_fn=id, they should produce distinct reverse buckets.
        a: list[int] = [1, 2, 3]
        b: list[int] = [1, 2, 3]
        assert a == b and a is not b

        d: GroupedDict[str, list[int]] = GroupedDict(key_fn=id)
        d["k_a"] = a
        d["k_b"] = b

        # Lookup goes by id(value)
        assert set(d.keys_for(a)) == {"k_a"}
        assert set(d.keys_for(b)) == {"k_b"}

    def test_id_key_fn_accepts_unhashable_values(self) -> None:
        # Lists are unhashable — default key_fn would explode, but key_fn=id
        # bypasses that since id() is always an int.
        d: GroupedDict[str, list[int]] = GroupedDict(key_fn=id)
        v = [1, 2, 3]
        d["x"] = v
        assert "x" in d.keys_for(v)
        del d["x"]
        assert len(d.keys_for(v)) == 0


class TestCopy:
    def test_copy_returns_independent_instance(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1, "b": 2})
        c = d.copy()
        assert isinstance(c, GroupedDict)
        assert c == d
        assert c is not d
        c["a"] = 99
        # Original is untouched
        assert d["a"] == 1

    def test_copy_preserves_key_fn(self) -> None:
        a: list[int] = [1]
        d: GroupedDict[str, list[int]] = GroupedDict(key_fn=id)
        d["k"] = a
        c = d.copy()
        # The copy must also use id-based indexing
        assert "k" in c.keys_for(a)
        other: list[int] = [1]
        assert len(c.keys_for(other)) == 0

    def test_dunder_copy(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1})
        c = copy.copy(d)
        assert isinstance(c, GroupedDict)
        assert c == d
        assert c is not d


class TestIterationSafety:
    def test_mutation_during_iteration_raises(self) -> None:
        # Standard dict / set contract: mutating during iteration raises
        # RuntimeError. The view inherits this from the underlying set.
        d: GroupedDict[str, int] = GroupedDict({"a": 1, "b": 1, "c": 1})
        view = d.keys_for(1)
        with pytest.raises(RuntimeError):
            for k in view:
                d[f"new_{k}"] = 1

    def test_snapshot_protects_iteration(self) -> None:
        d: GroupedDict[str, int] = GroupedDict({"a": 1, "b": 1})
        snapshot = list(d.keys_for(1))
        # Mutate freely, snapshot is detached
        for k in snapshot:
            del d[k]
        assert len(d) == 0
