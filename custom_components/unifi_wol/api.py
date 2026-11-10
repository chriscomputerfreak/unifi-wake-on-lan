"""UniFi Network API client and SSH based Wake-on-LAN sender.

The UniFi Network API has no Wake-on-LAN endpoint, so the magic packet is
sent by the gateway itself: we log in via SSH, find the gateway interface
that belongs to the client's network/VLAN and emit the packet there.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import ipaddress
import logging
import re
import shlex
import ssl
from typing import Any

import aiohttp
import asyncssh
from yarl import URL

_LOGGER = logging.getLogger(__name__)

API_TIMEOUT = aiohttp.ClientTimeout(total=20)
SSH_TIMEOUT = 15

_MAC_RE = re.compile(r"^[0-9a-f]{12}$")
_IFACE_RE = re.compile(
    r"^\d+:\s+(?P<name>[^\s@]+)(?:@\S+)?\s+inet\s+(?P<cidr>\d+\.\d+\.\d+\.\d+/\d+)"
)

# Tools that can send a raw/L2 magic packet on a given interface.
L2_TOOLS = ("etherwake", "ether-wake", "busybox-ether-wake", "python3")


class UnifiWolError(Exception):
    """Base error."""


class CannotConnect(UnifiWolError):
    """Controller or gateway not reachable."""


class InvalidAuth(UnifiWolError):
    """Credentials rejected."""


class HostKeyMismatch(UnifiWolError):
    """The gateway presented an unexpected SSH host key."""


class CertificateMismatch(UnifiWolError):
    """The controller presented an unexpected TLS certificate."""


class WakeFailed(UnifiWolError):
    """Sending the magic packet failed."""


def ssh_fingerprint(host_key: str) -> str:
    """Return the SHA256 fingerprint of an OpenSSH public key (like ssh-keygen -l)."""
    return str(asyncssh.import_public_key(host_key).get_fingerprint("sha256"))


def format_tls_fingerprint(fingerprint: str) -> str:
    """Format a hex SHA256 certificate fingerprint like browsers do (AB:CD:...)."""
    value = fingerprint.upper()
    return ":".join(value[i : i + 2] for i in range(0, len(value), 2))


async def async_fetch_tls_fingerprint(host: str) -> str:
    """Return the SHA256 fingerprint (hex) of the controller's TLS certificate."""
    url = URL(f"https://{host}")
    # No CA verification: this is used to pin self-signed certificates.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(url.host, url.port or 443, ssl=context),
            API_TIMEOUT.total,
        )
    except (OSError, ssl.SSLError, TimeoutError) as err:
        raise CannotConnect(str(err) or type(err).__name__) from err
    try:
        cert = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass
    if not cert:
        raise CannotConnect("The controller did not present a TLS certificate")
    return hashlib.sha256(cert).hexdigest()


def normalize_mac(mac: str) -> str:
    """Return a MAC address as lower case, colon separated string."""
    cleaned = re.sub(r"[^0-9a-fA-F]", "", mac or "").lower()
    if not _MAC_RE.match(cleaned):
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


def magic_packet(mac: str) -> bytes:
    """Build the 102 byte Wake-on-LAN payload."""
    return b"\xff" * 6 + bytes.fromhex(normalize_mac(mac).replace(":", "")) * 16


def _parse_ipv4_interface(value: Any) -> ipaddress.IPv4Interface | None:
    if not value:
        return None
    try:
        iface = ipaddress.ip_interface(value)
    except ValueError:
        return None
    return iface if isinstance(iface, ipaddress.IPv4Interface) else None


def _parse_ipv4(value: Any) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        return None


def _parse_wired(raw: dict[str, Any]) -> bool | None:
    """Wired/wireless flag of a client record (classic and v2 API)."""
    if isinstance(wired := raw.get("is_wired"), bool):
        return wired
    kind = str(raw.get("type") or "").upper()
    if kind in ("WIRED", "WIRELESS"):
        return kind == "WIRED"
    return None


@dataclass(frozen=True, slots=True)
class UnifiNetwork:
    """A UniFi LAN/VLAN network."""

    id: str
    name: str
    vlan: int | None
    subnet: ipaddress.IPv4Interface | None  # gateway address incl. prefix


@dataclass(frozen=True, slots=True)
class UnifiClient:
    """A (possibly offline) UniFi client."""

    mac: str
    name: str
    ip: str | None
    network_id: str | None
    online: bool
    wired: bool | None = None


@dataclass(frozen=True, slots=True)
class WakeResult:
    """Outcome of a wake request."""

    method: str
    interface: str


