"""Task 标签组合表达式:与(&)、或(|)、非(!)与括号。

标签任务看板用它筛选 Task;标签匹配不区分大小写,`&&`/`||` 与单写等价。
AST 用元组表示:("label", 文本) | ("not", 子式) | ("and"|"or", 左, 右)。
"""
from __future__ import annotations

_OPERATOR_CHARS = "&|!()"


def _tokenize(expr: str) -> list:
    tokens: list = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        text = buffer.strip()
        if text:
            tokens.append(("label", text))
        buffer = ""

    index = 0
    while index < len(expr):
        char = expr[index]
        if char in _OPERATOR_CHARS:
            flush()
            if char in "&|" and index + 1 < len(expr) and expr[index + 1] == char:
                index += 1
            tokens.append(char)
        else:
            buffer += char
        index += 1
    flush()
    return tokens


def parse(expr: str):
    """解析表达式为 AST;语法错误抛 ValueError(带中文原因)。"""
    tokens = _tokenize(str(expr or ""))
    if not tokens:
        raise ValueError("表达式不能为空")
    position = 0

    def peek():
        return tokens[position] if position < len(tokens) else None

    def take():
        nonlocal position
        token = tokens[position]
        position += 1
        return token

    def parse_or():
        node = parse_and()
        while peek() == "|":
            take()
            node = ("or", node, parse_and())
        return node

    def parse_and():
        node = parse_not()
        while peek() == "&":
            take()
            node = ("and", node, parse_not())
        return node

    def parse_not():
        token = peek()
        if token == "!":
            take()
            return ("not", parse_not())
        if token == "(":
            take()
            node = parse_or()
            if peek() != ")":
                raise ValueError("缺少右括号")
            take()
            return node
        if isinstance(token, tuple) and token[0] == "label":
            take()
            return ("label", token[1].lower())
        raise ValueError(f"运算符 {token} 后缺少标签" if token else "表达式意外结束")

    node = parse_or()
    if position != len(tokens):
        extra = tokens[position]
        raise ValueError(
            f"多余的内容: {extra[1] if isinstance(extra, tuple) else extra}")
    return node


def matches(expr_or_ast, labels) -> bool:
    """判断标签集合是否命中表达式;expr 传字符串时即时解析。"""
    ast = parse(expr_or_ast) if isinstance(expr_or_ast, str) else expr_or_ast
    lowered = {str(label).lower() for label in labels}

    def evaluate(node) -> bool:
        kind = node[0]
        if kind == "label":
            return node[1] in lowered
        if kind == "not":
            return not evaluate(node[1])
        if kind == "and":
            return evaluate(node[1]) and evaluate(node[2])
        return evaluate(node[1]) or evaluate(node[2])

    return evaluate(ast)
