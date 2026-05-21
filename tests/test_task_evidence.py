"""Tests for build-class / no-changes evidence rules."""
from src.task_evidence import (
    allows_no_changes,
    is_build_class_task,
    no_changes_should_block,
)


def test_build_class_from_title():
    task = {"title": "Build a polished demo website", "body": "Next.js 14", "metadata": {}}
    assert is_build_class_task(task) is True


def test_allow_no_changes_metadata():
    task = {
        "title": "Build site",
        "body": "x",
        "metadata": {"allow_no_changes": True},
    }
    assert is_build_class_task(task) is False
    assert no_changes_should_block(task, backend_name="claude") == (False, "")


def test_no_changes_blocks_build():
    task = {"title": "Scaffold Next app", "body": "", "metadata": {}}
    block, reason = no_changes_should_block(task, backend_name="claude")
    assert block is True
    assert reason == "no_shipped_artefact"


def test_codex_always_blocks_without_changes():
    task = {"title": "Quick question", "body": "What is X?", "metadata": {}}
    block, reason = no_changes_should_block(task, backend_name="codex")
    assert block is True
    assert reason == "codex_advisory_no_files"
