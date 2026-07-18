"""
Register Windows services with WinSW.

Two services actually have to exist for palctl to do its job:

  * **PalServer** — the game server. It is not service-aware (it's a plain
    console exe), so Windows can't supervise it directly; it needs a shim.
  * **palctl-daemon** — the watchdog / scheduler / Discord bot. Same story.

The shim is WinSW (the wrapper Jenkins ships), which replaced NSSM here: NSSM's
last release was 2014 and it is unmaintained, while WinSW is maintained and —
more importantly — configured *declaratively*. The whole service definition
lives in one XML file that is rewritten on every install, so the NSSM-era bug
class where a re-install inherited stale per-setting state (an old account, old
launch arguments) is structurally impossible: there is no per-setting state,
only the file.

Registration model (WinSW v2): each service is a copy of the WinSW exe next to
an XML config with the same basename; `<copy>.exe install` registers it.
Removal and start/stop go through plain `sc.exe`, which works on any service —
including ones registered by the old NSSM builds, so upgrades migrate cleanly.

Windows-only in practice. The XML builder and command sequencing are pure and
unit tested anywhere; the runners no-op / raise cleanly off Windows.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

# Stable WinSW release: the last v2 stable. v2 targets .NET Framework 4.6.1+,
# which every supported Windows (10/11, Server 2016+) ships with — no runtime
# to install. (v3 is still prerelease; revisit when it goes stable.)
WINSW_URL = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"

# SHA-256 of that exact exe. Same reasoning as the old NSSM pin: this binary is
# registered as / runs LocalSystem services, so a compromised source or a MITM
# on the download is SYSTEM-level code execution — refuse anything that doesn't
# match. The value is verified from the bytes GitHub serves for the v2.12.0
# release asset (checked from CI infrastructure independent of any one
# developer machine; see the install-lifecycle CI job, which re-downloads and
# re-hashes it on every run).
WINSW_SHA256 = "05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class WrapperChecksumError(RuntimeError):
    """The downloaded service wrapper didn't match the pinned SHA-256. We
    refuse to run a binary we can't vouch for as SYSTEM."""


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------- pure: the declarative service definition ----------------


def winsw_config_xml(
    name: str,
    exe: str | Path,
    args: str = "",
    app_dir: str | Path | None = None,
    *,
    user: str | None = None,
    password: str | None = None,
    appdata: str | None = None,
    description: str | None = None,
) -> str:
    """
    The complete WinSW service definition. Pure — install just writes this file
    and registers it, which is also what makes it testable.

    The account matters more than it looks: a service defaults to LocalSystem,
    which has its OWN %APPDATA% and Credential Manager — a daemon there reads a
    different config.json and token than the user's GUI, and can't decrypt the
    user's DPAPI secrets at all. So:

      * `user`/`password` set — run the service AS that account
        (<serviceaccount>, with allowservicelogon granting the logon right).
      * else, `appdata` set — stay LocalSystem but point %APPDATA% at the
        installing user's config dir, so config, token, and logs are shared.
        Per-user secrets remain unreadable (the daemon falls back to the
        AdminPassword already sitting in PalWorldSettings.ini).

    <onfailure action="restart"> gives the same 'keep it up' behaviour the
    systemd unit's Restart=on-failure provides on Linux.
    """
    q = lambda s: escape(str(s), {'"': "&quot;"})  # noqa: E731  (tiny, local)
    lines = [
        "<service>",
        f"  <id>{escape(name)}</id>",
        f"  <name>{escape(name)}</name>",
        f"  <description>{escape(description or name)}</description>",
        f"  <executable>{escape(str(exe))}</executable>",
    ]
    if args:
        lines.append(f"  <arguments>{escape(args)}</arguments>")
    if app_dir:
        lines.append(f"  <workingdirectory>{escape(str(app_dir))}</workingdirectory>")
    if user:
        lines += [
            "  <serviceaccount>",
            f"    <username>{escape(user)}</username>",
            f"    <password>{escape(password or '')}</password>",
            "    <allowservicelogon>true</allowservicelogon>",
            "  </serviceaccount>",
        ]
    elif appdata:
        lines.append(f'  <env name="APPDATA" value="{q(appdata)}"/>')
    lines += [
        "  <startmode>Automatic</startmode>",
        '  <onfailure action="restart" delay="5 sec"/>',
        "  <stoptimeout>30 sec</stoptimeout>",
        # The daemon and PalServer both do their own logging; the wrapper
        # capturing stdout too would just duplicate it into the config dir.
        '  <log mode="none"/>',
        "</service>",
        "",
    ]
    return "\n".join(lines)


def wrapper_paths(cache_dir: Path, name: str) -> tuple[Path, Path]:
    """The per-service WinSW copy and its config. WinSW v2 pairs them by
    basename: `<name>-service.exe` reads `<name>-service.xml`."""
    base = cache_dir / f"{name}-service"
    return base.with_suffix(".exe"), base.with_suffix(".xml")


# ---------------- runners (Windows) ----------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, creationflags=_NO_WINDOW
    )


def service_exists(name: str) -> bool:
    from . import procs

    return procs.service_state(name) != "UNKNOWN"


