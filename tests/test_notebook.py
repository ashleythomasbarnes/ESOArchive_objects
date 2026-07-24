from __future__ import annotations

import ast
import json
from pathlib import Path


def test_interpretation_notebook_is_valid_and_code_parses() -> None:
    path = Path(__file__).parents[1] / "notebooks" / "interpret_outputs.ipynb"
    notebook = json.loads(path.read_text(encoding="utf-8"))

    assert notebook["nbformat"] == 4
    assert notebook["cells"]
    assert notebook["cells"][0]["cell_type"] == "markdown"

    for index, cell in enumerate(notebook["cells"], start=1):
        if cell["cell_type"] == "code":
            source = "".join(cell["source"])
            ast.parse(source, filename=f"{path.name}:cell-{index}")
