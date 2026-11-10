"""Tests for unifi_wol."""

import asyncio
import datetime
import hashlib
import ipaddress
import ssl
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import asyncssh
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from custom_components.unifi_wol import api
from custom_components.unifi_wol.api import (
    UnifiApi,
    UnifiClient,
    UnifiNetwork,
    WakeResult,
    build_wake_script,
    magic_packet,
    normalize_mac,
    parse_interfaces,
    resolve_interface,
)
from custom_components.unifi_wol.const import DOMAIN

IP_ADDR = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever
5: br0    inet 192.168.1.1/24 brd 192.168.1.255 scope global br0\\       valid_lft forever
9: br20    inet 192.168.20.1/24 brd 192.168.20.255 scope global br20\\       valid_lft forever
11: eth4.30@eth4    inet 10.30.0.1/16 scope global eth4.30
12: ppp0    inet 84.1.2.3/32 scope global ppp0
"""

NETWORKS = {
    "n0": UnifiNetwork("n0", "Default", None, ipaddress.IPv4Interface("192.168.1.1/24")),
    "n20": UnifiNetwork("n20", "Gaming", 20, ipaddress.IPv4Interface("192.168.20.1/24")),
}
CLIENTS = {
    "aa:bb:cc:dd:ee:ff": UnifiClient("aa:bb:cc:dd:ee:ff", "Gaming-PC", "192.168.20.50", "n20", False, True),
    "11:22:33:44:55:66": UnifiClient("11:22:33:44:55:66", "NAS", "192.168.1.10", "n0", True),
    "22:22:22:22:22:22": UnifiClient("22:22:22:22:22:22", "Laptop", "192.168.1.20", "n0", True, False),
}

HOST_KEY = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()
NEW_HOST_KEY = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()
TLS_FP = "ab" * 32
NEW_TLS_FP = "cd" * 32


def test_pure_helpers() -> None:
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    with pytest.raises(ValueError):
        normalize_mac("xx")
    pkt = magic_packet("aa:bb:cc:dd:ee:ff")
    assert len(pkt) == 102 and pkt[:6] == b"\xff" * 6

    ifaces = parse_interfaces(IP_ADDR)
    assert [n for n, _ in ifaces] == ["lo", "br0", "br20", "eth4.30", "ppp0"]
    assert resolve_interface(ifaces, subnet=NETWORKS["n20"].subnet, client_ip=None)[0] == "br20"
    assert resolve_interface(ifaces, subnet=None, client_ip="10.30.5.5")[0] == "eth4.30"
    assert resolve_interface(ifaces, subnet=None, client_ip=None, override="br99") == ("br99", None)
    with pytest.raises(api.WakeFailed):
        resolve_interface(ifaces, subnet=None, client_ip="172.16.0.1")


def test_script_syntax_and_printf_bytes(tmp_path) -> None:
    script = build_wake_script("aa:bb:cc:dd:ee:ff", "br20", "192.168.20.50")
    subprocess.run(["sh", "-n"], input=script.encode(), check=True)
    subprocess.run(["bash", "-n"], input=script.encode(), check=True)
    # printf fallback must emit exactly the magic packet
    esc = "".join(f"\\{b:03o}" for b in magic_packet("aa:bb:cc:dd:ee:ff"))
    out = subprocess.run(["bash", "-c", 'printf "$1"', "_", esc], capture_output=True, check=True).stdout
    assert out == magic_packet("aa:bb:cc:dd:ee:ff")
    # embedded python is valid
    compile(api._PY_RAW, 'py', 'exec')
    assert "PY='import socket" in script


def test_script_falls_back_to_neigh(tmp_path) -> None:
    """Simulate a gateway without etherwake/python: fake `ip` and `bash`."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "log"
    for tool in ("ip", "bash"):
        f = bindir / tool
        f.write_text(f'#!/bin/sh\nprintf "%s\\n" "{tool} $*" >> {log}\nexit 0\n')
        f.chmod(0o755)
    script = build_wake_script("aa:bb:cc:dd:ee:ff", "br20", "192.168.20.50")
    res = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True,
                         env={"PATH": str(bindir)})
    # command -v needs only PATH; grep/busybox absent -> neigh path
    assert res.returncode == 0, res.stderr
    assert "method=static-neigh-udp" in res.stdout
    lines = log.read_text().splitlines()
    assert lines[0] == "ip neigh replace 192.168.20.50 lladdr aa:bb:cc:dd:ee:ff dev br20 nud permanent"
    assert lines[1].startswith("bash -c")
    assert lines[2] == "ip neigh del 192.168.20.50 dev br20"


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations):
    yield


