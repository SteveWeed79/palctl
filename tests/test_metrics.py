"""Prometheus exposition.

The format is fussy in ways that fail at the *scraper*, silently, hours later —
a NaN, an unescaped quote in a label, a number rendered in a precision-losing
notation. So the renderer is pure and the traps are pinned here.
"""

from __future__ import annotations

import math

import pytest

from palctl import metrics
from palctl.metrics import render


def samples(text: str) -> dict[str, str]:
    """{metric-with-labels: value} for every non-comment line."""
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name, _, value = line.rpartition(" ")
        out[name] = value
    return out


def test_up_is_always_emitted():
    """The sample that distinguishes "the server is down" from "palctl is down
    and cannot tell you"."""
    assert samples(render({}))["palctl_up"] == "1"


def test_the_version_rides_on_up_when_known():
    assert 'palctl_up{version="1.2.3"}' in samples(render({}, version="1.2.3"))


def test_every_line_is_a_name_help_type_triple():
    text = render({"alive": True})
    for name in ("palctl_up", "palctl_server_alive"):
        assert f"# HELP {name} " in text
        assert f"# TYPE {name} gauge" in text


def test_the_body_ends_with_a_newline():
    """A scrape body without a trailing newline is a parse error."""
    assert render({}).endswith("\n")


# ---------------- the numbers ----------------


def test_memory_is_reported_in_bytes_without_losing_precision():
    """%g defaults to six significant digits, which puts 8.5 GB of resident
    memory 1.7 MB adrift and reads as a plausible number."""
    text = render({"process": {"memory_mb": 8100.5}})

    assert samples(text)["palctl_server_memory_bytes"] == "8493989888"


def test_a_fractional_value_keeps_its_precision():
    text = render({"process": {"cpu_cores": 1.2400000000001}})
    assert samples(text)["palctl_server_cpu_cores"] == "1.2400000000001"


def test_an_integral_float_is_written_as_an_integer():
    assert metrics._format(4.0) == "4"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_number_prometheus_cannot_parse_is_omitted_entirely(bad):
    """Absent reads as "not measured", which is the truth. Substituting 0 would
    read as "measured, and it was zero"."""
    text = render({"metrics": {"server_frame_time": bad}})

    assert "palctl_server_frame_time_ms" not in samples(text)
    assert not any(math.isnan(0) for _ in [])  # sanity: nothing else swallowed it
    assert "palctl_up" in samples(text)  # the rest of the scrape still renders


def test_a_missing_reading_is_simply_absent():
    text = render({"metrics": {}})
    assert "palctl_players_online" not in samples(text)


def test_a_string_where_a_number_belongs_is_dropped_not_crashed():
    assert "palctl_players_online" not in samples(
        render({"metrics": {"current_players": "lots"}})
    )


# ---------------- labels ----------------


def test_a_label_value_with_a_quote_is_escaped():
    """An unescaped quote breaks the whole scrape, and label values come from
    places palctl does not control."""
    text = render({"service": 'RUN"NING'})

    assert '\\"' in text
    assert 'state="RUN\\"NING"' in text


def test_a_label_value_with_a_backslash_is_escaped():
    text = render({"service": "RUN\\NING"})
    assert 'state="RUN\\\\NING"' in text


def test_a_label_value_with_a_newline_cannot_forge_a_sample():
    """A newline in a label would end the line and let the rest be parsed as a
    metric of its own."""
    text = render({"service": "RUNNING\npalctl_evil 1"})

    assert "\\n" in text
    assert "palctl_evil" not in samples(text)


# ---------------- what it reports ----------------


def test_a_live_server_reads_as_alive():
    assert samples(render({"alive": True}))["palctl_server_alive"] == "1"
    assert samples(render({"alive": False}))["palctl_server_alive"] == "0"


def test_the_service_state_rides_as_a_label():
    text = samples(render({"service": "STOPPED"}))
    assert text['palctl_server_service_running{state="STOPPED"}'] == "0"


def test_an_operation_in_progress_is_named():
    text = samples(render({"operation": "backup"}))
    assert text['palctl_operation_in_progress{operation="backup"}'] == "1"


def test_an_idle_daemon_says_so_with_a_none_label():
    text = samples(render({}))
    assert text['palctl_operation_in_progress{operation="none"}'] == "0"


def test_a_leftover_second_server_is_visible():
    """instances > 1 is the collision palctl otherwise only mentions in a
    dashboard footnote."""
    text = samples(render({"process": {"instances": 2}}))
    assert text["palctl_server_instances"] == "2"


def test_the_watchdog_limit_is_exported_so_a_graph_can_draw_the_line():
    text = samples(render({"memory_limit_mb": 12000}))
    assert text["palctl_watchdog_memory_limit_bytes"] == str(12000 * 1_048_576)


def test_a_running_countdown_is_visible_with_its_kind():
    text = samples(render({"countdown": {"kind": "restart", "seconds_remaining": 300}}))
    assert text['palctl_countdown_seconds_remaining{kind="restart"}'] == "300"


# ---------------- the alert-worthy one ----------------


def test_a_healthy_daemon_reports_no_degraded_workers():
    assert samples(render({}))["palctl_workers_degraded"] == "0"


def test_each_dead_worker_is_named():
    """The most alert-worthy number here: the daemon is up and NOT doing its
    job, which /healthz alone would report as healthy."""
    text = samples(render({}, degraded={"watchdog": "boom", "scheduler": "bad cfg"}))

    assert text["palctl_workers_degraded"] == "2"
    assert text['palctl_worker_degraded{worker="watchdog"}'] == "1"
    assert text['palctl_worker_degraded{worker="scheduler"}'] == "1"


def test_the_renderer_never_raises_on_a_junk_payload():
    """It is fed /state, and /state grows fields. A metrics endpoint that 500s
    on an unexpected shape takes the monitoring down with it."""
    for junk in ({}, {"metrics": None}, {"process": None}, {"countdown": []},
                 {"service": None}, {"metrics": {"server_fps": None}}):
        assert render(junk).endswith("\n")
