#!/usr/bin/env python3
"""Tests for hive-ci-popup.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# Import the module under test
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / 'scripts'
sys.path.insert(0, str(_SCRIPTS_DIR))
import importlib
hive_ci_popup = importlib.import_module('hive-ci-popup')


class TestStatusIcon:
    """Tests for STATUS_ICON mapping."""

    def test_has_all_statuses(self) -> None:
        """STATUS_ICON should have all expected statuses."""
        expected = {'success', 'failure', 'running', 'waiting', 'cancelled', 'skipped'}
        assert expected <= set(hive_ci_popup.STATUS_ICON.keys())

    def test_icons_contain_ansi(self) -> None:
        """Icons should contain ANSI color codes."""
        for status, icon in hive_ci_popup.STATUS_ICON.items():
            assert '\033[' in icon, f"Icon for {status} should have ANSI codes"


class TestFindHiveRoot:
    """Tests for _find_hive_root()."""

    def test_returns_none_outside_git(self, tmp_path: Path, monkeypatch) -> None:
        """Returns None when not in a git repo."""
        monkeypatch.chdir(tmp_path)

        def mock_git(*args, cwd=None):
            return None

        monkeypatch.setattr(hive_ci_popup, '_git', mock_git)
        assert hive_ci_popup._find_hive_root() is None

    def test_returns_parent_of_git_root(self, tmp_path: Path, monkeypatch) -> None:
        """Returns parent of git root when inside a repo."""
        hive = tmp_path / 'hive'
        repo = hive / 'repo'
        repo.mkdir(parents=True)
        (repo / '.git').mkdir()

        def mock_git(*args, cwd=None):
            if args == ('rev-parse', '--show-toplevel'):
                return str(repo)
            return None

        monkeypatch.setattr(hive_ci_popup, '_git', mock_git)

        result = hive_ci_popup._find_hive_root()
        assert result == hive


class TestDiscoverWorkspaces:
    """Tests for _discover_workspaces()."""

    def test_finds_git_directories(self, tmp_path: Path) -> None:
        """Finds directories with .git subdirectory."""
        hive = tmp_path / 'hive'
        hive.mkdir()

        # Git repos
        repo1 = hive / 'repo-1'
        repo1.mkdir()
        (repo1 / '.git').mkdir()

        repo2 = hive / 'repo-2'
        repo2.mkdir()
        (repo2 / '.git').mkdir()

        # Non-git directory
        other = hive / 'not-a-repo'
        other.mkdir()

        result = hive_ci_popup._discover_workspaces(hive)

        assert len(result) == 2
        assert repo1 in result
        assert repo2 in result

    def test_returns_empty_for_nonexistent(self, tmp_path: Path) -> None:
        """Returns empty list for nonexistent directory."""
        result = hive_ci_popup._discover_workspaces(tmp_path / 'nonexistent')
        assert result == []


class TestParseForejoRemote:
    """Tests for _parse_forgejo_remote()."""

    def test_parses_https_url(self) -> None:
        """Parses HTTPS remote URL."""
        url = 'https://git.example.com/acme/widget.git'
        result = hive_ci_popup._parse_forgejo_remote(url)

        assert result is not None
        base, owner, repo = result
        assert base == 'https://git.example.com'
        assert owner == 'acme'
        assert repo == 'widget'

    def test_parses_ssh_url(self) -> None:
        """Parses SCP-style SSH URL."""
        url = 'git@git.example.com:acme/widget.git'
        result = hive_ci_popup._parse_forgejo_remote(url)

        assert result is not None
        base, owner, repo = result
        assert base == 'https://git.example.com'
        assert owner == 'acme'
        assert repo == 'widget'

    def test_parses_url_with_port(self) -> None:
        """Parses URL with port number."""
        url = 'http://git.example.com:3000/org/repo.git'
        result = hive_ci_popup._parse_forgejo_remote(url)

        assert result is not None
        base, owner, repo = result
        assert base == 'http://git.example.com:3000'
        assert owner == 'org'
        assert repo == 'repo'

    def test_returns_none_for_invalid(self) -> None:
        """Returns None for invalid URL."""
        url = 'not-a-valid-url'
        result = hive_ci_popup._parse_forgejo_remote(url)
        assert result is None

    def test_returns_none_for_local_path(self) -> None:
        """Local filesystem remotes are not Forgejo repos — must return None,
        not a bogus '://None' base that later crashes credential lookup."""
        for url in ('/tmp/repo.git', '../repo.git', 'file:///tmp/repo.git',
                    '/srv/git/weird:repo.git'):
            assert hive_ci_popup._parse_forgejo_remote(url) is None, url


class TestParseTime:
    """Tests for _parse_time() — PT must be DST-aware (review finding)."""

    def test_summer_timestamp_uses_pdt(self) -> None:
        # July → Pacific Daylight Time (UTC-7): 12:00Z → 05:00.
        assert hive_ci_popup._parse_time('2026-07-01T12:00:00Z') == '05:00'

    def test_winter_timestamp_uses_pst(self) -> None:
        # January → Pacific Standard Time (UTC-8): 12:00Z → 04:00.
        assert hive_ci_popup._parse_time('2026-01-01T12:00:00Z') == '04:00'

    def test_empty_string_returns_empty(self) -> None:
        assert hive_ci_popup._parse_time('') == ''


class TestDiscoverRepos:
    """Tests for _discover_repos()."""

    def test_deduplicates_by_identity(self, tmp_path: Path, monkeypatch) -> None:
        """Deduplicates repos by owner/repo identity."""
        hive = tmp_path / 'hive'
        hive.mkdir()

        # Create two workspaces pointing to the same repo
        for name in ['repo-1', 'repo-2']:
            ws = hive / name
            ws.mkdir()
            (ws / '.git').mkdir()

        def mock_git(*args, cwd=None):
            if args == ('remote', 'get-url', 'origin'):
                return 'https://git.example.com/org/same-repo.git'
            return None

        monkeypatch.setattr(hive_ci_popup, '_git', mock_git)

        result = hive_ci_popup._discover_repos(hive)

        # Should only have one entry despite two workspaces
        assert len(result) == 1
        assert result[0] == ('https://git.example.com', 'org', 'same-repo')

    def test_includes_multiple_distinct_repos(self, tmp_path: Path, monkeypatch) -> None:
        """Includes multiple distinct repos."""
        hive = tmp_path / 'hive'
        hive.mkdir()

        workspaces = []
        for name in ['repo-1', 'repo-2']:
            ws = hive / name
            ws.mkdir()
            (ws / '.git').mkdir()
            workspaces.append(ws)

        remote_map = {
            str(workspaces[0]): 'https://git.example.com/org/repo-a.git',
            str(workspaces[1]): 'https://git.example.com/org/repo-b.git',
        }

        def mock_git(*args, cwd=None):
            if args == ('remote', 'get-url', 'origin') and cwd:
                return remote_map.get(str(cwd))
            return None

        monkeypatch.setattr(hive_ci_popup, '_git', mock_git)

        result = hive_ci_popup._discover_repos(hive)

        assert len(result) == 2


class TestGetTokenFromCredentials:
    """Tests for _get_token_from_credentials()."""

    def test_reads_from_git_credentials(self, tmp_path: Path, monkeypatch) -> None:
        """Reads token from ~/.git-credentials."""
        creds = tmp_path / '.git-credentials'
        creds.write_text('https://user:mytoken123@git.example.com\n')
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)

        token = hive_ci_popup._get_token_from_credentials(
            'https://git.example.com')
        assert token == 'mytoken123'

    def test_returns_none_when_no_match(self, tmp_path: Path, monkeypatch) -> None:
        """Returns None when no matching credential."""
        creds = tmp_path / '.git-credentials'
        creds.write_text('https://user:tok@other.host.io\n')
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)

        token = hive_ci_popup._get_token_from_credentials(
            'https://git.example.com')
        assert token is None


class TestApiGet:
    """Tests for _api_get() error handling — silence 404 noise."""

    def _http_error(self, code):
        from urllib.error import HTTPError
        return HTTPError('http://x', code, 'Not Found', {}, None)

    def test_silences_404(self, monkeypatch, capsys):
        """A 404 is an expected 'no data at that index' outcome and must
        not print to the popup."""
        monkeypatch.setattr(hive_ci_popup, 'urlopen',
                            mock.MagicMock(side_effect=self._http_error(404)))
        result = hive_ci_popup._api_get('https://x', 'tok', '/pulls/9999')
        assert result == []
        assert capsys.readouterr().out == ''

    def test_prints_non_404_http_errors(self, monkeypatch, capsys):
        """A 500 (or other non-404 HTTP error) is genuine and is reported."""
        monkeypatch.setattr(hive_ci_popup, 'urlopen',
                            mock.MagicMock(side_effect=self._http_error(500)))
        result = hive_ci_popup._api_get('https://x', 'tok', '/pulls/1')
        assert result == []
        assert 'API error' in capsys.readouterr().out

    def test_prints_network_errors(self, monkeypatch, capsys):
        """A non-HTTP exception (timeout / DNS / connection) is reported."""
        from urllib.error import URLError
        monkeypatch.setattr(hive_ci_popup, 'urlopen',
                            mock.MagicMock(side_effect=URLError('refused')))
        result = hive_ci_popup._api_get('https://x', 'tok', '/anything')
        assert result == []
        assert 'API error' in capsys.readouterr().out


class TestVisLen:
    """Tests for _vis_len()."""

    def test_excludes_ansi_codes(self) -> None:
        """Excludes ANSI escape codes from length."""
        plain = 'hello'
        colored = '\033[32mhello\033[0m'

        assert hive_ci_popup._vis_len(plain) == 5
        assert hive_ci_popup._vis_len(colored) == 5

    def test_handles_multiple_codes(self) -> None:
        """Handles multiple ANSI codes."""
        text = '\033[1m\033[32mhello\033[0m \033[34mworld\033[0m'
        assert hive_ci_popup._vis_len(text) == 11  # "hello world"


class TestWfShort:
    """Tests for _wf_short()."""

    def test_abbreviates_known_workflows(self) -> None:
        """Abbreviates known workflow names."""
        assert hive_ci_popup._wf_short('ci.yml') == 'ci'
        assert hive_ci_popup._wf_short('lint.yaml') == 'lint'
        assert hive_ci_popup._wf_short('deploy.yml') == 'deploy'

    def test_strips_extension_for_unknown(self) -> None:
        """Strips extension for unknown workflows."""
        assert hive_ci_popup._wf_short('custom-workflow.yml') == 'custom-workflow'
        assert hive_ci_popup._wf_short('my-task.yaml') == 'my-task'


class TestNormalizeBranch:
    """Tests for _normalize_branch()."""

    def test_formats_pr_reference(self) -> None:
        """Formats PR reference with 'PR ' prefix."""
        assert hive_ci_popup._normalize_branch('#57', 'ci.yml') == 'PR #57'

    def test_truncates_sha(self) -> None:
        """Truncates full SHA to 7 characters."""
        sha = 'abcdef1234567890abcdef1234567890abcdef12'
        result = hive_ci_popup._normalize_branch(sha, 'ci.yml')
        assert result == 'abcdef1'

    def test_passes_through_branch_names(self) -> None:
        """Passes through regular branch names."""
        assert hive_ci_popup._normalize_branch('main', 'ci.yml') == 'main'
        assert hive_ci_popup._normalize_branch('feat/foo', 'ci.yml') == 'feat/foo'


class TestExtractPrNumber:
    """Tests for _extract_pr_number()."""

    def test_extracts_pr_number(self) -> None:
        """Extracts PR number from 'PR #N' format."""
        assert hive_ci_popup._extract_pr_number('PR #57') == 57
        assert hive_ci_popup._extract_pr_number('PR #123') == 123

    def test_returns_none_for_branches(self) -> None:
        """Returns None for non-PR branches."""
        assert hive_ci_popup._extract_pr_number('main') is None
        assert hive_ci_popup._extract_pr_number('feat/foo') is None


class TestBuildBranchGroups:
    """Tests for _build_branch_groups()."""

    def test_groups_by_branch(self) -> None:
        """Groups runs by branch."""
        runs = [
            {'prettyref': 'main', 'workflow_id': 'ci.yml', 'status': 'success',
             'title': 'Test', 'id': 1},
            {'prettyref': 'main', 'workflow_id': 'lint.yml', 'status': 'success',
             'title': 'Test', 'id': 2},
            {'prettyref': 'feat', 'workflow_id': 'ci.yml', 'status': 'running',
             'title': 'Feature', 'id': 3},
        ]

        groups = hive_ci_popup._build_branch_groups(runs)

        assert len(groups) == 2
        branches = {g['branch'] for g in groups}
        assert branches == {'main', 'feat'}

    def test_keeps_latest_per_workflow(self) -> None:
        """Keeps only the latest run per workflow per branch."""
        runs = [
            {'prettyref': 'main', 'workflow_id': 'ci.yml', 'status': 'success',
             'title': 'Latest', 'id': 2},
            {'prettyref': 'main', 'workflow_id': 'ci.yml', 'status': 'failure',
             'title': 'Old', 'id': 1},
        ]

        groups = hive_ci_popup._build_branch_groups(runs)

        assert len(groups) == 1
        assert groups[0]['workflows']['ci']['status'] == 'success'


class TestCollectPrNumbers:
    """Tests for _collect_pr_numbers()."""

    def test_extracts_pr_numbers_from_runs(self) -> None:
        """Extracts PR numbers from run refs."""
        runs = [
            {'prettyref': '#57', 'workflow_id': 'ci.yml'},
            {'prettyref': '#58', 'workflow_id': 'ci.yml'},
            {'prettyref': 'main', 'workflow_id': 'ci.yml'},
        ]

        result = hive_ci_popup._collect_pr_numbers(runs)

        assert result == {57, 58}