def _wait_for(predicate, timeout: float, interval: float = 1.0) -> bool:
    """Poll `predicate` until it's true or `timeout` elapses. The SCM acts
    asynchronously (stops and deletions land after the command returns), so
    anything sequencing service operations needs this."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def install_service(
    winsw: str | Path,
    name: str,
    exe: str | Path,
    args: str = "",
    app_dir: str | Path | None = None,
    *,
    user: str | None = None,
    password: str | None = None,
    appdata: str | None = None,
    start: bool = True,
) -> None:
    """Register the service from a freshly written config, then optionally
    start it.

    A re-install replaces the whole registration: any existing service (this
    module's, or one left by the old NSSM builds) is stopped and removed first,
    and the config XML is rewritten whole — so nothing stale can survive from a
    previous install, by construction."""
    winsw = Path(winsw)
    if service_exists(name):
        remove_service(name)
        # The SCM deletes asynchronously, and re-creating a name whose old
        # registration is still pending deletion fails. Wait until it's
        # actually gone before registering — and if it never goes, say why
        # instead of silently issuing commands against a zombie registration.
        if not _wait_for(lambda: not service_exists(name), timeout=30.0):
            raise RuntimeError(
                f"Windows still reports service '{name}' as registered after "
                "removal — it is pending deletion, which usually means "
                "something holds a handle to it (an open services.msc or Task "
                "Manager). Close those and run the install again."
            )
    svc_exe, svc_xml = wrapper_paths(winsw.parent, name)
    # The old copy can't be running here (the service was just removed), so the
    # overwrite is safe; copying fresh every install also upgrades the wrapper
    # whenever the pinned WinSW version moves.
    shutil.copyfile(winsw, svc_exe)
    svc_xml.write_text(
        winsw_config_xml(
            name, exe, args, app_dir,
            user=user, password=password, appdata=appdata,
        ),
        encoding="utf-8",
    )
    cp = _run([str(svc_exe), "install"])
    if cp.returncode != 0:
        # WinSW registering the service is the one step here with no later
        # verification to catch it (the daemon-reachable check belongs to the
        # caller, and the PalServer service isn't started until setup's verify
        # step). Claiming "registered" after a failed install is exactly the
        # silent-failure class this module exists to prevent.
        detail = " ".join(
            part for part in ((cp.stderr or "").strip(), (cp.stdout or "").strip()) if part
        )
        raise RuntimeError(
            f"WinSW could not register the '{name}' service "
            f"(exit {cp.returncode})" + (f": {detail}" if detail else "")
        )
    if start:
        _run([str(svc_exe), "start"])


def start_service(name: str) -> None:
    """Start a registered service (any service — plain SCM, no wrapper
    needed). Split out so a caller can do work between registering
    (install_service(start=False)) and starting — e.g. clearing a stale
    daemon off the port the service is about to need."""
    _run(["sc.exe", "start", name])


def remove_service(name: str) -> None:
    """Stop and delete a service via plain `sc.exe` — works on any service,
    including ones registered by the old NSSM builds, so upgrades migrate
    without needing the old wrapper around."""
    from . import procs

    _run(["sc.exe", "stop", name])
    # Removing a service that hasn't fully stopped marks it "pending deletion"
    # in the SCM — a state that blocks re-creating the name until every handle
    # closes. Wait for the stop to land first (PalServer can take a while
    # saving the world on the way down).
    _wait_for(
        lambda: procs.service_state(name) in ("STOPPED", "UNKNOWN"), timeout=90.0
    )
    _run(["sc.exe", "delete", name])


def ensure_winsw(
    cache_dir: Path, *, url: str = WINSW_URL, sha256: str = WINSW_SHA256
) -> Path:
    """
    Return a usable WinSW exe, downloading and caching it under ``cache_dir``
    the first time. The cached copy is re-verified against the pinned SHA-256
    on every call — the cache is not a trust store: a copy that no longer
    matches the pin (the pinned WinSW version moved, or the file was corrupted
    or replaced on disk) is refreshed from the pinned URL instead of being
    handed out to run as SYSTEM. Pass ``sha256=""`` only to deliberately skip
    verification (there is no good reason to in production).
    """
    cached = cache_dir / "winsw.exe"
    if cached.exists():
        if not sha256 or _sha256_of(cached) == sha256.lower():
            return cached
        cached.unlink()

    cache_dir.mkdir(parents=True, exist_ok=True)
    # Download into the cache dir itself so the final os.replace is atomic:
    # a crash mid-write can never leave a half-written winsw.exe behind.
    with tempfile.NamedTemporaryFile(
        suffix=".exe.part", dir=cache_dir, delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        # Timeout so a hung/blocked CDN can't stall setup indefinitely.
        with urllib.request.urlopen(url, timeout=60) as resp, tmp_path.open("wb") as f:
            shutil.copyfileobj(resp, f)
        # Verify BEFORE it can be used — this binary ends up running as SYSTEM,
        # so a tampered or corrupted download must never become winsw.exe.
        actual = _sha256_of(tmp_path)
        if sha256 and actual.lower() != sha256.lower():
            raise WrapperChecksumError(
                f"WinSW download from {url} failed its checksum: expected "
                f"{sha256}, got {actual}. Refusing to install an unverified "
                "service binary. Check your network/proxy for interference, or "
                "open a palctl issue if the release asset has genuinely been "
                "reissued."
            )
        os.replace(tmp_path, cached)
        return cached
    finally:
        tmp_path.unlink(missing_ok=True)