# --------------------------------------------------------------------------
# UniFi Network API (UniFi OS, e.g. UCG / UDM / UCG Fiber)
# --------------------------------------------------------------------------


class UnifiApi:
    """Minimal read-only client for the UniFi Network application."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        site: str,
        tls_fingerprint: str | None = None,
    ) -> None:
        """Initialize.

        With a TLS fingerprint the certificate is pinned instead of being
        verified against a CA (UniFi OS uses a self-signed certificate).
        """
        self._session = session
        self._base = f"https://{host}"
        self._username = username
        self._password = password
        self._site = site
        self._request_kwargs: dict[str, Any] = {"timeout": API_TIMEOUT}
        if tls_fingerprint:
            self._request_kwargs["ssl"] = aiohttp.Fingerprint(
                bytes.fromhex(tls_fingerprint)
            )

    async def login(self) -> None:
        """Log in to UniFi OS; the session cookie is kept in the cookie jar."""
        try:
            async with self._session.post(
                f"{self._base}/api/auth/login",
                json={
                    "username": self._username,
                    "password": self._password,
                    "rememberMe": True,
                },
                **self._request_kwargs,
            ) as resp:
                if resp.status in (400, 401, 403):
                    raise InvalidAuth(f"Login rejected (HTTP {resp.status})")
                if resp.status != 200:
                    raise CannotConnect(f"Login failed (HTTP {resp.status})")
        except aiohttp.ServerFingerprintMismatch as err:
            raise CertificateMismatch(str(err)) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err

    async def _get(self, path: str, *, retry: bool = True) -> Any:
        url = f"{self._base}/proxy/network{path}"
        try:
            async with self._session.get(url, **self._request_kwargs) as resp:
                if resp.status == 401:
                    if not retry:
                        raise InvalidAuth(f"GET {path}: HTTP 401")
                    payload = None
                elif resp.status != 200:
                    raise CannotConnect(f"GET {path} failed (HTTP {resp.status})")
                else:
                    payload = await resp.json(content_type=None)
        except aiohttp.ServerFingerprintMismatch as err:
            raise CertificateMismatch(str(err)) from err
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err

        if payload is None:  # session expired or not logged in yet
            await self.login()
            return await self._get(path, retry=False)
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    async def networks(self) -> dict[str, UnifiNetwork]:
        """Return all non-WAN networks keyed by id."""
        result: dict[str, UnifiNetwork] = {}
        for raw in await self._get(f"/api/s/{self._site}/rest/networkconf") or []:
            if not isinstance(raw, dict) or "_id" not in raw:
                continue
            if raw.get("purpose") == "wan":
                continue
            vlan: int | None = None
            if raw.get("vlan_enabled") and raw.get("vlan") not in (None, ""):
                try:
                    vlan = int(raw["vlan"])
                except (TypeError, ValueError):
                    vlan = None
            result[raw["_id"]] = UnifiNetwork(
                id=raw["_id"],
                name=raw.get("name") or raw["_id"],
                vlan=vlan,
                subnet=_parse_ipv4_interface(raw.get("ip_subnet")),
            )
        return result

    async def clients(self) -> dict[str, UnifiClient]:
        """Return known, historic and active clients keyed by MAC.

        Later sources override earlier ones: known clients < history < active.
        """
        clients: dict[str, UnifiClient] = {}

        def merge(
            raw: Any,
            ip_keys: tuple[str, ...],
            network_keys: tuple[str, ...],
            online: bool,
        ) -> None:
            if not isinstance(raw, dict):
                return
            try:
                mac = normalize_mac(raw.get("mac", ""))
            except ValueError:
                return
            old = clients.get(mac)
            alias = raw.get("name") or raw.get("display_name")
            name = (
                alias
                or (old.name if old and old.name != mac else None)
                or raw.get("hostname")
                or mac
            )
            ip = next(
                (v for k in ip_keys if (v := _parse_ipv4(raw.get(k)))), None
            ) or (old.ip if old else None)
            network_id = next(
                (raw[k] for k in network_keys if raw.get(k)), None
            ) or (old.network_id if old else None)
            wired = _parse_wired(raw)
            if wired is None and old:
                wired = old.wired
            clients[mac] = UnifiClient(mac, str(name), ip, network_id, online, wired)

        site = self._site
        for raw in await self._get(f"/api/s/{site}/rest/user") or []:
            merge(raw, ("fixed_ip", "last_ip"), ("network_id",), False)

        try:
            history = await self._get(
                f"/v2/api/site/{site}/clients/history"
                "?onlyNonBlocked=true&includeUnifiDevices=false&withinHours=0"
            )
        except CannotConnect as err:
            _LOGGER.debug("Client history not available: %s", err)
        else:
            for raw in history or []:
                merge(
                    raw,
                    ("last_ip", "ip"),
                    ("last_connection_network_id", "network_id"),
                    False,
                )

        for raw in await self._get(f"/api/s/{site}/stat/sta") or []:
            merge(raw, ("ip",), ("network_id",), True)

        return clients


# --------------------------------------------------------------------------
# Gateway SSH
# --------------------------------------------------------------------------

_PROBE_SCRIPT = (
    "for t in etherwake ether-wake python3 bash ip; do "
    'command -v "$t" >/dev/null 2>&1 && echo "tool=$t"; done; '
    "busybox --list 2>/dev/null | grep -qx ether-wake "
    '&& echo "tool=busybox-ether-wake"; '
    "exit 0"
)

# Sends an Ethernet frame with EtherType 0x0842 (Wake-on-LAN) to ff:ff:ff:ff:ff:ff
_PY_RAW = (
    "import socket,sys\n"
    "i,h=sys.argv[1],sys.argv[2]\n"
    "s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW)\n"
    "s.bind((i,0))\n"
    "src=bytes.fromhex(open('/sys/class/net/%s/address'%i).read().strip().replace(':',''))\n"
    "s.send(b'\\xff'*6+src+b'\\x08\\x42'+bytes.fromhex(h))\n"
)


def parse_interfaces(output: str) -> list[tuple[str, ipaddress.IPv4Interface]]:
    """Parse `ip -o -4 addr show` output."""
    result: list[tuple[str, ipaddress.IPv4Interface]] = []
    for line in output.splitlines():
        if match := _IFACE_RE.match(line.strip()):
            result.append((match["name"], ipaddress.IPv4Interface(match["cidr"])))
    return result


def resolve_interface(
    interfaces: list[tuple[str, ipaddress.IPv4Interface]],
    *,
    subnet: ipaddress.IPv4Interface | None,
    client_ip: str | None,
    override: str | None = None,
) -> tuple[str, ipaddress.IPv4Interface | None]:
    """Pick the gateway interface that serves the client's network."""
    if override:
        for name, addr in interfaces:
            if name == override:
                return name, addr
        return override, None

    if subnet is not None:
        for name, addr in interfaces:
            if addr.network == subnet.network:
                return name, addr

    if client_ip:
        ip = ipaddress.IPv4Address(client_ip)
        matches = [
            (name, addr)
            for name, addr in interfaces
            if ip in addr.network and not addr.ip.is_loopback
        ]
        if matches:
            return max(matches, key=lambda m: m[1].network.prefixlen)

    raise WakeFailed(
        "Could not determine the gateway interface for this client "
        f"(network subnet: {subnet}, last IP: {client_ip}). "
        "Pass the interface explicitly (e.g. br20)."
    )


