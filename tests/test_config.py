import subprocess
from pathlib import Path

from screening import config


def test_env_var_wins_over_keychain(monkeypatch):
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "from-env")
    assert config.get_secret("OPENSANCTIONS_API_KEY") == "from-env"


def test_keychain_used_when_env_missing(monkeypatch):
    monkeypatch.delenv("NAMESCAN_API_KEY", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="from-keychain\n", stderr="")

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    assert config.get_secret("NAMESCAN_API_KEY") == "from-keychain"
    assert calls[0] == ["security", "find-generic-password", "-s", "NAMESCAN_API_KEY", "-w"]


def test_missing_everywhere_returns_none(monkeypatch):
    monkeypatch.delenv("NAMESCAN_API_KEY", raising=False)

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(44, cmd)

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    assert config.get_secret("NAMESCAN_API_KEY") is None


def test_cases_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENING_CASES_DIR", str(tmp_path / "c"))
    assert config.cases_dir() == tmp_path / "c"


def test_cases_dir_default_is_repo_cases(monkeypatch):
    monkeypatch.delenv("SCREENING_CASES_DIR", raising=False)
    assert config.cases_dir() == config.REPO_ROOT / "cases"


def test_max_subjects_default_and_override(monkeypatch):
    monkeypatch.delenv("NAMESCAN_MAX_SUBJECTS_RUN", raising=False)
    assert config.max_subjects_run() == 20
    monkeypatch.setenv("NAMESCAN_MAX_SUBJECTS_RUN", "5")
    assert config.max_subjects_run() == 5
