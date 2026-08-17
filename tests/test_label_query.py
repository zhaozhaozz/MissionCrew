"""标签组合表达式:解析、求值与语法错误。"""
import pytest

from missioncrew.core.label_query import matches, parse


def test_operators_parens_and_case_insensitive_match():
    expr = "(Bug | crash) & !wontfix"
    assert matches(expr, ["bug"])
    assert matches(expr, ["CRASH", "ui"])
    assert not matches(expr, ["bug", "WontFix"])
    assert not matches(expr, ["feature"])


def test_double_operators_and_spaced_labels():
    assert matches("a && b", ["A", "B"])
    assert not matches("a || b", ["c"])
    # 标签允许包含空格,前后空白剔除
    assert matches("high priority & bug", ["High Priority", "bug"])


def test_not_binds_tighter_than_and_or():
    expr = "!a & b | c"        # 解析为 ((!a) & b) | c
    assert matches(expr, ["b"])
    assert matches(expr, ["a", "c"])
    assert not matches(expr, ["a", "b"])


@pytest.mark.parametrize("expr, message", [
    ("", "表达式不能为空"),
    ("a &", "表达式意外结束"),
    ("(a | b", "缺少右括号"),
    ("a b) ", "多余的内容"),
    ("| a", "缺少标签"),
])
def test_syntax_errors(expr, message):
    with pytest.raises(ValueError, match=message):
        parse(expr)
