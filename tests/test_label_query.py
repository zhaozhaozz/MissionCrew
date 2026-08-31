"""标签组合表达式:解析、求值与语法错误。"""
import pytest

from missioncrew.core.label_query import (matches, normalize_label, parse,
                                          split_label)


def test_normalize_label_advanced_and_plain():
    assert normalize_label("owner:张三") == "owner: 张三"
    assert normalize_label("  owner :  张三  ") == "owner: 张三"
    assert normalize_label(" bug ") == "bug"
    # 冒号任一侧为空时按纯标签处理
    assert normalize_label("bug:") == "bug:"
    assert normalize_label(":weird") == ":weird"
    assert split_label("owner: John David") == ("owner", "John David")
    assert split_label("plain") == ("", "plain")


def test_advanced_label_whole_string_match():
    labels = ["owner:张三", "bug", "status: 处理中"]
    assert matches("owner: 张三", labels)          # 写法差异经归一化后等价
    assert matches("status:处理中 & bug", labels)
    assert not matches("owner: 李四", labels)


def test_property_star_existence_and_negation():
    with_owner = ["owner: 张三", "bug"]
    without_owner = ["bug"]
    assert matches("owner: *", with_owner)
    assert not matches("owner: *", without_owner)
    # !owner: 张三 命中「没有这条标签」的对象,含根本没有 owner 的
    assert matches("!owner: 张三", without_owner)
    assert not matches("!owner: 张三", with_owner)
    # owner: * & !owner: 张三 = 有 owner 且不是张三
    expr = "owner: * & !owner: 张三"
    assert not matches(expr, with_owner)
    assert not matches(expr, without_owner)
    assert matches(expr, ["owner: 李四"])
    # 未设置列的表达式
    assert matches("!owner: *", without_owner)


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
