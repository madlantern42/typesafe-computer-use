import pytest

from typesafe_computer_use.ax_walk import AxAttrs, walk_actionable


def walk_groups(tree, labels):
    """Distinct layout wrappers share a window's bounds; controls have their own bounds."""

    def attrs(name):
        if name == "app":
            return AxAttrs("AXApplication", "", None)
        if name in labels:
            return AxAttrs("AXButton", labels[name], (20.0, 100.0, 180.0, 30.0))
        return AxAttrs("AXGroup", "", (10.0, 30.0, 800.0, 600.0))

    return walk_actionable("app", lambda name: tree[name], attrs, lambda name: [], 1728.0, 1117.0, clock=lambda: 0.0)


def test_nested_unnamed_groups_with_equal_bounds_preserve_descendants():
    tree = {"app": ["outer"], "outer": ["inner"], "inner": ["origin"], "origin": []}

    found, hidden, capped = walk_groups(tree, {"origin": "Origin"})

    assert [(node.label, node.ref) for node in found] == [("Origin", "origin")]
    assert hidden == [] and not capped


def test_sibling_unnamed_groups_with_equal_bounds_preserve_distinct_descendants():
    tree = {
        "app": ["left", "right"],
        "left": ["origin"],
        "right": ["destination"],
        "origin": [],
        "destination": [],
    }

    found, hidden, capped = walk_groups(tree, {"origin": "Origin", "destination": "Destination"})

    assert [(node.label, node.ref) for node in found] == [("Origin", "origin"), ("Destination", "destination")]
    assert hidden == [] and not capped


@pytest.mark.parametrize("collection_role", ["AXTable", "AXOutline", "AXList"])
@pytest.mark.parametrize("limit", ["nodes", "time"])
def test_dense_collection_does_not_starve_nested_search_field(collection_role, limit):
    tree = {
        "app": ["collection", "form"],
        "collection": [f"row{i}" for i in range(100)],
        "form": ["wrapper"],
        "wrapper": ["search"],
    }
    visited = []

    def attrs(name):
        visited.append(name)
        if name == "app":
            return AxAttrs("AXApplication", "", None)
        if name == "collection":
            return AxAttrs(collection_role, "", (0, 100, 800, 600))
        if name == "search":
            return AxAttrs("AXTextField", "Search", (100, 50, 300, 30))
        if name.startswith("row"):
            return AxAttrs("AXButton", name, (100, 110, 100, 30))
        return AxAttrs("AXGroup", "", (0, 30, 800, 700))

    found, hidden, capped = walk_actionable(
        "app",
        lambda name: tree.get(name, []),
        attrs,
        lambda name: [],
        1000,
        800,
        node_cap=12 if limit == "nodes" else 4000,
        time_cap=12 if limit == "time" else 100,
        clock=lambda: float(len(visited)),
    )

    assert found[0].ref == "search"
    assert any(node.ref.startswith("row") for node in found)
    assert len(visited) == 12 and capped
    assert hidden == []


def test_deferred_collections_are_eventually_walked_and_cycles_remain_bounded():
    tree = {"app": ["table"], "table": ["list", "button"], "list": ["table"], "button": []}
    roles = {"app": "AXApplication", "table": "AXTable", "list": "AXList", "button": "AXButton"}
    visited = []

    def attrs(name):
        visited.append(name)
        return AxAttrs(roles[name], "Open" if name == "button" else "", (20, 40, 100, 30))

    found, hidden, capped = walk_actionable(
        "app",
        lambda name: tree[name],
        attrs,
        lambda name: [],
        1000,
        800,
        clock=lambda: 0.0,
    )

    assert [node.ref for node in found] == ["button"]
    assert sorted(visited) == sorted(tree)
    assert hidden == [] and not capped
