"""Stack contract validation."""
import json
from pathlib import Path

from src.stack_policy import (
    format_stack_prompt_block,
    validate_workspace_against_contract,
)


def test_validate_astro_scaffold_ok(tmp_path: Path):
    ws = tmp_path
    (ws / "package.json").write_text(
        json.dumps({"dependencies": {"astro": "^5.0.0", "tailwindcss": "^4.0.0"}}),
        encoding="utf-8",
    )
    (ws / "astro.config.mjs").write_text("export default {}", encoding="utf-8")
    src = ws / "src" / "pages"
    src.mkdir(parents=True)
    (src / "index.astro").write_text("<html></html>", encoding="utf-8")
    (src / "about.astro").write_text("<html></html>", encoding="utf-8")

    contract = {
        "profile_id": "astro-static-demo",
        "required_deps": ["astro"],
        "forbidden_deps": ["next"],
        "required_path_groups": [
            ["package.json"],
            ["astro.config.mjs"],
            ["src/pages/index.astro"],
        ],
    }
    ok, failures = validate_workspace_against_contract(ws, contract)
    assert ok is True
    assert failures == []


def test_validate_astro_missing_astro_dep(tmp_path: Path):
    ws = tmp_path
    (ws / "package.json").write_text(json.dumps({"dependencies": {}}), encoding="utf-8")
    contract = {
        "profile_id": "astro-static-demo",
        "required_deps": ["astro"],
        "forbidden_deps": [],
        "required_path_groups": [["package.json"]],
    }
    ok, failures = validate_workspace_against_contract(ws, contract)
    assert ok is False
    assert any("astro" in f for f in failures)


def test_format_stack_prompt_block_contains_profile():
    block = format_stack_prompt_block({"profile_id": "astro-static-demo", "label": "Astro"})
    assert "stack_contract" in block
    assert "astro-static-demo" in block
