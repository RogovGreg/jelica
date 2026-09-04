from __future__ import annotations

from dataclasses import dataclass

from .enums import CodeNamespace


@dataclass(frozen=True, slots=True)
class CodeRange:
    namespace: CodeNamespace
    prefix: str
    start: int
    end: int

    def contains(self, code: int) -> bool:
        return self.start <= code <= self.end


EVENT_CODE_RANGES: tuple[CodeRange, ...] = (
    # Reserved for future cross-component/system-level events.
    CodeRange(namespace=CodeNamespace.SYSTEM, prefix="SYSTEM_", start=1000, end=1999),
    CodeRange(namespace=CodeNamespace.CORE, prefix="CORE_", start=2000, end=2999),
    CodeRange(namespace=CodeNamespace.CLI, prefix="CLI_", start=3000, end=3999),
    CodeRange(namespace=CodeNamespace.SERVER, prefix="SERVER_", start=4000, end=4999),
    CodeRange(namespace=CodeNamespace.WEB, prefix="WEB_", start=5000, end=5999),
    CodeRange(namespace=CodeNamespace.DESKTOP, prefix="DESKTOP_", start=6000, end=6999),
    CodeRange(namespace=CodeNamespace.RESERVED, prefix="RESERVED_", start=7000, end=9999),
)


def get_code_range(namespace: CodeNamespace) -> CodeRange:
    for code_range in EVENT_CODE_RANGES:
        if code_range.namespace is namespace:
            return code_range
    raise ValueError(f"Unknown code namespace: {namespace}")


def validate_code_namespace(*, namespace: CodeNamespace, code: int, name: str) -> None:
    code_range = get_code_range(namespace)
    if not code_range.contains(code):
        raise ValueError(
            f"Code {code} is outside range {code_range.start}-{code_range.end} "
            f"for namespace {namespace.value}."
        )
    if not name.startswith(code_range.prefix):
        raise ValueError(
            f"Name '{name}' must start with '{code_range.prefix}' for namespace {namespace.value}."
        )