@pytest.fixture
def mocks():
    with (
        patch("custom_components.unifi_wol.api.UnifiApi.login", AsyncMock()),
        patch("custom_components.unifi_wol.api.UnifiApi.networks", AsyncMock(return_value=NETWORKS)),
        patch("custom_components.unifi_wol.api.UnifiApi.clients", AsyncMock(return_value=CLIENTS)),
        patch("custom_components.unifi_wol.api.GatewaySSH.async_fetch_host_key", AsyncMock(return_value=HOST_KEY)),
        patch("custom_components.unifi_wol.config_flow.async_fetch_tls_fingerprint", AsyncMock(return_value=TLS_FP)),
        patch("custom_components.unifi_wol.async_fetch_tls_fingerprint", AsyncMock(return_value=TLS_FP)),
        patch("custom_components.unifi_wol.api.GatewaySSH.async_probe", AsyncMock(return_value=["bash", "ip"])),
        patch("custom_components.unifi_wol.api.GatewaySSH.async_wake", AsyncMock(return_value=WakeResult("etherwake", "br20"))) as wake,
    ):
        yield wake


async def test_full_flow(hass: HomeAssistant, mocks) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "host": "192.168.1.1", "username": "ha", "password": "pw", "site": "default", "verify_ssl": False})
    assert result["step_id"] == "ssh", result
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "ssh_port": 22, "ssh_username": "root", "ssh_password": "sshpw"})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["ssh_host_key"] == HOST_KEY
    assert entry.data["tls_fingerprint"] == TLS_FP

    # options: pick gaming pc
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert not result["errors"]
    labels = {o["value"]: o["label"] for o in result["data_schema"].schema["targets"].config["options"]}
    assert "📶" in labels["22:22:22:22:22:22"] and "📶" not in labels["aa:bb:cc:dd:ee:ff"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"targets": ["aa:bb:cc:dd:ee:ff"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.state is ConfigEntryState.LOADED

    ent_reg = er.async_get(hass)
    buttons = [e for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id) if e.domain == "button"]
    assert len(buttons) == 1
    entity_id = buttons[0].entity_id
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    assert state.attributes["network"] == "Gaming" and state.attributes["vlan"] == 20
    assert state.attributes["wired"] is True
    print(entity_id, dict(state.attributes))

    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    kwargs = mocks.await_args.kwargs
    assert mocks.await_args.args == ("aa:bb:cc:dd:ee:ff",)
    assert kwargs["subnet"] == NETWORKS["n20"].subnet and kwargs["client_ip"] == "192.168.20.50"
    assert hass.states.get(entity_id).attributes["last_interface"] == "br20"

    resp = await hass.services.async_call(DOMAIN, "wake", {"mac": "AA-BB-CC-DD-EE-FF"}, blocking=True, return_response=True)
    assert resp == {"mac": "aa:bb:cc:dd:ee:ff", "interface": "br20", "method": "etherwake"}

    mocks.side_effect = api.WakeFailed("boom")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "wake", {"mac": "aa:bb:cc:dd:ee:ff"}, blocking=True)

    # deselect -> button removed
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"targets": []})
    await hass.async_block_till_done()
    assert not [e for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id) if e.domain == "button"]

    assert await hass.config_entries.async_unload(entry.entry_id)


def _entry(hass: HomeAssistant, **data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="192.168.1.1_default", title="UniFi WoL (192.168.1.1)", data={
        "host": "192.168.1.1", "username": "ha", "password": "pw", "site": "default", "verify_ssl": False,
        "ssh_port": 22, "ssh_username": "root", "ssh_password": "x", "ssh_host_key": HOST_KEY,
        "tls_fingerprint": TLS_FP, **data},
        options={"targets": {"aa:bb:cc:dd:ee:ff": {"name": "PC", "network_id": "n20", "ip": "192.168.20.50"}}})
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_survives_controller_down(hass: HomeAssistant, mocks) -> None:
    entry = _entry(hass)
    with patch("custom_components.unifi_wol.api.UnifiApi.networks", AsyncMock(side_effect=api.CannotConnect("down"))):
        await _setup(hass, entry)
        await hass.services.async_call(DOMAIN, "wake", {"mac": "aa:bb:cc:dd:ee:ff"}, blocking=True)
        # without live data the stored IP is used as a hint
        assert mocks.await_args.kwargs["client_ip"] == "192.168.20.50"

        # options still work with the stored clients only
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["errors"] == {"base": "controller_unavailable"}
        labels = [o["label"] for o in result["data_schema"].schema["targets"].config["options"]]
        assert labels == ["? PC – aa:bb:cc:dd:ee:ff"]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"targets": ["aa:bb:cc:dd:ee:ff"]})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options["targets"]["aa:bb:cc:dd:ee:ff"]["ip"] == "192.168.20.50"


