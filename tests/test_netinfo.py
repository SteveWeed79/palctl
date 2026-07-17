"""netinfo talks to the network, so we only pin what's deterministic: the game
port default, and that lan_ip returns a plausible address or None without ever
raising (a lookup failure must not crash the wizard)."""

from palctl import netinfo


def test_game_port_default():
    assert netinfo.GAME_PORT_DEFAULT == 8211


def test_lan_ip_returns_str_or_none():
    ip = netinfo.lan_ip()
    assert ip is None or (isinstance(ip, str) and ip.count(".") == 3)


def test_is_loopback():
    for host in ("127.0.0.1", "localhost", "::1", "", "  127.0.0.1  ", "LOCALHOST"):
        assert netinfo.is_loopback(host), host
    for host in ("0.0.0.0", "::", "192.168.1.10", "10.0.0.5"):
        assert not netinfo.is_loopback(host), host


def test_dashboard_targets_loopback_has_no_shareable_url():
    open_url, share = netinfo.dashboard_targets(
        "127.0.0.1", 8830, "tok", lan_ip="192.168.1.5"
    )
    assert open_url == "http://127.0.0.1:8830/#tok"
    assert share is None  # nothing off-box can reach a loopback bind


def test_dashboard_targets_wildcard_opens_loopback_shares_lan_ip():
    open_url, share = netinfo.dashboard_targets(
        "0.0.0.0", 8830, "tok", lan_ip="192.168.1.5"
    )
    # 0.0.0.0 isn't itself connectable, so a local browser dials loopback...
    assert open_url == "http://127.0.0.1:8830/#tok"
    # ...but another device uses the box's real LAN address.
    assert share == "http://192.168.1.5:8830/#tok"


def test_dashboard_targets_wildcard_without_lan_ip():
    open_url, share = netinfo.dashboard_targets("0.0.0.0", 8830, "tok", lan_ip=None)
    assert open_url == "http://127.0.0.1:8830/#tok"
    assert share is None  # LAN bind, but we couldn't determine the address


def test_dashboard_targets_specific_interface_ip():
    # A specific interface bind is dialed directly for both the local browser
    # and other devices (loopback wouldn't reach it).
    open_url, share = netinfo.dashboard_targets(
        "192.168.1.5", 8830, "tok", lan_ip="10.0.0.9"
    )
    assert open_url == "http://192.168.1.5:8830/#tok"
    assert share == "http://192.168.1.5:8830/#tok"
