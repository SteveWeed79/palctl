"""The diagnostics bundle is what makes 'send me your logs' one click. It must
include the logs, the config, and a summary — and must not fall over when a
brand-new install has no logs yet."""

import zipfile

import palctl.diagnostics as diag


def test_bundle_contains_logs_config_and_summary(tmp_path, monkeypatch):
    cfgdir = tmp_path / "palctl"
    (cfgdir / "logs").mkdir(parents=True)
    (cfgdir / "config.json").write_text('{"api_port": 8212}', encoding="utf-8")
    (cfgdir / "logs" / "palctl.log").write_text("some log line", encoding="utf-8")
    monkeypatch.setattr(diag, "config_dir", lambda: cfgdir)
    monkeypatch.setattr(diag, "CONFIG_PATH", cfgdir / "config.json")

    out = diag.build_bundle(tmp_path / "diag.zip")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        summary = z.read("system.txt").decode("utf-8")

    assert "system.txt" in names
    assert "config.json" in names
    assert "logs/palctl.log" in names
    assert "palctl diagnostics" in summary


def test_bundle_ok_with_nothing_to_include(tmp_path, monkeypatch):
    cfgdir = tmp_path / "empty"
    cfgdir.mkdir()
    monkeypatch.setattr(diag, "config_dir", lambda: cfgdir)
    monkeypatch.setattr(diag, "CONFIG_PATH", cfgdir / "config.json")  # doesn't exist

    out = diag.build_bundle(tmp_path / "d.zip")
    with zipfile.ZipFile(out) as z:
        assert "system.txt" in z.namelist()  # summary is always there
