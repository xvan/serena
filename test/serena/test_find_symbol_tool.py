from unittest.mock import MagicMock

import pytest

from serena.tools.symbol_tools import FindSymbolTool


@pytest.mark.parametrize("relative_path", ["", "   "])
def test_find_symbol_rejects_unscoped_search(relative_path: str) -> None:
    tool = FindSymbolTool(MagicMock())

    with pytest.raises(ValueError, match="relative_path"):
        tool.apply(name_path_pattern="Foo", relative_path=relative_path)
