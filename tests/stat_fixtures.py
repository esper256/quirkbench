"""Alter selected stat fields without replacing the rest of the native result."""
import os
import stat


def stat_with(info, **changes):
    # Native pickle arguments separate tuple fields from optional attributes.
    # Python 3.13 rejects tuple fields repeated in the attribute dictionary.
    native_values, attributes = info.__reduce__()[1]
    values = list(native_values)
    attributes = dict(attributes)
    for name, value in changes.items():
        index = getattr(stat, name.upper(), None)
        if index is not None:
            values[index] = value
        if index is None or name in attributes:
            attributes[name] = value
    # The second argument preserves optional attributes such as st_rdev and
    # nanosecond timestamps; rebuilding only the tuple silently loses them.
    return os.stat_result(values, attributes)