async def test_old_entry_gets_certificate_pinned(hass: HomeAssistant, mocks) -> None:
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={k: v for k, v in entry.data.items() if k != "tls_fingerprint"})
    await _setup(hass, entry)
    assert entry.data["tls_fingerprint"] == TLS_FP


async def test_reauth(hass: HomeAssistant, mocks) -> None:
    entry = _entry(hass)
    # controller rejects the credentials -> reauth flow is started
    with patch("custom_components.unifi_wol.api.UnifiApi.networks", AsyncMock(side_effect=api.InvalidAuth("no"))):
        await _setup(hass, entry)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    flow = flows[0]

    with patch("custom_components.unifi_wol.config_flow.GatewaySSH.async_probe",
               AsyncMock(side_effect=api.InvalidAuth("ssh"))):
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"password": "new"})
    assert result["errors"] == {"base": "invalid_auth_ssh"}

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"password": "new", "ssh_password": "newssh"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new" and entry.data["ssh_password"] == "newssh"
    assert entry.data["ssh_host_key"] == HOST_KEY

    # an SSH auth failure while waking also starts a reauth flow
    await hass.async_block_till_done()
    mocks.side_effect = api.InvalidAuth("ssh")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "wake", {"mac": "aa:bb:cc:dd:ee:ff"}, blocking=True)
    await hass.async_block_till_done()
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == ["reauth"]


async def test_reconfigure(hass: HomeAssistant, mocks) -> None:
    entry = _entry(hass)
    await _setup(hass, entry)
    ent_reg = er.async_get(hass)
    button = er.async_entries_for_config_entry(ent_reg, entry.entry_id)[0]
    assert button.unique_id == "192.168.1.1_default_aa:bb:cc:dd:ee:ff_wake"

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    # passwords are never prefilled
    suggested = {str(k): k.description for k in result["data_schema"].schema}
    assert suggested["password"] is None and suggested["host"] == {"suggested_value": "192.168.1.1"}

    with (
        patch("custom_components.unifi_wol.api.GatewaySSH.async_fetch_host_key", AsyncMock(return_value=NEW_HOST_KEY)),
        patch("custom_components.unifi_wol.config_flow.async_fetch_tls_fingerprint", AsyncMock(return_value=NEW_TLS_FP)),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "host": "10.0.0.1", "username": "ha", "site": "default", "verify_ssl": False,
            "ssh_port": 22, "ssh_username": "root"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.unique_id == "10.0.0.1_default"
    assert entry.title == "UniFi WoL (10.0.0.1)"
    assert entry.data["password"] == "pw" and entry.data["ssh_password"] == "x"
    assert entry.data["ssh_host_key"] == NEW_HOST_KEY and entry.data["tls_fingerprint"] == NEW_TLS_FP
    assert entry.options["targets"]
    migrated = ent_reg.async_get(button.entity_id)
    assert migrated.unique_id == "10.0.0.1_default_aa:bb:cc:dd:ee:ff_wake"
    assert entry.state is ConfigEntryState.LOADED

    # same host: a changed certificate is not silently accepted
    result = await entry.start_reconfigure_flow(hass)
    with patch("custom_components.unifi_wol.api.UnifiApi.login", AsyncMock(side_effect=api.CertificateMismatch("x"))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "host": "10.0.0.1", "username": "ha", "site": "default", "verify_ssl": False,
            "ssh_port": 22, "ssh_username": "root"})
    assert result["errors"] == {"base": "certificate_mismatch"}


async def test_retrust_via_options(hass: HomeAssistant, mocks) -> None:
    entry = _entry(hass)
    await _setup(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with (
        patch("custom_components.unifi_wol.api.GatewaySSH.async_fetch_host_key", AsyncMock(return_value=NEW_HOST_KEY)),
        patch("custom_components.unifi_wol.trust.async_fetch_tls_fingerprint", AsyncMock(return_value=NEW_TLS_FP)),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"targets": ["aa:bb:cc:dd:ee:ff"], "retrust_host_key": True})
    assert result["step_id"] == "trust"
    ph = result["description_placeholders"]
    assert ph["ssh_old"] == api.ssh_fingerprint(HOST_KEY) and ph["ssh_new"] == api.ssh_fingerprint(NEW_HOST_KEY)
    assert ph["ssh_old"].startswith("SHA256:")
    assert ph["tls_old"].startswith("AB:AB:") and ph["tls_new"].startswith("CD:CD:")
    # nothing stored before confirmation
    assert entry.data["ssh_host_key"] == HOST_KEY
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data["ssh_host_key"] == NEW_HOST_KEY and entry.data["tls_fingerprint"] == NEW_TLS_FP
    assert entry.state is ConfigEntryState.LOADED


