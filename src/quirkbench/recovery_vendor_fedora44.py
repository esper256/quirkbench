"""Compatibility names for the historical Fedora44 reviewed inventory."""
from .recovery_vendor import bundled_inventory
from .contracts import canonical,digest
from .build import BuildError
_inventory = bundled_inventory('44')
# Legacy profile-v1 has no inventory reference: preserve its reviewed byte identity.
if digest(canonical(_inventory)) != 'f0093b2c62284e1506e026c012e8cedacb9dd97d6ee10b766811812b9f92fd8c':
    raise BuildError('historical Fedora44 vendor inventory changed')
FEDORA44_ENABLED_LINKS = _inventory['enabled_links']
FEDORA44_GENERATORS = _inventory['generators']
FEDORA44_ETC_LINKS = _inventory['etc_links']
