"""Tests for unifi_wol."""

import ipaddress
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.unifi_wol import api
from custom_components.unifi_wol.api import (
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
    "aa:bb:cc:dd:ee:ff": UnifiClient("aa:bb:cc:dd:ee:ff", "Gaming-PC", "192.168.20.50", "n20", False),
    "11:22:33:44:55:66": UnifiClient("11:22:33:44:55:66", "NAS", "192.168.1.10", "n0", True),
}


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
        f.write_text(f'#!/bin/sh\necho "{tool} $*" >> {log}\nexit 0\n')
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
        patch("custom_components.unifi_wol.api.GatewaySSH.async_fetch_host_key", AsyncMock(return_value="ssh-ed25519 AAAA")),
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
    assert entry.data["ssh_host_key"] == "ssh-ed25519 AAAA"

    # options: pick gaming pc
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
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


async def test_setup_survives_controller_down(hass: HomeAssistant, mocks) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, unique_id="192.168.1.1_default", data={
        "host": "192.168.1.1", "username": "ha", "password": "pw", "site": "default", "verify_ssl": False,
        "ssh_port": 22, "ssh_username": "root", "ssh_password": "x", "ssh_host_key": "k"},
        options={"targets": {"aa:bb:cc:dd:ee:ff": {"name": "PC", "network_id": "n20", "ip": "192.168.20.50"}}})
    entry.add_to_hass(hass)
    with patch("custom_components.unifi_wol.api.UnifiApi.networks", AsyncMock(side_effect=api.CannotConnect("down"))):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    await hass.services.async_call(DOMAIN, "wake", {"mac": "aa:bb:cc:dd:ee:ff"}, blocking=True)
    # without live data the stored IP is used as a hint
    assert mocks.await_args.kwargs["client_ip"] == "192.168.20.50"
