"""Alter selected stat fields without replacing the rest of the native result."""
import os
import stat


def stat_with(info, **changes):
    values = list(info)
    attributes = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
    attributes.update(changes)
    for name, value in changes.items():
        index = getattr(stat, name.upper(), None)
        if index is not None:
            values[index] = value
    # The second argument preserves optional attributes such as st_rdev and
    # nanosecond timestamps; rebuilding only the tuple silently loses them.
    return os.stat_result(values, attributes)
