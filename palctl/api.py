"""
Palworld dedicated server REST API client.

RCON is deprecated; this is Pocketpair's recommended admin interface, and it
gives us things RCON never could — real FPS, frametime, uptime, and per-player
level / location / ping / building count.

Auth is HTTP Basic with username `admin` and your AdminPassword.
Requires RESTAPIEnabled=True and RESTAPIPort in PalWorldSettings.ini.

Note: Pocketpair explicitly says these endpoints are NOT designed to be exposed
to the internet. We only ever talk to 127.0.0.1.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx


class PalApiError(RuntimeError):
    pass


class PalApiUnauthorized(PalApiError):
    pass


class PalApiUnreachable(PalApiError):
    pass


@dataclass(frozen=True)
class Metrics:
    server_fps: int
    current_players: int
    server_frame_time: float
    max_players: int
    uptime: int
    base_camps: int
    days: int

    @classmethod
    def from_json(cls, d: dict) -> Metrics:
        return cls(
            server_fps=int(d.get("serverfps", 0)),
            current_players=int(d.get("currentplayernum", 0)),
            server_frame_time=float(d.get("serverframetime", 0.0)),
            max_players=int(d.get("maxplayernum", 0)),
            uptime=int(d.get("uptime", 0)),
            base_camps=int(d.get("basecampnum", 0)),
            days=int(d.get("days", 0)),
        )


@dataclass(frozen=True)
class Player:
    name: str
    account_name: str
    player_id: str
    user_id: str
    ip: str
    ping: float
    location_x: float
    location_y: float
    level: int
    building_count: int

    @classmethod
    def from_json(cls, d: dict) -> Player:
        return cls(
            name=str(d.get("name", "")),
            account_name=str(d.get("accountName", "")),
            player_id=str(d.get("playerId", "")),
            user_id=str(d.get("userId", "")),
            ip=str(d.get("ip", "")),
            ping=float(d.get("ping", 0.0)),
            location_x=float(d.get("location_x", 0.0)),
            location_y=float(d.get("location_y", 0.0)),
            level=int(d.get("level", 0)),
            building_count=int(d.get("building_count", 0)),
        )


@dataclass(frozen=True)
class ServerInfo:
    version: str
    server_name: str
    description: str

    @classmethod
    def from_json(cls, d: dict) -> ServerInfo:
        return cls(
            version=str(d.get("version", "")),
            server_name=str(d.get("servername", "")),
            description=str(d.get("description", "")),
        )


class PalApi:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8212,
        password: str = "",
        timeout: float = 6.0,
    ) -> None:
        self._base = f"http://{host}:{port}/v1/api"
        self._auth = ("admin", password)
        self._timeout = timeout

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        url = f"{self._base}/{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.request(method, url, auth=self._auth, json=json)
        except httpx.RequestError as e:
            raise PalApiUnreachable(
                f"Can't reach the Palworld REST API at {url}. "
                "Is the server running, and is RESTAPIEnabled=True?"
            ) from e

        if r.status_code == 401:
            raise PalApiUnauthorized(
                "REST API rejected the password. It must match AdminPassword in "
                "PalWorldSettings.ini exactly."
            )
        if r.status_code >= 400:
            raise PalApiError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:200]}")

        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}

    # ---------- reads ----------

    async def info(self) -> ServerInfo:
        return ServerInfo.from_json(await self._request("GET", "info"))

    async def metrics(self) -> Metrics:
        return Metrics.from_json(await self._request("GET", "metrics"))

    async def players(self) -> list[Player]:
        data = await self._request("GET", "players")
        return [Player.from_json(p) for p in data.get("players", [])]

    async def settings(self) -> dict:
        """Live active settings, read-only. Useful for detecting ini/runtime drift."""
        return await self._request("GET", "settings")

    # ---------- writes ----------

    async def announce(self, message: str) -> None:
        # Unlike RCON's Broadcast, this takes real spaces. No underscore hack.
        await self._request("POST", "announce", {"message": message})

    async def kick(self, user_id: str, message: str = "Kicked by admin") -> None:
        await self._request("POST", "kick", {"userid": user_id, "message": message})

    async def ban(self, user_id: str, message: str = "Banned by admin") -> None:
        await self._request("POST", "ban", {"userid": user_id, "message": message})

    async def unban(self, user_id: str) -> None:
        await self._request("POST", "unban", {"userid": user_id})

    async def save(self) -> None:
        await self._request("POST", "save")

    async def shutdown(self, seconds: int = 60, message: str = "Server restarting") -> None:
        """Graceful: warns players in-game, counts down, then exits."""
        await self._request("POST", "shutdown", {"waittime": seconds, "message": message})

    async def force_stop(self) -> None:
        """Immediate. Does NOT save. Last resort."""
        await self._request("POST", "stop")

    # ---------- helpers ----------

    async def is_alive(self) -> bool:
        try:
            await self.metrics()
            return True
        except PalApiError:
            return False

    async def wait_until_alive(self, timeout: float = 180.0, interval: float = 3.0) -> bool:
        """Palworld takes ~60s+ after process start before the API answers."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if await self.is_alive():
                return True
            await asyncio.sleep(interval)
        return False
