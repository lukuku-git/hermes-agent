"""launchd system-domain awareness (2026-09-07 gateway duplicate registration).

`ai.hermes.gateway` was registered as a LaunchDaemon under /Library/LaunchDaemons
AND as a user LaunchAgent. Both carry KeepAlive and run `gateway run --replace`,
so each start killed the other's process: the PID churned and Slack/Telegram
sockets flapped. Nothing in the launchd path had ever looked at the system
domain, so `status` claimed the gateway ran "manually" and `install` created the
second supervisor without a word.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

pwd = pytest.importorskip("pwd")

import hermes_cli.gateway as gateway_cli


@pytest.fixture
def system_plist(tmp_path, monkeypatch):
    """A registered system LaunchDaemon for this profile's label."""
    path = tmp_path / "LaunchDaemons" / "ai.hermes.gateway.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr(gateway_cli, "get_launchd_system_plist_path", lambda: path)
    return path


@pytest.fixture
def no_user_agent(tmp_path, monkeypatch):
    """A user LaunchAgent path that does not exist."""
    path = tmp_path / "LaunchAgents" / "ai.hermes.gateway.plist"
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: path)
    return path


class TestSystemPlistPath:
    def test_system_plist_lives_under_library_launchdaemons(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "_profile_suffix", lambda: "")
        assert gateway_cli.get_launchd_system_plist_path() == Path(
            "/Library/LaunchDaemons/ai.hermes.gateway.plist"
        )

    def test_system_plist_is_scoped_per_profile(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "_profile_suffix", lambda: "coder")
        assert gateway_cli.get_launchd_system_plist_path() == Path(
            "/Library/LaunchDaemons/ai.hermes.gateway-coder.plist"
        )


class TestSystemDaemonIsSeen:
    def test_service_is_installed_when_only_the_system_daemon_exists(
        self, system_plist, no_user_agent, monkeypatch
    ):
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)

        assert gateway_cli._is_service_installed() is True

    def test_snapshot_reports_system_scope_instead_of_a_manual_process(
        self, system_plist, no_user_agent, monkeypatch
    ):
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_windows", lambda: False)
        monkeypatch.setattr(gateway_cli, "_probe_launchd_service_running", lambda: False)
        monkeypatch.setattr(gateway_cli, "_launchd_system_service_pid", lambda: 4242)
        monkeypatch.setattr(gateway_cli, "find_gateway_pids", lambda: [4242])

        snapshot = gateway_cli.get_gateway_runtime_snapshot()

        assert snapshot.service_installed is True
        assert snapshot.service_running is True
        assert snapshot.service_scope == "launchd (system)"
        assert snapshot.has_process_service_mismatch is False

    def test_service_pids_include_the_system_daemon(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)
        monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: "ai.hermes.gateway")
        monkeypatch.setattr(gateway_cli, "_launchd_system_service_pid", lambda: 4242)
        monkeypatch.setattr(
            gateway_cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
        )

        assert 4242 in gateway_cli._get_service_pids()


class TestInstallRefusesDuplicateRegistration:
    def test_install_refuses_when_a_system_daemon_owns_the_label(
        self, system_plist, no_user_agent, capsys
    ):
        gateway_cli.launchd_install()

        out = capsys.readouterr().out
        assert "Refusing to install" in out
        assert str(system_plist) in out
        assert not no_user_agent.exists()

    def test_start_does_not_regenerate_a_user_agent_over_the_daemon(
        self, system_plist, no_user_agent, capsys
    ):
        gateway_cli.launchd_start()

        out = capsys.readouterr().out
        assert "Refusing to regenerate" in out
        assert not no_user_agent.exists()


class TestStatusReportsTheSystemDaemon:
    def test_status_reports_the_supervising_system_daemon(
        self, system_plist, no_user_agent, monkeypatch, capsys
    ):
        monkeypatch.setattr(gateway_cli, "_launchd_system_service_pid", lambda: 4242)

        gateway_cli.launchd_status()

        out = capsys.readouterr().out
        assert "system scope" in out
        assert "PID 4242" in out

    def test_status_names_the_duplicate_when_both_domains_are_registered(
        self, system_plist, tmp_path, monkeypatch, capsys
    ):
        user_plist = tmp_path / "LaunchAgents" / "ai.hermes.gateway.plist"
        user_plist.parent.mkdir(parents=True, exist_ok=True)
        user_plist.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: user_plist)
        monkeypatch.setattr(gateway_cli, "_launchd_system_service_pid", lambda: 4242)
        monkeypatch.setattr(gateway_cli, "launchd_plist_is_current", lambda: True)
        monkeypatch.setattr(
            gateway_cli, "_parse_launchd_pid_from_list_output", lambda output: None
        )
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""),
        )

        gateway_cli.launchd_status()

        out = capsys.readouterr().out
        assert "DUPLICATE REGISTRATION" in out
        assert str(user_plist) in out
