"""Explicit file selection and transfer choices shared by both ends of rsync."""


def options(value=None):
    value = {} if value is None else value
    defaults = dict(
        mode="full",
        delete=False,
        exclude=[],
        checksum=False,
        ignore_times=False,
        dry_run=False,
        update_boot=True,
    )
    if not isinstance(value, dict) or value.keys() - defaults.keys():
        raise ValueError("Unknown rsync option.")
    result = defaults | value
    if result["mode"] not in ("full", "delta"):
        raise ValueError("rsync mode must be full or delta.")
    for key in ("delete", "checksum", "ignore_times", "dry_run", "update_boot"):
        if type(result[key]) is not bool:
            raise ValueError(key + " must be a boolean.")
    if not isinstance(result["exclude"], list) or any(
        not isinstance(pattern, str) or not pattern or "\0" in pattern
        for pattern in result["exclude"]
    ):
        raise ValueError("exclude must contain nonempty rsync patterns.")
    if result["dry_run"] and result["update_boot"]:
        raise ValueError("A dry run requires update_boot=false.")
    return result


def arguments(value):
    selected = options(value)
    args = [
        "-aHAX",
        "--numeric-ids",
        "--super",
        "--stats",
        "--itemize-changes",
        "--whole-file" if selected["mode"] == "full" else "--no-whole-file",
    ]
    for name in ("delete", "checksum", "ignore_times", "dry_run"):
        if selected[name] and not (name == "checksum" and selected["ignore_times"]):
            args.append("--" + name.replace("_", "-"))
    for pattern in selected["exclude"]:
        args.append("--exclude=" + pattern)
    return args


def sender_arguments(value):
    selected = options(value)
    # rsync's remote sender protocol flags for the supported receiver options.
    # No user-controlled shell command, source path or arbitrary server flag.
    short = "-" + ("n" if selected["dry_run"] else "") + "l"
    short += "W" if selected["mode"] == "full" else ""
    short += "HogDtpAXr"
    short += "c" if selected["checksum"] and not selected["ignore_times"] else ""
    short += "I" if selected["ignore_times"] else ""
    return [short + "e.iLsfxCIvu", "--numeric-ids", "--timeout=120"]
