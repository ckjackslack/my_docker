import pytest

from my_docker.interpolation import (
    InterpolationError,
    VarRef,
    find_refs,
    interpolate,
    interpolate_tree,
    walk_strings,
)


@pytest.mark.parametrize(
    ("text", "env", "expected"),
    [
        ("plain", {}, "plain"),
        ("${A}", {"A": "x"}, "x"),
        ("$A/b", {"A": "x"}, "x/b"),
        ("${A}", {}, ""),
        ("${A:-d}", {}, "d"),
        ("${A:-d}", {"A": ""}, "d"),
        ("${A-d}", {"A": ""}, ""),
        ("${A-d}", {}, "d"),
        ("${A:+alt}", {"A": "1"}, "alt"),
        ("${A:+alt}", {"A": ""}, ""),
        ("${A+alt}", {"A": ""}, "alt"),
        ("${A:?boom}", {"A": "v"}, "v"),
        ("$$A and $${B}", {"A": "x"}, "$A and ${B}"),
        ("${A:-${B:-deep}}", {}, "deep"),
        ("${A:-${B}}", {"B": "b"}, "b"),
        ("127.0.0.1:${P:-80}:80", {"P": "8081"}, "127.0.0.1:8081:80"),
        ("cost $5", {}, "cost $5"),
    ],
)
def test_interpolate(text, env, expected):
    assert interpolate(text, env) == expected


def test_required_variable_raises_with_message():
    with pytest.raises(InterpolationError, match="POSTGRES_DB is missing a value: set it"):
        interpolate("${POSTGRES_DB:?set it}", {})
    with pytest.raises(InterpolationError):
        interpolate("${A?}", {})
    assert interpolate("${A?}", {"A": ""}) == ""


def test_default_with_required_inner_is_lazy():
    # the inner required reference is only evaluated when the default is needed
    assert interpolate("${A:-${B:?no}}", {"A": "a"}) == "a"
    with pytest.raises(InterpolationError):
        interpolate("${A:-${B:?no}}", {})


@pytest.mark.parametrize("bad", ["${", "${A", "${1A}", "${A%x}", "${}"])
def test_malformed(bad):
    with pytest.raises(InterpolationError):
        interpolate(bad, {})


def test_find_refs_records_operators_and_nested():
    refs = find_refs("${A:?msg} $B ${C:-${D}} $$E")
    assert refs == [VarRef("A", ":?", "msg"), VarRef("B"), VarRef("C", ":-", "${D}"), VarRef("D")]
    assert refs[0].required and not refs[0].has_fallback
    assert refs[2].has_fallback


def test_tree_helpers():
    tree = {"a": ["${X}", {"b": "${Y:-y}"}], "n": 3, "k${Z}": None}
    assert list(walk_strings(tree)) == ["${X}", "${Y:-y}"]
    assert interpolate_tree(tree, {"X": "x"}) == {"a": ["x", {"b": "y"}], "n": 3, "k${Z}": None}
