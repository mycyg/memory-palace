"""Emit dependency-free TypedDict shapes and client stubs from OpenAPI."""


def annotation(schema):
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in schema:
        return " | ".join(annotation(s) for s in schema["anyOf"])
    if "enum" in schema:
        return "Literal[" + ", ".join(repr(v) for v in schema["enum"]) + "]"
    kind = schema.get("type")
    if kind == "array":
        return "list[" + annotation(schema.get("items", {})) + "]"
    if kind == "object":
        return (
            "dict[str, "
            + annotation(
                schema.get("additionalProperties", {})
                if isinstance(schema.get("additionalProperties"), dict)
                else {}
            )
            + "]"
        )
    return {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "null": "None",
    }.get(kind, "Any")


def generate(schema, root):
    shapes = [
        "# Generated from contracts/openapi.json.",
        "from __future__ import annotations",
        "from typing import Any, Literal",
        "from typing_extensions import Required, TypedDict",
        "",
    ]
    for name, value in schema.get("components", {}).get("schemas", {}).items():
        if value.get("properties"):
            shapes.append(f"class {name}(TypedDict, total=False):")
            for key, prop in value["properties"].items():
                kind = annotation(prop)
                if key in value.get("required", []):
                    kind = f"Required[{kind}]"
                shapes.append(f"    {key}: {kind}")
        else:
            shapes.append(f"{name} = {annotation(value)}")
        shapes.append("")
    (root / "_types.py").write_text("\n".join(shapes).rstrip() + "\n")
    stub = [
        "# Generated from contracts/openapi.json.",
        "from typing import Any",
        "from pathlib import Path",
        "from pydantic import BaseModel",
        "from ._types import ("
        + ", ".join(name + " as " + name for name in schema["components"]["schemas"])
        + ")",
        "class Client:",
        "    def __init__(self, url: str = ..., token: str | None = ..., *, root: str | Path | None = ..., transport: Any = ..., timeout: float = ...) -> None: ...",
        "    def close(self) -> None: ...",
        "    def __enter__(self) -> Client: ...",
        "    def __exit__(self, *args: Any) -> None: ...",
        "    def call(self, operation: str, body: Any = ..., *, params: dict | None = ..., files: Any = ..., **path_params: Any) -> Any: ...",
        "    def upload(self, path: str | Path, metadata: BaseModel | dict) -> SourceResult: ...",
    ]
    for methods in schema["paths"].values():
        for operation in methods.values():
            if "operationId" not in operation:
                continue
            content = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            result = (
                operation.get("responses", {})
                .get("200", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            stub.append(
                f"    def {operation['operationId']}(self, body: {annotation(content)} | BaseModel | None = ..., *, params: dict | None = ..., files: Any = ..., **path_params: Any) -> {annotation(result)}: ..."
            )
    stub += [
        "class DeliveryInbox:",
        "    def __init__(self, path: str | Path) -> None: ...",
        "    def accept(self, delivery: dict, handler: Any) -> bool: ...",
    ]
    (root / "__init__.pyi").write_text("\n".join(stub) + "\n")
    (root / "py.typed").write_text("")