def build_wake_script(mac: str, interface: str, ip: str | None) -> str:
    """Shell script run on the gateway; tries several ways to send the packet."""
    mac = normalize_mac(mac)
    packet = magic_packet(mac)
    octal = "".join(f"\\{b:03o}" for b in packet)
    q = shlex.quote
    return "\n".join(
        [
            f"IF={q(interface)}",
            f"MAC={q(mac)}",
            f"IP={q(ip or '')}",
            f"HEX={q(packet.hex())}",
            f"ESC={q(octal)}",
            f"PY={q(_PY_RAW)}",
            'have() { command -v "$1" >/dev/null 2>&1; }',
            'if have etherwake; then etherwake -i "$IF" "$MAC" '
            "&& { echo method=etherwake; exit 0; }; fi",
            'if have ether-wake; then ether-wake -i "$IF" "$MAC" '
            "&& { echo method=ether-wake; exit 0; }; fi",
            "if have busybox && busybox --list 2>/dev/null | grep -qx ether-wake; then "
            'busybox ether-wake -i "$IF" "$MAC" '
            "&& { echo method=busybox-ether-wake; exit 0; }; fi",
            'if have python3; then python3 -c "$PY" "$IF" "$HEX" '
            "&& { echo method=python3-raw; exit 0; }; fi",
            # Fallback: temporary static ARP entry -> unicast UDP to the sleeping
            # client's MAC; the switches flood it to the right port.
            'if [ -n "$IP" ] && have bash && have ip; then',
            '  if ip neigh replace "$IP" lladdr "$MAC" dev "$IF" nud permanent; then',
            "    bash -c 'printf \"$1\" > \"/dev/udp/$2/9\"' _ \"$ESC\" \"$IP\"; rc=$?",
            '    ip neigh del "$IP" dev "$IF" >/dev/null 2>&1',
            '    [ "$rc" -eq 0 ] && { echo method=static-neigh-udp; exit 0; }',
            "  fi",
            "fi",
            'echo "no usable wake-on-lan method found on the gateway" >&2',
            "exit 2",
        ]
    )


