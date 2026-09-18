"""Repo-level deploy opt-out via a `.jarvis-no-deploy` marker file."""
import json

from src.runner import DEPLOY_OPT_OUT_MARKER, _deploy_opted_out, _looks_like_web_project


def _astro_workspace(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"astro": "^7.0.0"}, "scripts": {"build": "astro build"}}),
        encoding="utf-8",
    )
    return tmp_path


def test_marker_present_opts_out(tmp_path):
    ws = _astro_workspace(tmp_path)
    (ws / DEPLOY_OPT_OUT_MARKER).write_text("", encoding="utf-8")
    assert _deploy_opted_out(ws) is True
    # The web-project heuristic is unchanged: tests and smoke checks still run.
    assert _looks_like_web_project(ws) is True


def test_marker_absent_does_not_opt_out(tmp_path):
    ws = _astro_workspace(tmp_path)
    assert _deploy_opted_out(ws) is False
    assert _looks_like_web_project(ws) is True


def test_marker_must_be_at_repo_root(tmp_path):
    ws = _astro_workspace(tmp_path)
    (ws / "nested").mkdir()
    (ws / "nested" / DEPLOY_OPT_OUT_MARKER).write_text("", encoding="utf-8")
    assert _deploy_opted_out(ws) is False
