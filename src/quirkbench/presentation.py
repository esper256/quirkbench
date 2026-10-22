"""Purpose-written terminal output; JSON responses keep their complete records."""

import json
from datetime import datetime


def size(value):
    return f"{value / 1024**3:.2f} GiB" if value >= 1024**3 else f"{value / 1024**2:.1f} MiB"


def image_label(image):
    when = image.get("built_at", "date unavailable")
    source = image.get("recovery_source", image.get("source", "unknown"))[:12]
    return f"{image.get('architecture', 'x86_64')} · {when} · source {source}"


def show(args, result):
    if set(result) == {"message"}:
        print(result["message"])
        return
    action = getattr(args, "action", None)
    if args.command == "recovery":
        if action == "list":
            if not result["images"]:
                print(
                    "No recovery images cached. Run quirkbench recovery download, or recovery build."
                )
            for image in result["images"]:
                labels = [
                    name
                    for key, name in (
                        ("release_default", "release default"),
                        ("latest_build", "latest build"),
                    )
                    if image[key]
                ]
                print(
                    f"{image_label(image)} · {size(image['bytes'])}"
                    + (f" [{', '.join(labels)}]" if labels else "")
                )
                print(f"  Exact image: {image['id']}")
            return
        if action in ("build", "download"):
            print("Recovery ready: " + image_label(result))
            print("Next: quirkbench recovery flash" + (" --built" if action == "build" else ""))
            return
    if args.command == "status" or (args.command == "controller" and action in ("status", "check")):
        print("Controller: " + ("running" if result["directory_in_use"] else "stopped"))
        print("Data: " + result["data_directory"])
        if action in ("status", "check"):
            installed = result["installed"]
            print(f"Installed: {installed['version']} · source {installed['source'][:12]}")
            if installed.get("source_label"):
                print("  " + installed["source_label"])
            if result["running"]:
                running = result["running"]
                print(f"Running:   {running['version']} · source {running['source'][:12]}")
                if running.get("source_label"):
                    print("  " + running["source_label"])
                if installed["source"] != running["source"]:
                    print(
                        "Software has changed. Stop and restart the controller when the investigation can be interrupted."
                    )
        for target in result.get("targets", []):
            print(f"Target {target['name']}: {target['mode']}")
        for work in result.get("work", []):
            print(f"Work: {work.get('stage', work.get('status', 'unknown'))}")
        return
    if args.command == "storage":
        if result.get("removed"):
            print("Cleanup complete.")
            return
        print("Storage on this controller")
        labels = {
            "cache": "Build cache",
            "investigations": "Investigations",
            "recovery_images": "Recovery images",
            "unassigned_diagnostics": "Unassigned diagnostics",
            "logs": "Controller and build logs",
        }
        for name, label in labels.items():
            print(f"{label:27} {size(result['bytes'][name]):>12}")
        if result.get("investigations"):
            print("Investigations:")
            for name in result["investigations"]:
                print(f"  {name}")
        for item in result.get("cleanup", []):
            print(f"  {item['path']} · {size(item['bytes'])}")
        return
    if args.command == "investigation" and action == "delete":
        print(
            ("Deleted investigation: " if result["removed"] else "Delete investigation: ")
            + result["investigation"]
        )
        if not result["removed"]:
            print(
                f"Runs and diagnostics: {result['runs']} · Unused software versions: {result['candidates_removed']}"
            )
        return
    if args.command == "investigation" and action in ("show", "pause", "resume", "force-stop"):
        context = result.get("context", result)
        print(f"{context['investigation']}: {context['investigation_status']}")
        print(f"Target: {context['target']} · {context['mode']}")
        for problem in context["problems"]:
            print(f"{problem['name']}: {problem['description']} [{problem['status']}]")
        if context.get("attention"):
            print("Needs attention: " + context["attention"])
        print(
            f"Runs: {context.get('run_count', len(result.get('runs', [])))}. Use --json for full records."
        )
        return
    if args.command == "investigation" and action == "wait":
        print(result.get("stage", result.get("status", "Waiting")))
        if result.get("log"):
            print("Log: " + result["log"])
        return
    if args.command == "investigation" and action == "timeline":
        for event in result["events"]:
            print(
                f"{datetime.fromtimestamp(event['at']).astimezone().isoformat(timespec='seconds')}  {event['stage']}"
            )
        if not result["events"]:
            print("No investigation activity recorded yet.")
        for work in result["durations"]:
            print(f"{work['action']}: {work['status']} · {work['seconds']:.1f} seconds")
        return
    if "message" in result:
        print(result["message"])
    if "directory" in result:
        print("Files: " + result["directory"])
    for key, label in (
        ("mcp_url", "Agent MCP"),
        ("mcp_token_file", "MCP token file"),
        ("next", "Next"),
    ):
        if result.get(key):
            print(f"{label}: {result[key]}")
    if not any(key in result for key in ("message", "next", "bytes", "directory")):
        print(json.dumps(result, indent=2))
