"""Restricted assertion evaluator for task/skill criteria.

Assertions are machine checks, not a Python sandbox escape hatch. The expression
language only allows reads under ``workdir`` — no host ``Path`` construction, no
writes, no imports, and no builtins.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any


class UnsafeAssertionError(ValueError):
    """Raised when an assertion expression uses a disallowed construct."""


_ALLOWED_PATH_ATTRS = frozenset(
    {
        "exists",
        "is_file",
        "is_dir",
        "is_symlink",
        "read_text",
        "read_bytes",
        "name",
        "suffix",
        "stem",
        "as_posix",
        "parts",
    }
)


def evaluate_assertion(expr: str, *, workdir: Path) -> bool:
    """Evaluate a restricted assertion expression against ``workdir``."""

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise UnsafeAssertionError(f"invalid assertion expression: {exc}") from exc
    return bool(_SafeEval(workdir.resolve()).eval(tree.body))


class _SafeEval(ast.NodeVisitor):
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir

    def eval(self, node: ast.AST) -> Any:
        return self.visit(node)

    def visit(self, node: ast.AST) -> Any:  # type: ignore[override]
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            raise UnsafeAssertionError(f"disallowed expression node: {type(node).__name__}")
        return method(node)

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, (bool, int, float, str, bytes, type(None))):
            return node.value
        raise UnsafeAssertionError(f"disallowed constant: {type(node.value).__name__}")

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id == "workdir":
            return self.workdir
        if node.id in {"True", "False", "None"}:
            return {"True": True, "False": False, "None": None}[node.id]
        raise UnsafeAssertionError(f"unknown name: {node.id!r}")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise UnsafeAssertionError("disallowed unary operator")

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            value: Any = True
            for child in node.values:
                value = self.visit(child)
                if not value:
                    return value
            return value
        if isinstance(node.op, ast.Or):
            value = False
            for child in node.values:
                value = self.visit(child)
                if value:
                    return value
            return value
        raise UnsafeAssertionError("disallowed boolean operator")

    def visit_Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = self.visit(comparator)
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            elif isinstance(op, ast.In):
                ok = left in right
            elif isinstance(op, ast.NotIn):
                ok = left not in right
            else:
                raise UnsafeAssertionError("disallowed comparison")
            if not ok:
                return False
            left = right
        return True

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Div) and isinstance(left, Path):
            return self._confine(left / str(right))
        if (
            isinstance(node.op, ast.Add)
            and isinstance(left, str)
            and isinstance(right, str)
        ):
            return left + right
        if (
            isinstance(node.op, ast.Add)
            and isinstance(left, (int, float))
            and isinstance(right, (int, float))
        ):
            return left + right
        raise UnsafeAssertionError("disallowed binary operator")

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        value = self.visit(node.value)
        if isinstance(value, Path):
            if node.attr not in _ALLOWED_PATH_ATTRS:
                raise UnsafeAssertionError(f"disallowed path attribute: {node.attr!r}")
            attr = getattr(value, node.attr)
            # Bound methods are always truthy; require an explicit Call (e.g. .exists()).
            if callable(attr):
                raise UnsafeAssertionError(
                    f"path attribute {node.attr!r} must be called "
                    f"(e.g. .{node.attr}()), not used bare"
                )
            return attr
        raise UnsafeAssertionError("attribute access is only allowed on workdir paths")

    def visit_Call(self, node: ast.Call) -> Any:
        if node.keywords:
            raise UnsafeAssertionError("keyword arguments are not allowed in assertions")
        func = node.func
        if isinstance(func, ast.Name) and func.id in {"file_exists", "file_contains"}:
            args = [self.visit(arg) for arg in node.args]
            if func.id == "file_exists":
                if len(args) != 1 or not isinstance(args[0], str):
                    raise UnsafeAssertionError("file_exists expects one string path")
                return self._file_exists(args[0])
            if len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], str):
                raise UnsafeAssertionError("file_contains expects a path and a pattern")
            return self._file_contains(args[0], args[1])
        if isinstance(func, ast.Attribute):
            obj = self.visit(func.value)
            if not isinstance(obj, Path) or func.attr not in _ALLOWED_PATH_ATTRS:
                raise UnsafeAssertionError(f"disallowed call: {func.attr!r}")
            method = getattr(obj, func.attr)
            if not callable(method):
                raise UnsafeAssertionError(f"path attribute {func.attr!r} is not callable")
            if func.attr.startswith("read"):
                # Bound reads to files that resolve under workdir.
                self._confine(obj)
            args = [self.visit(arg) for arg in node.args]
            return method(*args)
        raise UnsafeAssertionError("only allowlisted path methods may be called")

    def visit_JoinedStr(self, node: ast.JoinedStr) -> str:
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append(str(self.visit(value.value)))
            else:
                raise UnsafeAssertionError("disallowed f-string component")
        return "".join(parts)

    def visit_FormattedValue(self, node: ast.FormattedValue) -> Any:
        return self.visit(node.value)

    def _file_exists(self, path: str) -> bool:
        return self._confine(self.workdir / path).is_file()

    def _file_contains(self, path: str, pattern: str) -> bool:
        if len(pattern) > 256:
            return False
        try:
            compiled = re.compile(pattern)
        except re.error:
            return False
        confined = self._confine(self.workdir / path)
        if not confined.is_file():
            return False
        text = confined.read_bytes()[: 1024 * 1024].decode("utf-8", errors="replace")
        return compiled.search(text) is not None

    def _confine(self, path: Path) -> Path:
        resolved = path if path.is_absolute() else (self.workdir / path)
        resolved = resolved.resolve()
        try:
            resolved.relative_to(self.workdir)
        except ValueError as exc:
            raise UnsafeAssertionError(f"path escapes workdir: {path}") from exc
        return resolved
