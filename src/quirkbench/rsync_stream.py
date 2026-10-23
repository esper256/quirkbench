"""On-demand rsync pipe over the existing authenticated HTTPS service.

Also executed as rsync's remote-shell helper. Never starts a daemon or opens a port.
"""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import ssl
import sys
import tempfile

import aiohttp
from aiohttp import web


async def stop_process(proc):
    """Reap the owned sender and its process group before releasing the source."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


async def serve_bundle(request, bundle, *, sender_args=None, on_complete=None):
    ws = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=30)
    await ws.prepare(request)
    proc = None
    tasks = []
    try:
        with tempfile.TemporaryFile() as errors:
            proc = await asyncio.create_subprocess_exec(
                "rsync",
                "--server",
                "--sender",
                *(sender_args or ["-rce.iLsfxCIvu", "--timeout=120"]),
                ".",
                str(bundle) + "/",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=errors,
                start_new_session=True,
            )

            async def receive():
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.BINARY:
                        raise ValueError("Expected rsync bytes.")
                    if not message.data:
                        proc.stdin.close()
                        # EOF on input is not evidence that the sender completed.
                        await proc.wait()
                        return
                    proc.stdin.write(message.data)
                    await proc.stdin.drain()
                raise ConnectionError("Rsync connection closed before completion.")

            async def send():
                while data := await proc.stdout.read(65536):
                    await ws.send_bytes(data)
                code = await proc.wait()
                if on_complete:
                    await on_complete()
                await ws.send_json({"exit_code": code})

            tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
            async with asyncio.timeout(3600):
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                await tasks[1]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if proc is not None:
            stopped = asyncio.create_task(stop_process(proc))
            while not stopped.done():
                try:
                    await asyncio.shield(stopped)
                except asyncio.CancelledError:
                    pass
            stopped.result()
        await ws.close()
    return ws


async def remote_shell(settings_dir, run_id, phase="bundle"):
    # Credentials are read from the existing settings, never argv or tool output.
    settings = json.loads((settings_dir / "connection.json").read_text())
    tls = ssl.create_default_context(cafile=str(settings_dir / "controller.crt"))
    url = settings["url"] + "/api/v1/runs/" + run_id + "/rsync?phase=" + phase
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    input_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    output_transport, output_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(output_transport, output_protocol, None, loop)
    sender = None
    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=tls),
            timeout=aiohttp.ClientTimeout(total=3600),
        ) as http:
            async with http.ws_connect(
                url,
                headers={"Authorization": "Bearer " + settings["token"]},
                max_msg_size=256 * 1024,
                heartbeat=30,
            ) as ws:

                async def send():
                    while data := await reader.read(65536):
                        await ws.send_bytes(data)
                    await ws.send_bytes(b"")

                sender = asyncio.create_task(send())
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        writer.write(message.data)
                        await writer.drain()
                    elif message.type == aiohttp.WSMsgType.TEXT:
                        code = json.loads(message.data)["exit_code"]
                        if type(code) is not int or not 0 <= code <= 255:
                            raise ValueError("Invalid rsync exit status.")
                        return code
                    else:
                        break
                raise ConnectionError("Rsync stream ended without a sender exit status.")
    finally:
        if sender:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        input_transport.close()
        output_transport.close()


if __name__ == "__main__":
    try:
        # The remaining arguments are rsync's generated remote command. The
        # controller never executes client-supplied commands or accepts a path.
        code = asyncio.run(remote_shell(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
    except Exception as error:
        print(f"Rsync HTTPS connection failed: {type(error).__name__}", file=sys.stderr)
        code = 1
    sys.exit(code)
