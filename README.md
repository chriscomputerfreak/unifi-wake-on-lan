# UniFi Wake-on-LAN for Home Assistant

[![Validate](https://github.com/chriscomputerfreak/unifi-wake-on-lan/actions/workflows/validate.yml/badge.svg)](https://github.com/chriscomputerfreak/unifi-wake-on-lan/actions/workflows/validate.yml)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

*Deutsche Anleitung: [README.de.md](README.de.md)*

Wake clients **on any VLAN**. Instead of sending the magic packet from Home Assistant (which
only reaches its own broadcast domain), this integration lets your UniFi gateway send it
directly on the interface of the network the client was last seen in.

Tested with a **UniFi Cloud Gateway Fiber (UCG Fiber)**. It is meant for any UniFi OS gateway
(UCG Ultra/Max, UDM, UDM Pro/SE, UXG…).

## How it works

1. The UniFi Network API (read only) provides clients (active and history) and networks
   (subnet/VLAN).
2. When you press a button, the integration connects to the gateway via SSH, reads
   `ip -o -4 addr show` and picks the interface that serves the client's network (e.g. `br20`).
3. The packet is sent with the first tool available on the gateway:
   `etherwake` → `ether-wake` → `busybox ether-wake` → `python3` (raw Ethernet frame,
   EtherType 0x0842). If none of these exist, it falls back to a temporary static ARP entry
   plus a UDP packet to port 9 via `bash`; the entry is removed right away.

The UniFi API has no Wake-on-LAN command, which is why SSH is required.

## Installation

### HACS

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=chriscomputerfreak&repository=unifi-wake-on-lan&category=integration)

Or add `https://github.com/chriscomputerfreak/unifi-wake-on-lan` as a custom repository
(type *Integration*), install **UniFi Wake-on-LAN** and restart Home Assistant.

### Manual

Copy `custom_components/unifi_wol` to `<config>/custom_components/` and restart Home Assistant.

## Configuration

Prerequisites on the gateway:

- A **local UniFi OS account** without 2FA. The *View Only* role is sufficient.
- **SSH enabled** in the UniFi OS console settings, with a password set. The user is `root`.

Then go to *Settings → Devices & services → Add integration → UniFi Wake-on-LAN*:

1. Controller: host (e.g. `192.168.1.1`), username, password and site (`default`).
2. SSH: port `22`, user `root` and the SSH password. The SSH host key is pinned on first
   connect.
3. Open **Configure** and select the clients that should get a **Wake** button.

## Usage

Each selected client gets a button entity. Its attributes show network, VLAN and last IP.
After the first press they also show the interface and method that were used.

The action also works for clients without a button:

```yaml
action: unifi_wol.wake
data:
  mac: "aa:bb:cc:dd:ee:ff"
  # interface: br20   # optional, overrides auto detection
response_variable: result   # {mac, interface, method}
```

## Troubleshooting

- Wake-on-LAN must be enabled in the BIOS/UEFI and in the network adapter. On Windows, also
  disable *Fast Startup*. Over Wi-Fi, WoL almost never works.
- A sleeping client is "offline" in UniFi. Its network and IP then come from the client history
  or from the data stored when you selected it.
- *Host key changed* (e.g. after a gateway reset): tick "Re-read and trust the SSH host key" in
  the options.
- Debug logging:
  ```yaml
  logger:
    logs:
      custom_components.unifi_wol: debug
  ```

## Security

The SSH password grants root access to your gateway. Like all integration credentials, it is
stored in `.storage/core.config_entries`. The integration only runs `ip -o -4 addr show` and
the wake script; all values are escaped with `shlex.quote`.

## Disclaimer

This project is not affiliated with or endorsed by Ubiquiti Inc. UniFi is a trademark of
Ubiquiti Inc.

## License

[MIT](LICENSE)
