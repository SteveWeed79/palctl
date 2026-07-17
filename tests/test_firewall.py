"""The firewall helpers gate LAN dashboard access on Windows. The command
builders are pure and pinned here; the runners no-op off Windows (where a
0.0.0.0 bind reaches the LAN without a palctl rule) and are driven with a faked
platform + netsh so the add/remove/present logic is covered without a real
firewall."""

from palctl import firewall


def test_add_rule_command_shape():
    cmd = firewall.add_rule_command(8830)
    assert cmd[:5] == ["netsh", "advfirewall", "firewall", "add", "rule"]
    assert "name=palctl dashboard" in cmd
    assert "dir=in" in cmd and "action=allow" in cmd and "protocol=TCP" in cmd
    assert "localport=8830" in cmd
    assert "profile=private,domain" in cmd  # never "public"
    assert not any("public" in tok for tok in cmd)


def test_remove_and_show_commands():
    assert firewall.remove_rule_command() == [
        "netsh", "advfirewall", "firewall", "delete", "rule", "name=palctl dashboard",
    ]
    assert firewall.show_rule_command()[3:5] == ["show", "rule"]


def test_manual_add_command_quotes_the_name():
    s = firewall.manual_add_command(8830)
    assert 'name="palctl dashboard"' in s
    assert "localport=8830" in s and "profile=private,domain" in s


def test_runners_noop_off_windows(monkeypatch):
    monkeypatch.setattr(firewall.sys, "platform", "linux")
    assert firewall.ensure_rule(8830) == "skipped"
    assert firewall.remove_rule() == "skipped"
    assert firewall.rule_present() is False


class _Result:
    def __init__(self, code):
        self.returncode = code


def test_ensure_rule_added_present_and_failed(monkeypatch):
    monkeypatch.setattr(firewall, "_on_windows", lambda: True)
    present = {"v": False}
    monkeypatch.setattr(firewall, "rule_present", lambda **k: present["v"])

    monkeypatch.setattr(firewall, "_run", lambda cmd: _Result(0))
    assert firewall.ensure_rule(8830) == "added"

    present["v"] = True
    assert firewall.ensure_rule(8830) == "present"  # not added twice

    present["v"] = False
    monkeypatch.setattr(firewall, "_run", lambda cmd: _Result(1))  # netsh refused
    assert firewall.ensure_rule(8830) == "failed"


def test_remove_rule_absent_and_removed(monkeypatch):
    monkeypatch.setattr(firewall, "_on_windows", lambda: True)
    present = {"v": False}
    monkeypatch.setattr(firewall, "rule_present", lambda **k: present["v"])
    assert firewall.remove_rule() == "absent"

    present["v"] = True
    monkeypatch.setattr(firewall, "_run", lambda cmd: _Result(0))
    assert firewall.remove_rule() == "removed"
