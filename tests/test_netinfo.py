"""netinfo talks to the network, so we only pin what's deterministic: the game
port default, and that lan_ip returns a plausible address or None without ever
raising (a lookup failure must not crash the wizard)."""

from palctl import netinfo


def test_game_port_default():
    assert netinfo.GAME_PORT_DEFAULT == 8211


def test_lan_ip_returns_str_or_none():
    ip = netinfo.lan_ip()
    assert ip is None or (isinstance(ip, str) and ip.count(".") == 3)
