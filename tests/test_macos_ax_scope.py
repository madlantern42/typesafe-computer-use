from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from typesafe_computer_use import macos
from typesafe_computer_use.ax_walk import AxAttrs


@dataclass(frozen=True)
class Element:
    key: str


@pytest.fixture
def ax_tree(monkeypatch):
    """Every native call is replaced; repeated lookups wrap the same AX identity anew."""
    tree = SimpleNamespace(
        focused="focused",
        children={
            "app": ["background", "menu", "focused", "shared"],
            "focused": ["origin"],
            "background": ["unrelated"],
            "menu": ["file"],
            "shared": ["shared_button"],
        },
        roles={
            "app": "AXApplication",
            "focused": "AXWindow",
            "background": "AXWindow",
            "menu": "AXMenuBar",
            "shared": "AXGroup",
            "origin": "AXTextField",
            "unrelated": "AXButton",
            "file": "AXMenuBarItem",
            "shared_button": "AXButton",
        },
        labels={"origin": "Origin", "unrelated": "Unrelated", "file": "File", "shared_button": "Shared"},
        visited=[],
    )

    def attr(element, name):
        if name == "AXFocusedWindow":
            return Element(tree.focused) if tree.focused is not None else None
        if name == "AXRole":
            return tree.roles.get(element.key)
        pytest.fail(f"unexpected AX query: {name}")

    def children(element):
        tree.visited.append(element.key)
        return [Element(key) for key in tree.children.get(element.key, [])]

    def attrs(element):
        key = element.key
        frame = None if key == "app" else (10.0, 30.0, 800.0, 600.0)
        return AxAttrs(tree.roles[key], tree.labels.get(key, ""), frame)

    monkeypatch.setattr(
        macos,
        "AS",
        SimpleNamespace(
            AXUIElementCreateApplication=lambda pid: Element("app"),
            AXUIElementSetMessagingTimeout=lambda app, timeout: None,
            kAXFocusedWindowAttribute="AXFocusedWindow",
            kAXRoleAttribute="AXRole",
        ),
    )
    monkeypatch.setattr(macos, "_ax_attr", attr)
    monkeypatch.setattr(macos, "_ax_children", children)
    monkeypatch.setattr(macos, "_ax_attrs", attrs)
    monkeypatch.setattr(macos, "_ax_actions", lambda element: [])
    return tree


def test_focused_window_excludes_other_windows_and_preserves_app_level_roots(ax_tree):
    found, hidden, capped = macos.actionable_elements(123, 1728.0, 1117.0)

    assert [node.label for node in found] == ["Origin", "File", "Shared"]
    assert "background" not in ax_tree.visited and "unrelated" not in ax_tree.visited
    assert hidden == [] and not capped


def test_focused_window_can_be_missing_from_application_children(ax_tree):
    ax_tree.children["app"].remove("focused")

    found, _, _ = macos.actionable_elements(123, 1728.0, 1117.0)

    assert [node.label for node in found] == ["Origin", "File", "Shared"]


def test_equal_focused_window_wrappers_are_traversed_once(ax_tree):
    ax_tree.children["app"].append("focused")

    found, _, _ = macos.actionable_elements(123, 1728.0, 1117.0)

    assert [node.label for node in found].count("Origin") == 1
    assert ax_tree.visited.count("focused") == 1


@pytest.mark.parametrize("unusable", ["missing", "invalid", "empty"])
def test_missing_or_unusable_focused_window_preserves_full_app_fallback(ax_tree, unusable):
    if unusable == "missing":
        ax_tree.focused = None
    elif unusable == "invalid":
        ax_tree.roles["focused"] = "AXGroup"
    else:
        ax_tree.children["focused"] = []

    found, _, _ = macos.actionable_elements(123, 1728.0, 1117.0)

    assert "Unrelated" in [node.label for node in found]
    assert "background" in ax_tree.visited
