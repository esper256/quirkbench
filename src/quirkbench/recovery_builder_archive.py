"""Bounded read-only inspection of one retained x86-64 OCI builder archive."""
from __future__ import annotations

import hashlib
import json
import re
import tarfile

from .build import BuildError
from .contracts import ContractError
from .product_contracts import _pairs


BLOB = re.compile(r'sha256:([0-9a-f]{64})\Z')
MAX_MEMBERS = 512
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_JSON_BYTES = 1024 * 1024


def inspect_builder_archive(stream, expected_config: str, *, require_no_entrypoint=False, expected_manifest=None,
                            verify=lambda:None, consume=lambda size:None) -> None:
    """Require the locked archive to contain exactly the expected OCI image.

    Layers are hashed as stored; the archive is never extracted or executed.
    """
    if not isinstance(expected_config, str) or not BLOB.fullmatch(expected_config):
        raise BuildError('invalid derived builder config digest')
    if expected_manifest is not None and (not isinstance(expected_manifest,str) or not BLOB.fullmatch(expected_manifest)):
        raise BuildError('invalid pinned builder manifest digest')
    try:
        with tarfile.open(fileobj=stream, mode='r:') as archive:
            files = {}
            total = 0
            count = 0
            for member in archive:
                verify()
                count += 1
                if count > MAX_MEMBERS:
                    raise BuildError('builder OCI archive member count is invalid')
                name = member.name
                if member.isdir():
                    if name.rstrip('/') not in ('blobs', 'blobs/sha256'):
                        raise BuildError('builder OCI archive has an unexpected directory')
                    continue
                if (not member.isfile() or name in files
                        or name not in ('index.json', 'oci-layout')
                        and not re.fullmatch(r'blobs/sha256/[0-9a-f]{64}', name)):
                    raise BuildError('builder OCI archive has an unsafe member')
                if member.size < 0 or member.size > MAX_ARCHIVE_BYTES:
                    raise BuildError('builder OCI archive member exceeds bounds')
                total += member.size
                if total > MAX_ARCHIVE_BYTES:
                    raise BuildError('builder OCI archive payload exceeds bounds')
                files[name] = member
            if not count:
                raise BuildError('builder OCI archive member count is invalid')

            def content(name, limit):
                verify()
                member = files.get(name)
                if member is None or member.size > limit:
                    raise BuildError('builder OCI archive is missing a bounded member')
                handle = archive.extractfile(member)
                if handle is None:
                    raise BuildError('builder OCI archive member is unavailable')
                with handle:
                    raw = handle.read(limit + 1)
                consume(len(raw));verify()
                if len(raw) != member.size:
                    raise BuildError('builder OCI archive member is truncated')
                return raw

            def document(name):
                try:
                    return json.loads(content(name, MAX_JSON_BYTES), object_pairs_hook=_pairs)
                except (UnicodeError, ValueError, TypeError, ContractError) as exc:
                    raise BuildError('builder OCI archive metadata is invalid') from exc

            def blob(descriptor, *, limit=MAX_ARCHIVE_BYTES):
                if (not isinstance(descriptor, dict) or
                        not isinstance(descriptor.get('digest'), str)):
                    raise BuildError('builder OCI archive descriptor is invalid')
                match = BLOB.fullmatch(descriptor['digest'])
                size = descriptor.get('size')
                if match is None or type(size) is not int or not 0 <= size <= limit:
                    raise BuildError('builder OCI archive descriptor is invalid')
                name = 'blobs/sha256/' + match.group(1)
                member = files.get(name)
                if member is None or member.size != size:
                    raise BuildError('builder OCI archive blob is missing or has wrong size')
                handle = archive.extractfile(member)
                if handle is None:
                    raise BuildError('builder OCI archive blob is unavailable')
                hasher = hashlib.sha256()
                count = 0
                chunks = [] if size <= MAX_JSON_BYTES else None
                with handle:
                    while True:
                        verify()
                        chunk=handle.read(1024 * 1024)
                        consume(len(chunk));verify()
                        if not chunk:break
                        count += len(chunk)
                        if count > size:
                            raise BuildError('builder OCI archive blob exceeds descriptor size')
                        hasher.update(chunk)
                        if chunks is not None:
                            chunks.append(chunk)
                if count != size or hasher.hexdigest() != match.group(1):
                    raise BuildError('builder OCI archive blob digest differs')
                return b''.join(chunks) if chunks is not None else None

            if document('oci-layout') != {'imageLayoutVersion': '1.0.0'}:
                raise BuildError('builder OCI archive layout is unsupported')
            index = document('index.json')
            if (not isinstance(index, dict) or index.get('schemaVersion') != 2
                    or not isinstance(index.get('manifests'), list)
                    or len(index['manifests']) != 1):
                raise BuildError('builder OCI archive requires one image manifest')
            descriptor = index['manifests'][0]
            if expected_manifest is not None and (not isinstance(descriptor,dict) or descriptor.get('digest') != expected_manifest):
                raise BuildError('builder OCI manifest differs from pinned baseline')
            if (not isinstance(descriptor, dict) or
                    descriptor.get('mediaType') != 'application/vnd.oci.image.manifest.v1+json'):
                raise BuildError('builder OCI archive manifest media type is unsupported')
            try:
                manifest = json.loads(blob(descriptor, limit=MAX_JSON_BYTES),
                                      object_pairs_hook=_pairs)
            except (UnicodeError, ValueError, TypeError, ContractError) as exc:
                raise BuildError('builder OCI archive manifest is invalid') from exc
            if (not isinstance(manifest, dict) or manifest.get('schemaVersion') != 2
                    or not isinstance(manifest.get('config'), dict)
                    or manifest['config'].get('digest') != expected_config
                    or manifest['config'].get('mediaType') != 'application/vnd.oci.image.config.v1+json'
                    or not isinstance(manifest.get('layers'), list)
                    or not 1 <= len(manifest['layers']) <= 128):
                raise BuildError('builder OCI archive config differs from operation intent')
            try:
                config = json.loads(blob(manifest['config'], limit=MAX_JSON_BYTES),
                                    object_pairs_hook=_pairs)
            except (UnicodeError, ValueError, TypeError, ContractError) as exc:
                raise BuildError('builder OCI archive config is invalid') from exc
            if (not isinstance(config, dict) or config.get('architecture') != 'amd64'
                    or config.get('os') != 'linux'
                    or not isinstance(config.get('rootfs'), dict)
                    or not isinstance(config['rootfs'].get('diff_ids'), list)
                    or len(config['rootfs']['diff_ids']) != len(manifest['layers'])
                    or any(not isinstance(value, str) or not BLOB.fullmatch(value)
                           for value in config['rootfs']['diff_ids'])):
                raise BuildError('builder OCI archive config platform or layer count differs')
            if require_no_entrypoint and (not isinstance(config.get('config', {}), dict)
                    or config.get('config', {}).get('Entrypoint') not in (None, [])):
                raise BuildError('supported setup builder must have no OCI Entrypoint')
            for layer in manifest['layers']:
                if (not isinstance(layer, dict) or layer.get('mediaType') not in (
                        'application/vnd.oci.image.layer.v1.tar',
                        'application/vnd.oci.image.layer.v1.tar+gzip')):
                    raise BuildError('builder OCI archive layer media type is unsupported')
                blob(layer)
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise BuildError('builder OCI archive cannot be inspected') from exc
