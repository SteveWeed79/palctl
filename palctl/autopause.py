"""
Put an empty server away, and bring it back when someone knocks.

A Palworld server with nobody on it still burns a core and several gigabytes.
The field's answer is auto-pause: `SIGSTOP` the process when it empties and
wake it on the first inbound packet (palworld-server-docker does this with
NFLOG and a knockd fallback; itzg's stack models it as an explicit state
machine).

**palctl stops the server instead of suspending it**, and the two reasons are
worth stating because the difference is not cosmetic:

  1. *SIGSTOP keeps the socket open.* A suspended process still owns the UDP
     port, so the wake packet lands in its receive buffer where nothing can see
     it without root and packet capture. A stopped server releases the port,
     and palctl can simply listen on it — no NFLOG, no knockd, no privileges
     palctl does not already have.
  2. *A suspended process still holds its leaked memory.* palctl exists because
     Palworld leaks; freezing a 12 GB process leaves 12 GB frozen. Stopping it
     returns all of it, so the server that comes back is the one that has just
     started — which is the state the memory watchdog spends its life trying to
     get back to.

The cost is honest and belongs to the first player back: a stopped server takes
tens of seconds to load, where a suspended one resumes instantly. That is the
trade, and it is why this is off by default.

**Everything here fails safe toward running.** Any uncertainty — the player
count unknown, the API not answering, an operation in flight, a service state
palctl does not recognise — holds, and holding means the server stays up. A
half-working auto-pause is indistinguishable from a crashed server, and this
module's job is to make sure the ambiguity never arises.

The decision is pure, in the shape `supervisor.decide` established: the daemon
supplies an Observation, applies the Action, and owns the clock and counters.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from dataclasses import dataclass
from enum import StrEnum

# How long a server has to be empty before it is put away. Not a knob for
# "instantly": a server that empties for ninety seconds between two friends'
# sessions must not spend a minute restarting for the second one.
MIN_IDLE_SECONDS = 600

# After waking, palctl leaves the server alone for this long regardless of who
# is on it. Without it a player whose connection attempt woke the server, and
# who then takes forty seconds to load in, arrives at a server that has already
# decided it is empty again and put itself away.
WAKE_GRACE_SECONDS = 900


class Action(StrEnum):
    HOLD = "hold"          # do nothing
    PAUSE = "pause"        # save, then stop the server and start listening
    WAKE = "wake"          # someone knocked: start the server again


@dataclass(frozen=True)
class Observation:
    """Everything the decision is allowed to depend on."""

    enabled: bool
    # True once palctl has stopped the server itself and is listening for a
    # knock. Distinct from "the service is STOPPED", which is also true when an
    # admin stopped it — and those two must never be confused.
    paused: bool
    knocked: bool
    # None when palctl could not read the player list at all. Deliberately not
    # folded into 0: "nobody is on" and "I cannot tell" lead opposite ways.
    players: int | None
    alive: bool
    service_state: str
    operation: str | None
    # The admin's standing intent. A server stopped on purpose is not idle, and
    # must never be woken by a stray packet.
    desired_running: bool
    empty_seconds: float
    idle_after_seconds: int = MIN_IDLE_SECONDS
    since_wake_seconds: float = 1e9


@dataclass(frozen=True)
class Decision:
    action: Action
    why: str


def decide(obs: Observation) -> Decision:
    """What to do about an idle (or sleeping) server. Pure."""
    if not obs.enabled:
        return Decision(Action.HOLD, "auto-pause is off")

    # An admin's Stop outranks everything here, in both directions: palctl must
    # not put away a server that is already deliberately down (there is nothing
    # to save and nothing to gain), and must never wake one.
    if not obs.desired_running:
        return Decision(Action.HOLD, "the server is stopped on purpose")

    if obs.paused:
        if obs.knocked:
            return Decision(Action.WAKE, "somebody tried to connect")
        return Decision(Action.HOLD, "asleep, waiting for someone to connect")

    # Never act across another operation. A backup, update or restore holds the
    # server-operation lock, and stopping the server underneath one is how a
    # world gets copied mid-write.
    if obs.operation:
        return Decision(Action.HOLD, f"{obs.operation} is in progress")

    if obs.service_state not in ("RUNNING",):
        # Includes STOPPED, START_PENDING and UNKNOWN. Something else is going
        # on — starting, stopping, or unreadable — and none of those is an idle
        # server to put away.
        return Decision(Action.HOLD, f"the service is {obs.service_state or 'unknown'}")

    if not obs.alive:
        # The REST API is not answering. That is the watchdog's and
        # auto-recovery's business, and pausing here would hide a sick server
        # behind a deliberate-looking stop.
        return Decision(Action.HOLD, "the server's API isn't answering")

    if obs.players is None:
        return Decision(Action.HOLD, "palctl can't tell who is online")
    if obs.players > 0:
        return Decision(Action.HOLD, f"{obs.players} player(s) online")

    if obs.since_wake_seconds < WAKE_GRACE_SECONDS:
        # Someone knocked recently and may still be loading in.
        return Decision(Action.HOLD, "recently woken — giving players time to join")

    if obs.empty_seconds < obs.idle_after_seconds:
        left = int(obs.idle_after_seconds - obs.empty_seconds)
        return Decision(
            Action.HOLD,
            f"empty, but only for {int(obs.empty_seconds)}s ({left}s to go)",
        )

    return Decision(
        Action.PAUSE,
        f"nobody has been online for {int(obs.empty_seconds / 60)} minutes",
    )


class KnockListener:
    """Binds the game's UDP port while the server is away, and reports the
    first packet that arrives.

    This is the whole wake mechanism, and it is only possible because palctl
    *stops* the server rather than suspending it — a suspended process would
    still hold this port.

    It is deliberately dumb: any datagram counts as a knock. Reading Palworld's
    handshake to distinguish a real client from a port scan would be a parser
    for an undocumented protocol standing between players and their server, and
    the cost of a false wake is that an empty server runs for `MIN_IDLE_SECONDS`
    and puts itself away again.
    """

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None
        self.knocked = False
        self.error = ""

    @property
    def listening(self) -> bool:
        return self._transport is not None

    async def start(self, host: str, port: int) -> bool:
        """Begin listening. False (with `error` set) if the port can't be taken
        — which the caller must treat as a reason NOT to pause: a server put
        away with no way to wake it is a server nobody can reach."""
        if self._transport is not None:
            return True
        loop = asyncio.get_running_loop()
        listener = self

        class _Protocol(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr) -> None:  # noqa: ARG002
                listener.knocked = True

        try:
            transport, _ = await loop.create_datagram_endpoint(
                _Protocol, local_addr=(host, port)
            )
        except OSError as e:
            self.error = str(e)
            return False
        self._transport = transport
        self.knocked = False
        self.error = ""
        return True

    async def stop(self, host: str = "", port: int = 0, timeout: float = 5.0) -> bool:
        """Release the port, and — when told which one — confirm it is free.

        `transport.close()` is asynchronous: it schedules the close and returns,
        so the socket can still be held for an event-loop turn or two
        afterwards. Starting the server in that window hands it a port palctl
        has not let go of yet, and Palworld exits when it cannot bind — turning
        a sleeping server into a dead one, on the wake path, where nobody is
        watching.

        So this waits for the port to actually become bindable and returns
        whether it did. A caller that gets False must not start the server; it
        should say so and try again rather than produce that failure.
        """
        if self._transport is not None:
            with contextlib.suppress(Exception):
                self._transport.close()
            self._transport = None
        self.knocked = False
        if not port:
            # Nothing to confirm against; yield once so the close is processed.
            await asyncio.sleep(0)
            return True

        deadline = timeout
        while deadline > 0:
            await asyncio.sleep(0.05)
            deadline -= 0.05
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.bind((host or "0.0.0.0", port))
                return True
            except OSError:
                continue
            finally:
                probe.close()
        return False
