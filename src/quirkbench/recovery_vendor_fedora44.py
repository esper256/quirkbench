"""Compatibility names for the historical Fedora44 reviewed inventory."""
from .recovery_vendor import bundled_inventory
_inventory = bundled_inventory('44')
FEDORA44_ENABLED_LINKS = _inventory['enabled_links']
FEDORA44_GENERATORS = _inventory['generators']
FEDORA44_ETC_LINKS = _inventory['etc_links']
