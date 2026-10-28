import tomllib

import pytest


def leaf_values(node: object) -> list[str]:
    if isinstance(node, dict):
        return [value for child in node.values() for value in leaf_values(child)]
    if isinstance(node, list):
        return [value for child in node for value in leaf_values(child)]
    return [str(node)]


@pytest.fixture
def echoed():
    """AC-20: the values of a TOML text that appear in an error message. Words of the rule itself do not count."""

    def find(message: str, text: str, rule_words: tuple[str, ...] = ()) -> list[str]:
        values = leaf_values(tomllib.loads(text))
        return [value for value in values if value and value not in rule_words and value in message]

    return find
