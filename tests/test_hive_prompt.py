"""Tests for hive prompt host detection and PR parsing."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hive_prompt  # noqa: E402


def test_pr_cli_for_github_urls_prefers_gh():
    with patch.object(hive_prompt.shutil, "which", side_effect=lambda cmd: f"/bin/{cmd}" if cmd == "gh" else None):
        assert hive_prompt.pr_cli_for_origin("git@github.com:tfiala/term-public.git") == "gh"
        assert hive_prompt.pr_cli_for_origin("https://github.com/tfiala/term-public.git") == "gh"


def test_pr_cli_for_non_github_urls_prefers_fj():
    with patch.object(hive_prompt.shutil, "which", side_effect=lambda cmd: f"/bin/{cmd}" if cmd == "fj" else None):
        assert hive_prompt.pr_cli_for_origin("https://git.home.invezt.io/infra/forgejo") == "fj"
        assert hive_prompt.pr_cli_for_origin("git@git.home.invezt.io:infra/forgejo.git") == "fj"


def test_pr_cli_for_origin_returns_none_when_cli_missing():
    with patch.object(hive_prompt.shutil, "which", return_value=None):
        assert hive_prompt.pr_cli_for_origin("https://github.com/tfiala/term-public.git") is None
        assert hive_prompt.pr_cli_for_origin("https://git.home.invezt.io/infra/forgejo") is None


def test_first_pr_number_handles_expected_payloads():
    assert hive_prompt.first_pr_number('[{"number": 48}]') == "48"
    assert hive_prompt.first_pr_number("[]") == ""
    assert hive_prompt.first_pr_number("{") == ""
