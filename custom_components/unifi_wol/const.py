"""Constants for the UniFi Wake-on-LAN integration."""

from datetime import timedelta

DOMAIN = "unifi_wol"

CONF_SITE = "site"
CONF_SSH_PORT = "ssh_port"
CONF_SSH_USERNAME = "ssh_username"
CONF_SSH_PASSWORD = "ssh_password"
CONF_SSH_HOST_KEY = "ssh_host_key"
CONF_TARGETS = "targets"
CONF_RETRUST_HOST_KEY = "retrust_host_key"

DEFAULT_SITE = "default"
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_USERNAME = "root"

UPDATE_INTERVAL = timedelta(minutes=5)

SERVICE_WAKE = "wake"
ATTR_MAC = "mac"
ATTR_INTERFACE = "interface"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

# Keys stored per target in the options
TARGET_NAME = "name"
TARGET_NETWORK_ID = "network_id"
TARGET_IP = "ip"