def has_wol_method(tools: list[str]) -> bool:
    """Whether the probed tool set allows sending a magic packet."""
    return any(t in tools for t in L2_TOOLS) or {"bash", "ip"} <= set(tools)


class GatewaySSH:
    """Runs commands on the UniFi gateway via SSH."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        host_key: str | None,
    ) -> None:
        """Initialize."""
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._host_key = host_key
        self._lock = asyncio.Lock()

    @staticmethod
    async def async_fetch_host_key(host: str, port: int) -> str:
        """Return the gateway's public host key in OpenSSH format."""
        try:
            key = await asyncio.wait_for(
                asyncssh.get_server_host_key(host, port, config=None), SSH_TIMEOUT
            )
        except (OSError, asyncssh.Error, TimeoutError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err
        if key is None:
            raise CannotConnect("The gateway did not present an SSH host key")
        return key.export_public_key().decode().strip()

    async def _connect(self) -> asyncssh.SSHClientConnection:
        known_hosts: Any = None
        if self._host_key:
            known_hosts = ([asyncssh.import_public_key(self._host_key)], [], [])
        try:
            return await asyncio.wait_for(
                asyncssh.connect(
                    self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    known_hosts=known_hosts,
                    client_keys=None,
                    agent_path=None,
                    config=None,
                    preferred_auth="password,keyboard-interactive",
                ),
                SSH_TIMEOUT,
            )
        except asyncssh.HostKeyNotVerifiable as err:
            raise HostKeyMismatch(str(err)) from err
        except asyncssh.PermissionDenied as err:
            raise InvalidAuth(str(err)) from err
        except (OSError, asyncssh.Error, TimeoutError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err

    @staticmethod
    async def _run(
        conn: asyncssh.SSHClientConnection, command: str
    ) -> asyncssh.SSHCompletedProcess:
        try:
            return await asyncio.wait_for(conn.run(command, check=False), SSH_TIMEOUT)
        except (OSError, asyncssh.Error, TimeoutError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err

    @staticmethod
    def _parse_tools(stdout: Any) -> list[str]:
        return [
            line.removeprefix("tool=")
            for line in str(stdout or "").splitlines()
            if line.startswith("tool=")
        ]

    async def async_probe(self) -> list[str]:
        """Return the WoL related tools available on the gateway."""
        conn = await self._connect()
        async with conn:
            result = await self._run(conn, _PROBE_SCRIPT)
        return self._parse_tools(result.stdout)

    async def async_diagnostics(self) -> dict[str, Any]:
        """Return available tools and IPv4 interfaces of the gateway."""
        async with self._lock:
            conn = await self._connect()
            async with conn:
                probe = await self._run(conn, _PROBE_SCRIPT)
                addr_out = await self._run(conn, "ip -o -4 addr show")
        return {
            "tools": self._parse_tools(probe.stdout),
            "interfaces": {
                name: str(addr) for name, addr in parse_interfaces(str(addr_out.stdout or ""))
            },
        }

    async def async_wake(
        self,
        mac: str,
        *,
        subnet: ipaddress.IPv4Interface | None,
        client_ip: str | None,
        interface: str | None = None,
    ) -> WakeResult:
        """Send the magic packet on the interface of the client's network."""
        mac = normalize_mac(mac)
        async with self._lock:
            conn = await self._connect()
            async with conn:
                addr_out = await self._run(conn, "ip -o -4 addr show")
                interfaces = parse_interfaces(str(addr_out.stdout or ""))
                iface, iface_addr = resolve_interface(
                    interfaces, subnet=subnet, client_ip=client_ip, override=interface
                )
                ip = client_ip
                if ip and (
                    iface_addr is None
                    or ipaddress.IPv4Address(ip) not in iface_addr.network
                ):
                    ip = None
                _LOGGER.debug("Waking %s via %s (ip hint %s)", mac, iface, ip)
                result = await self._run(conn, build_wake_script(mac, iface, ip))

        stdout = str(result.stdout or "")
        if result.exit_status != 0:
            stderr = str(result.stderr or "").strip()
            raise WakeFailed(stderr or f"Gateway script exited with {result.exit_status}")
        method = next(
            (
                line.removeprefix("method=")
                for line in stdout.splitlines()
                if line.startswith("method=")
            ),
            "unknown",
        )
        _LOGGER.info("Sent Wake-on-LAN to %s on %s using %s", mac, iface, method)
        return WakeResult(method=method, interface=iface)
