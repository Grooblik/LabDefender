from dataclasses import dataclass
from typing import Generic, TypeVar

from labdefender.services.errors import ObjectUnavailable

PAGE_SIZE = 8
T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    offset: int
    has_next: bool


def validate_offset(offset: int) -> None:
    if not 0 <= offset <= 2**31 - 1:
        raise ObjectUnavailable()
