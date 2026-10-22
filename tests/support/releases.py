"""Tiny real recovery archives served over a local HTTP connection."""

from aiohttp import web

from quirkbench.files import digest_file, save_json
from quirkbench.recovery_images import FILES, release_archive


class RecoveryRelease:
    def __init__(self, root):
        self.root = root
        self.image = root / "original"
        self.image.mkdir(parents=True)
        for name in FILES:
            (self.image / name).write_bytes(bytes(range(256)) * 4)
        record = {
            "id": "a" * 24,
            "architecture": "x86_64",
            "files": {name: digest_file(self.image / name) for name in FILES},
            "source": "b" * 64,
            "built_at": "2026-01-01T00:00:00+00:00",
        }
        save_json(self.image / "image.json", record)
        self.selection = release_archive(self.image, "1.2.0", root / "release")
        self.archive = next((root / "release").glob("*.tar.gz"))
        self.requests = 0
        self.corrupt = False

    async def start(self, monkeypatch):
        async def respond(request):
            self.requests += 1
            return web.Response(body=b"broken" if self.corrupt else self.archive.read_bytes())

        app = web.Application()
        app.router.add_get("/recovery", respond)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.selection["url"] = f"http://127.0.0.1:{port}/recovery"
        monkeypatch.setattr("quirkbench.recovery_images.release_selection", lambda: self.selection)
        return self

    async def close(self):
        await self.runner.cleanup()

    def assert_files(self, root):
        for name in FILES:
            assert (root / "images" / self.selection["image"]["id"] / name).read_bytes() == (
                self.image / name
            ).read_bytes()