async def test_repair_issue_and_fix_flow(hass: HomeAssistant, mocks) -> None:
    from custom_components.unifi_wol.repairs import async_create_fix_flow

    entry = _entry(hass)
    await _setup(hass, entry)
    issue_reg = ir.async_get(hass)
    issue_id = f"fingerprint_changed_{entry.entry_id}"

    mocks.side_effect = api.HostKeyMismatch("changed")
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, "wake", {"mac": "aa:bb:cc:dd:ee:ff"}, blocking=True)
    assert err.value.translation_key == "host_key_mismatch"
    issue = issue_reg.async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable

    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    with (
        patch("custom_components.unifi_wol.api.GatewaySSH.async_fetch_host_key", AsyncMock(return_value=NEW_HOST_KEY)),
        patch("custom_components.unifi_wol.trust.async_fetch_tls_fingerprint", AsyncMock(return_value=TLS_FP)),
    ):
        result = await flow.async_step_init()
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["tls_old"] == result["description_placeholders"]["tls_new"]
    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data["ssh_host_key"] == NEW_HOST_KEY
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

    # a changed controller certificate raises the same issue
    with patch("custom_components.unifi_wol.api.UnifiApi.networks",
               AsyncMock(side_effect=api.CertificateMismatch("x"))):
        await entry.runtime_data.async_refresh()
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_remove(entry.entry_id)
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None


async def test_diagnostics(hass: HomeAssistant, mocks) -> None:
    from custom_components.unifi_wol.diagnostics import async_get_config_entry_diagnostics

    entry = _entry(hass)
    await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "wake", {"mac": "aa:bb:cc:dd:ee:ff"}, blocking=True)
    with patch("custom_components.unifi_wol.api.GatewaySSH.async_diagnostics",
               AsyncMock(return_value={"tools": ["etherwake"], "interfaces": {"br20": "192.168.20.1/24"}})):
        diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    for secret in ("'pw'", "'x'", "'ha'", "aa:bb:cc:dd:ee:ff", "192.168.20.50", "Gaming-PC", HOST_KEY):
        assert secret not in text, secret
    assert diag["entry"]["data"]["ssh_host_key_fingerprint"] == api.ssh_fingerprint(HOST_KEY)
    target = diag["targets"]["target_1"]
    assert target["mac"] == "aa:bb:cc:**:**:**" and target["ip"] == "192.168.20.x"
    assert target["network"] == {"vlan": 20, "subnet": "192.168.20.1/24"}
    assert target["last_wake"]["interface"] == "br20"
    assert diag["gateway"]["tools"] == ["etherwake"]

    with patch("custom_components.unifi_wol.api.GatewaySSH.async_diagnostics",
               AsyncMock(side_effect=api.CannotConnect("down"))):
        diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["gateway"] == {"error": "CannotConnect: down"}


def test_wired_flag_and_fingerprint_format() -> None:
    assert api._parse_wired({"is_wired": False}) is False
    assert api._parse_wired({"type": "WIRED"}) is True
    assert api._parse_wired({}) is None
    assert api.format_tls_fingerprint("ab01") == "AB:01"


async def test_api_maps_certificate_mismatch() -> None:
    session = MagicMock()
    session.get.side_effect = aiohttp.ServerFingerprintMismatch(b"a", b"b", "h", 443)
    client = UnifiApi(session, "192.168.1.1", "u", "p", "default", TLS_FP)
    with pytest.raises(api.CertificateMismatch):
        await client.networks()
    assert isinstance(session.get.call_args.kwargs["ssl"], aiohttp.Fingerprint)


async def test_fetch_tls_fingerprint(socket_enabled, tmp_path) -> None:
    """Pin the certificate of a real local TLS server."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unifi")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")

    async def handle(reader, writer):
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        fp = await api.async_fetch_tls_fingerprint(f"127.0.0.1:{port}")
    finally:
        server.close()
        await server.wait_closed()
    assert fp == hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()

    with pytest.raises(api.CannotConnect):
        await api.async_fetch_tls_fingerprint(f"127.0.0.1:{port}")
