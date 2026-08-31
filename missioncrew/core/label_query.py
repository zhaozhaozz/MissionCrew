"""标签与标签组合表达式。

标签有两种形态:纯标签 `text` 与高级标签 `property: value`(首个冒号切分,
写入侧统一归一化为「冒号后单空格」;冒号任一侧为空时按纯标签处理)。

表达式支持与(&)、或(|)、非(!)与括号;`&&`/`||` 与单写等价。项由整串标签
构成:普通项与标签整串匹配(含高级标签);`property: *` 是存在性匹配,命中
「带任意该属性标签」的对象。匹配不区分大小写,否定只经 `!` 运算符表达。
AST 用元组表示:("label", 文本) | ("has", 属性) | ("not", 子式)
| ("and"|"or", 左, 右)。
"""
from __future__ import annotations

_OPERATOR_CHARS = "&|!()"


def split_label(text: str) -> tuple[str, str]:
    """把标签拆成 (属性, 值);纯标签返回 ("", 文本)。"""
    raw = str(text or "").strip()
    if ":" in raw:
        prop, _, value = raw.partition(":")
        prop, value = prop.strip(), value.strip()
        if prop and value:
            return prop, value
    return "", raw


def normalize_label(text: str) -> str:
    """标签写入前的归一化:首个冒号切分、两侧 trim、冒号后单空格。"""
    prop, value = split_label(text)
    return f"{prop}: {value}" if prop else value


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
            prop, value = split_label(token[1])
            if prop and value == "*":
                return ("has", prop.lower())
            return ("label", normalize_label(token[1]).lower())
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
    lowered = {normalize_label(label).lower() for label in labels}
    props = {split_label(label)[0].lower()
             for label in labels if split_label(label)[0]}

    def evaluate(node) -> bool:
        kind = node[0]
        if kind == "label":
            return node[1] in lowered
        if kind == "has":
            return node[1] in props
        if kind == "not":
            return not evaluate(node[1])
        if kind == "and":
            return evaluate(node[1]) and evaluate(node[2])
        return evaluate(node[1]) or evaluate(node[2])

    return evaluate(ast)
