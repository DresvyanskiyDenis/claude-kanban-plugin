from collections.abc import Callable
from typing import Any

import pytest

from kanban_claude import validate
from kanban_claude.errors import Refused

GOOD: list[tuple[Callable[[Any], object], str]] = [
    (validate.slug, "board-1"),
    (validate.task_id, "t_0123abcd"),
    (validate.model, "opus[1m]"),
    (lambda v: validate.skills([v]), "plugin:skill"),
    (validate.duration, "30m"),
]


@pytest.mark.parametrize(("check", "value"), GOOD)
def test_validators_accept_a_plain_value(check: Callable[[Any], object], value: str) -> None:
    check(value)


@pytest.mark.parametrize(("check", "value"), GOOD)
def test_validators_refuse_a_trailing_newline(check: Callable[[Any], object], value: str) -> None:
    with pytest.raises(Refused):
        check(value + "\n")
