"""Tailscale-only remote mode: listen on the Tailscale address and nowhere else."""
import pytest

from kwanttrades.web import server


@pytest.mark.parametrize("host,expected", [
    ("100.64.0.1", True), ("100.88.12.34", True), ("100.127.255.254", True),
    ("100.63.255.255", False), ("100.128.0.1", False), ("10.0.0.5", False),
    ("192.168.1.20", False), ("127.0.0.1", False), ("0.0.0.0", False), ("not-an-ip", False), ("", False),
])
def test_is_tailscale_ip(host, expected):
    assert server.is_tailscale_ip(host) is expected


def test_tailscale_ipv4_picks_the_tailscale_address(monkeypatch):
    monkeypatch.setattr(server.authmod, "lan_ipv4s", lambda: ["192.168.1.20", "100.95.1.2", "172.26.16.1"])
    assert server.tailscale_ipv4() == "100.95.1.2"
    monkeypatch.setattr(server.authmod, "lan_ipv4s", lambda: ["192.168.1.20"])
    assert server.tailscale_ipv4() is None


def test_server_refuses_ordinary_lan_address():
    with pytest.raises(ValueError):
        server.DashboardServer(0, None, host="192.168.1.20", auth=object())


def test_server_refuses_tailscale_address_without_login():
    with pytest.raises(ValueError, match="remote mode"):
        server.DashboardServer(0, None, host="100.95.1.2", auth=None)


def test_main_tailscale_and_host_are_exclusive(capsys):
    assert server.main(["--tailscale", "--host", "0.0.0.0", "--no-browser"]) == 2
    assert "either --tailscale or --host" in capsys.readouterr().err


def test_main_tailscale_needs_a_connection(monkeypatch, capsys):
    monkeypatch.setattr(server, "tailscale_ipv4", lambda: None)
    assert server.main(["--tailscale", "--no-browser"]) == 2
    assert "Tailscale is not connected" in capsys.readouterr().err


def test_main_tailscale_still_requires_a_password(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("KWANT_WEB_PASSWORD", raising=False)
    monkeypatch.setattr(server, "tailscale_ipv4", lambda: "100.95.1.2")
    assert server.main(["--tailscale", "--no-browser"]) == 2
    assert "KWANT_WEB_PASSWORD" in capsys.readouterr().err


def test_banner_for_tailscale_shows_only_that_address(monkeypatch):
    monkeypatch.setattr(server.authmod, "lan_ipv4s", lambda: ["192.168.1.20", "100.95.1.2"])
    text = server.remote_banner(8787, "100.95.1.2")
    assert "http://100.95.1.2:8787" in text
    assert "192.168.1.20" not in text
