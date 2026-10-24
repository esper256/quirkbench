"""Small controller-owned human requests, independent of diagnostic scripts."""

import hashlib
import time
from .errors import RequestRejected


def key(investigation, request_id):
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
        raise ValueError("Use a request ID of 1–200 characters.")
    return "human-" + hashlib.sha256((investigation + "\0" + request_id).encode()).hexdigest()[:24]


async def request(controller, investigation, request_id, action, message=None, chime=False):
    inv = await controller.store.get("investigation", investigation)
    identity = key(investigation, request_id)
    newly_ready = False
    async with controller.store.admission():
        existing = [
            r
            for r in await controller.store.find("human-request", investigation=investigation)
            if r["id"] == identity
        ]
        record = existing[0] if existing else None
        if action in ("prepare", "ready"):
            await controller.require_active(investigation)
            if not record:
                if (
                    not isinstance(message, str)
                    or not 1 <= len(message) <= 500
                    or not message.isprintable()
                ):
                    raise ValueError("Provide a short printable message (1–500 characters).")
                record = await controller.store.put(
                    "human-request",
                    identity,
                    investigation=investigation,
                    target=inv["target"],
                    request_id=request_id,
                    message=message,
                    status="prepared",
                    created_at=time.time(),
                )
            elif message is not None and message != record["message"]:
                raise ValueError("This request ID already has a different message. Use a new ID.")
            if action == "ready" and record["status"] == "prepared":
                target = controller.targets.get(inv["target"], {})
                if not target.get("boot_id"):
                    raise ValueError("Connect the target before marking the request ready.")
                others = await controller.store.find(
                    "human-request", target=inv["target"], status="ready"
                )
                if others:
                    raise ValueError("Finish or cancel the existing ready human request first.")
                controller.require_operation(inv["target"], "human-request")
                newly_ready = True
                record = await controller.store.change(
                    "human-request",
                    identity,
                    status="ready",
                    ready_at=time.time(),
                    boot_id=target["boot_id"],
                )
        elif action == "cancel" and record and record["status"] in ("prepared", "ready"):
            record = await controller.store.change(
                "human-request", identity, status="cancelled", cancelled_at=time.time()
            )
        elif action not in ("status", "cancel"):
            raise ValueError("Choose prepare, ready, status or cancel.")
        if not record:
            raise ValueError("Human request not found.")
    # No VT changes. A lost delivery never repeats a sound automatically.
    if action in ("ready", "cancel"):
        try:
            shown = await controller.rpc(
                inv["target"], "human-request", request=record, chime=chime and newly_ready
            )
            record = dict(record, delivery=shown)
        except (RequestRejected, OSError, TimeoutError) as error:
            record = dict(record, delivery_error=str(error))
    return record


async def respond(controller, identity, answer, *, target=None, boot_id=None):
    if answer not in ("done", "unable"):
        raise ValueError("Choose done or unable.")
    async with controller.store.admission():
        record = await controller.store.get("human-request", identity)
        if target is not None and target != record["target"]:
            raise ValueError("Human request belongs to another computer.")
        if record.get("response"):
            if record["response"] != answer:
                raise ValueError("A different response is already recorded.")
            return record
        current = controller.targets.get(record["target"], {})
        late = (
            record["status"] != "ready"
            or current.get("boot_id") != record.get("boot_id")
            or (boot_id is not None and boot_id != record.get("boot_id"))
        )
        record = await controller.store.change(
            "human-request",
            identity,
            response=answer,
            responded_at=time.time(),
            late=late,
            status=record["status"] if late else "answered",
        )

    try:
        await controller.rpc(record["target"], "human-request", request=record)
    except (RequestRejected, OSError, TimeoutError):
        pass  # The durable response is authoritative even if the display is offline.
    return record
