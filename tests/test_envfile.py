import itertools

import pytest

from my_docker.envfile import (
    EnvFileError,
    env_dict,
    format_value,
    generate_secret,
    parse_assignment,
    parse_env,
    render_env,
)


def test_parse_env_variants():
    text = "\n".join(
        [
            "# comment",
            "",
            "A=1",
            "export B = two ",
            "C='single # not comment'",
            'D="x\\"y\\n"',
            "E=val # trailing comment",
            "F=a#b",
            "G=",
        ]
    )
    assert env_dict(text) == {
        "A": "1",
        "B": "two",
        "C": "single # not comment",
        "D": 'x"y\n',
        "E": "val",
        "F": "a#b",
        "G": "",
    }
    lines = parse_env(text)
    assert [l.key for l in lines[:3]] == [None, None, "A"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("JUSTAKEY", "expected KEY=VALUE"),
        ("1BAD=x", "invalid variable name"),
        ("A='open", "unterminated"),
        ("A='x' junk", "unexpected text"),
    ],
)
def test_parse_env_errors(text, message):
    with pytest.raises(EnvFileError, match=message) as exc:
        parse_env("OK=1\n" + text)
    assert exc.value.lineno == 2


@pytest.mark.parametrize(
    "value", ["simple", "", "with space", "it's", "a$b", 'q"uote', "back\\slash", "#hash"]
)
def test_format_value_round_trips(value):
    assert env_dict(f"K={format_value(value)}")["K"] == value


def test_format_value_escapes_dollar_in_double_quotes():
    assert format_value("it's $HOME") == '"it\'s $$HOME"'


def test_render_env_replaces_placeholders_and_keeps_layout():
    example = "# header\nUSER=postgres\nPASSWORD=change-me\n\nTOKEN=CHANGE-ME\nPORT=80\n"
    counter = itertools.count(1)
    result = render_env(example, {"PORT": "8080", "EXTRA": "x y"}, lambda: f"s{next(counter)}")
    assert result.text == (
        "# header\nUSER=postgres\nPASSWORD=s1\n\nTOKEN=s2\nPORT=8080\n"
        "\n# --- overrides ---\nEXTRA='x y'\n"
    )
    assert result.generated == ("PASSWORD", "TOKEN")
    assert result.overridden == ("PORT", "EXTRA")


def test_override_beats_placeholder():
    result = render_env("PASSWORD=change-me\n", {"PASSWORD": "mine"})
    assert env_dict(result.text) == {"PASSWORD": "mine"}
    assert result.generated == ()


def test_generate_secret_is_random_and_env_safe():
    a, b = generate_secret(), generate_secret()
    assert a != b and len(a) >= 32
    assert format_value(a) == a


def test_parse_assignment():
    assert parse_assignment("A=b=c") == ("A", "b=c")
    assert parse_assignment("A=") == ("A", "")
    for bad in ("A", "=x", "a b=1"):
        with pytest.raises(ValueError):
            parse_assignment(bad)
