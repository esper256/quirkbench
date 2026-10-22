"""SFTP over the existing authenticated candidate connection."""

from pathlib import Path
import asyncssh
from .files import disk, contained


class CandidateSFTP(asyncssh.SFTPServer):
    """Keep file writes off the connection's event loop."""

    def __init__(self, channel, *, chroot, evidence):
        super().__init__(channel, chroot=chroot)
        self.upload_root, self.evidence_root = Path(chroot), Path(evidence)

    def map_path(self, path):
        import os

        name = os.fsdecode(path).lstrip("/")
        root = self.upload_root
        if name.startswith("evidence/"):
            root, name = self.evidence_root, name[len("evidence/") :]
        try:
            return os.fsencode(contained(root, name))
        except ValueError as error:
            raise asyncssh.SFTPPermissionDenied(str(error)) from error

    def writable(self, path):
        # Uploads are single named files. Evidence is read-only through SFTP.
        name = path.lstrip(b"/")
        if not name or b"/" in name or name in (b".", b"..", b"evidence"):
            raise asyncssh.SFTPPermissionDenied(
                "Use a single upload file name; evidence is read-only."
            )

    async def open(self, path, pflags, attrs):
        if pflags != asyncssh.FXF_READ:
            self.writable(path)
        return await disk(super().open, path, pflags, attrs)

    async def setstat(self, *args):
        raise asyncssh.SFTPPermissionDenied("Changing file attributes is not supported.")

    fsetstat = lsetstat = mkdir = rmdir = symlink = link = setstat

    async def rename(self, oldpath, newpath):
        self.writable(oldpath)
        self.writable(newpath)
        return await disk(super().rename, oldpath, newpath)

    async def write(self, file_obj, offset, data):
        return await disk(super().write, file_obj, offset, data)

    async def read(self, file_obj, offset, size):
        return await disk(super().read, file_obj, offset, size)

    async def close(self, file_obj):
        return await disk(super().close, file_obj)

    async def fstat(self, file_obj):
        return await disk(super().fstat, file_obj)

    async def fsync(self, file_obj):
        return await disk(super().fsync, file_obj)

    async def realpath(self, path):
        await disk(self.map_path, path)
        return b"/" + path.lstrip(b"/")

    async def stat(self, path):
        return await disk(super().stat, path)

    async def lstat(self, path):
        return await disk(super().lstat, path)

    async def posix_rename(self, oldpath, newpath):
        self.writable(oldpath)
        self.writable(newpath)
        return await disk(super().posix_rename, oldpath, newpath)

    async def remove(self, path):
        self.writable(path)
        return await disk(super().remove, path)


async def upload(connection, source, name):
    import secrets

    temporary = ".pending-" + secrets.token_hex(12)
    async with connection.start_sftp_client() as client:
        stream = await disk(Path(source).open, "rb")
        try:
            async with client.open(temporary, "wb") as remote:
                while chunk := await disk(stream.read, 262144):
                    await remote.write(chunk)
                await remote.fsync()
            await client.posix_rename(temporary, name)
        finally:
            await disk(stream.close)
            try:
                await client.remove(temporary)
            except asyncssh.SFTPNoSuchFile:
                pass


async def download(connection, name, destination, check_connection):
    """Publish only a complete, unchanged file from the same candidate boot."""
    import hashlib
    import os
    import secrets

    remote_name = "evidence/" + name
    temporary = destination.parent / (".pending-" + secrets.token_hex(12))
    await disk(destination.parent.mkdir, parents=True, exist_ok=True)

    def identity(attrs):
        return attrs.size, attrs.mtime, attrs.mtime_ns, attrs.ctime, attrs.ctime_ns

    try:
        async with connection.start_sftp_client(sftp_version=4) as client:
            async with client.open(remote_name, "rb") as remote:
                before = await remote.stat()
                if before.mtime_ns is None:
                    raise ValueError(
                        "Target does not provide precise evidence modification times; update its target tools."
                    )
                digest, size = hashlib.sha256(), 0
                stream = await disk(temporary.open, "xb", on_cancel=lambda stream: stream.close())
                try:
                    while chunk := await remote.read(262144):
                        size += len(chunk)
                        if size > before.size:
                            raise ValueError(
                                "Evidence changed during download; finish the file before retrying."
                            )
                        await disk(stream.write, chunk)
                        digest.update(chunk)
                    if (
                        size != before.size
                        or identity(before) != identity(await remote.stat())
                        or identity(before) != identity(await client.stat(remote_name))
                    ):
                        raise ValueError(
                            "Evidence changed during download; finish the file before retrying."
                        )
                    await disk(stream.flush)
                    await disk(os.fsync, stream.fileno())
                finally:
                    await disk(stream.close)
        await check_connection()
        await disk(os.replace, temporary, destination)
        try:
            await check_connection()
        except Exception:
            await disk(destination.unlink, missing_ok=True)
            raise
        return dict(size=size, sha256=digest.hexdigest())
    finally:
        await disk(temporary.unlink, missing_ok=True)
