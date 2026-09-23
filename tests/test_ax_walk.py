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
