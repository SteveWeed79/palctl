"""
Prometheus exposition for the daemon.

palctl keeps seven days of metrics in SQLite and can show about two hours of
them, in its own sparkline, with no way to export any of it. That is a lot of
collected data an operator cannot answer a question with — "was the server
slow last Tuesday?" is unanswerable from inside palctl and trivial in Grafana.

This is deliberately the *small* version of the idea, following mc-monitor's
shape rather than a panel's: a handful of well-labelled gauges, collected at
scrape time, over the transport everything already speaks. palctl does not
become a metrics system; it hands the numbers to one.

The renderer is pure text-in/text-out so the format is testable — the exposition
format is fussy in ways that fail silently at the scraper rather than here (a
missing HELP line is tolerated, a stray `#` in a label value is not, and NaN is
a valid float in Python and a parse error in Prometheus).
"""

from __future__ import annotations

import math
from collections.abc import Iterable

# Everything palctl exports is a gauge: these are all point-in-time readings of
# a server that may not even be running. No counters, because palctl's own
# restarts would reset them and a reset counter is worse than no counter.
_PREFIX = "palctl"


def _sanitise(value: float | int | bool | None) -> float | None:
    """A number Prometheus will accept, or None to omit the sample entirely.

    NaN and infinity are ordinary Python floats and a parse error at the
    scraper — and they arrive here easily, since frame time is a division and
    an idle server reports zero. Omitting the sample is right: a gauge that is
    absent for a scrape reads as "not measured", which is the truth, whereas
    substituting 0 would read as "measured, and it was zero".
    """
    if value is None or isinstance(value, str):
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _format(value: float) -> str:
    """A number that keeps its precision.

    `%g` defaults to six significant digits, which turns 8,493,981,696 bytes of
    resident memory into 8.49399e+09 — a valid sample, and 1.7 MB adrift. An
    integral value is written as an integer and everything else with repr's
    round-tripping precision.
    """
    if value.is_integer() and abs(value) < 2**63:
        return str(int(value))
    return repr(value)


def _line(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    if not labels:
        return f"{_PREFIX}_{name} {_format(value)}"
    # Label values are escaped per the exposition format: backslash, quote and
    # newline. A player name or a world id lands in here, and neither is ours.
    rendered = ",".join(
        '{}="{}"'.format(
            key,
            str(val).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"),
        )
        for key, val in sorted(labels.items())
    )
    return f"{_PREFIX}_{name}{{{rendered}}} {_format(value)}"


def _metric(
    name: str,
    help_text: str,
    value: float | int | bool | None,
    *,
    kind: str = "gauge",
    labels: dict[str, str] | None = None,
) -> list[str]:
    number = _sanitise(value)
    if number is None:
        return []
    return [
        f"# HELP {_PREFIX}_{name} {help_text}",
        f"# TYPE {_PREFIX}_{name} {kind}",
        _line(name, number, labels),
    ]


def render(state: dict, *, version: str = "", degraded: Iterable[str] = ()) -> str:
    """The scrape body, built from the same dict `/state` serves.

    Reusing `/state` rather than collecting separately is the point: two
    collectors drift, and a metrics endpoint that disagrees with the dashboard
    is worse than none. Anything `/state` could not measure is simply absent
    here — see _sanitise.
    """
    lines: list[str] = []

    # Always emitted, always 1: the sample that lets an alert distinguish "the
    # server is down" from "palctl is down and cannot tell you".
    lines += _metric(
        "up", "1 when the palctl daemon answered this scrape.", 1,
        labels={"version": version} if version else None,
    )

    alive = state.get("alive")
    lines += _metric(
        "server_alive",
        "1 when the game server's REST API answered palctl's last poll.",
        1 if alive else 0,
    )
    service = state.get("service")
    if isinstance(service, str) and service:
        lines += _metric(
            "server_service_running",
            "1 when the game server's service reports RUNNING.",
            1 if service.upper() == "RUNNING" else 0,
            labels={"state": service},
        )

    lines += _metric(
        "operation_in_progress",
        "1 while palctl holds its operation lock (backup, update, restart…).",
        1 if state.get("operation") else 0,
        labels={"operation": str(state.get("operation") or "none")},
    )

    m = state.get("metrics") or {}
    lines += _metric("players_online", "Players currently connected.",
                     m.get("current_players"))
    lines += _metric("players_max", "Player slots configured on the server.",
                     m.get("max_players"))
    lines += _metric("server_fps", "Server-side frames per second.",
                     m.get("server_fps"))
    lines += _metric("server_frame_time_ms", "Server-side frame time.",
                     m.get("server_frame_time"))
    lines += _metric("server_uptime_seconds", "Game server uptime.",
                     m.get("uptime"))
    lines += _metric("world_days", "In-game days elapsed.", m.get("days"))
    lines += _metric("base_camps", "Base camps in the world.", m.get("base_camps"))

    p = state.get("process") or {}
    memory_mb = _sanitise(p.get("memory_mb"))
    if memory_mb is not None:
        # Bytes, not megabytes: Prometheus convention is base units, and a
        # dashboard that has to know palctl chose MB is a dashboard that will
        # get it wrong once.
        lines += _metric(
            "server_memory_bytes",
            "Resident memory of the game server process.",
            memory_mb * 1_048_576,
        )
    lines += _metric(
        "server_cpu_cores",
        "Game server CPU use, in cores (1.0 = one core fully busy).",
        p.get("cpu_cores"),
    )
    lines += _metric(
        "server_threads", "Threads in the game server process.", p.get("threads")
    )
    lines += _metric(
        "server_instances",
        "Palworld server processes seen; >1 means a leftover is running.",
        p.get("instances"),
    )

    limit_mb = _sanitise(state.get("memory_limit_mb"))
    if limit_mb:
        lines += _metric(
            "watchdog_memory_limit_bytes",
            "Resident memory at which the leak watchdog restarts the server.",
            limit_mb * 1_048_576,
        )

    countdown = state.get("countdown") or {}
    lines += _metric(
        "countdown_seconds_remaining",
        "Seconds until a pending restart/restore/update, when one is running.",
        countdown.get("seconds_remaining"),
        labels={"kind": str(countdown.get("kind") or "none")},
    )

    # Non-empty means a worker loop crashed past its restart budget. The single
    # most alert-worthy number here: the daemon is up and NOT doing its job.
    names = [n for n in degraded]
    lines += _metric(
        "workers_degraded",
        "Worker loops that crashed past their restart budget and stopped.",
        len(names),
    )
    for name in sorted(names):
        lines += _metric(
            "worker_degraded",
            "1 for each worker loop that has stopped for good.",
            1,
            labels={"worker": name},
        )

    return "\n".join(lines) + "\n"
