"""Signed pre-state installer acquisition; bounded synchronous journal, no scheduler."""
import os
from pathlib import Path
import re
import subprocess
import time

from .contracts import Conflict, ContractError, canonical, digest, identifier
from .controller_install import install
from .controller_release import bounded_file, verify_statement
from .filesystem import _durable_directory, _managed_path
from .filesystem import private_lock
from .release_trust import load_bundle
from .state_config import _config_home
from .filesystem import canonical_user_path
from .store import atomic_write


def download(url, limit, *, clock=time.monotonic):
    """The same bounded native transport as recovery; signatures remain authoritative."""
    from .release_http import fetch_metadata
    from .http_bounds import BoundedHTTPError
    try:
        return fetch_metadata(url,limit,clock=clock)
    except (Conflict,BoundedHTTPError) as exc:
        raise ContractError('release download exceeded its deadline or framing bound') from exc


def acquire_install(version, request_id, *, trust_bundle=None, cache_home=None, data_home=None,
                    config_home=None, fetch=download, run=subprocess.run, fault_hook=None):
    if not isinstance(version, str) or not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,63}', version):
        raise ContractError('invalid release version')
    identifier(request_id)
    trust = load_bundle(trust_bundle)
    bundle = trust['bundle']
    destination = canonical_user_path(Path(data_home or os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share').expanduser().resolve())
    base = _managed_path(Path(cache_home or os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') /
                         'quirkbench/releases')
    _durable_directory(base)
    stage = _managed_path(base / request_id); _durable_directory(stage)
    records = _managed_path(_config_home(config_home) / 'quirkbench/release-install' / request_id)
    _durable_directory(records)
    fault_hook = fault_hook or (lambda _: None)
    intent = {'schema_version': 1, 'version': version, 'request_id': request_id,
              'release_base_url': bundle['release_base_url'], 'trust_bundle_sha256': trust['bundle_sha256'],
              'data_home': str(destination), 'cache_root': str(base)}
    with private_lock(records / 'request.lock'):
        journal = records / 'intent.json'
        if journal.exists():
            if bounded_file(journal, 16384) != canonical(intent):
                raise Conflict('release installation request already has a different intent')
        else:
            atomic_write(journal, canonical(intent))
        fault_hook('intent_recorded')
        for name, limit in (('release.json', 16384), ('release.sig', 65536)):
            path = stage / name
            if not path.exists():
                raw = fetch(bundle['release_base_url'] + version + '/' + name, limit)
                if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
                    raise ContractError('invalid bounded release download')
                atomic_write(path, raw)
            fault_hook(name)
        statement_raw = bounded_file(stage / 'release.json', 16384)
        signature_raw = bounded_file(stage / 'release.sig', 65536)
        metadata = {'schema_version': 1, 'statement_sha256': digest(statement_raw), 'signature_sha256': digest(signature_raw)}
        accepted = records / 'metadata.json'
        if accepted.exists() and bounded_file(accepted, 4096) != canonical(metadata):
            raise Conflict('release request already accepted different statement/signature bytes')
        receipt = verify_statement(statement_raw, signature_raw, trust['public_key'], bundle['publisher_fingerprint'],
            download_directory=stage, run=run, expected_public_key_sha256=bundle['public_key_sha256'])
        statement = receipt['statement']
        if statement['controller_version'] != version:
            raise ContractError('signed release version differs from requested version')
        # Signed inputs are durable audit/readiness evidence, not optional cache.
        for name, raw in (('release.json', statement_raw), ('release.sig', signature_raw)):
            saved = records / name
            if saved.exists() and bounded_file(saved, 65536) != raw:
                raise Conflict('durable release evidence differs from accepted metadata')
            atomic_write(saved, raw)
        atomic_write(accepted, canonical(metadata))
        archive = stage / 'controller.tar.gz'
        if not archive.exists():
            raw = fetch(bundle['release_base_url'] + version + '/controller.tar.gz', 64 * 1024**2)
            if not isinstance(raw, bytes) or not 0 < len(raw) <= 64 * 1024**2:
                raise ContractError('invalid bounded controller download')
            if digest(raw) != statement['controller_archive_sha256']:
                raise ContractError('downloaded archive differs from signed release digest')
            atomic_write(archive, raw)
        fault_hook('controller_downloaded')
        # Keep the original publisher-authenticated bytes as required software
        # provenance. The runtime's local manifest is not a publisher signature.
        raw = bounded_file(archive, 64 * 1024**2)
        if digest(raw) != statement['controller_archive_sha256']:
            raise ContractError('controller archive differs from signed release digest')
        retained = _managed_path(destination / 'quirkbench/controller-archives')
        _durable_directory(retained)
        authenticated_archive = retained / (statement['controller_archive_sha256'] + '.tar.gz')
        if authenticated_archive.exists() or authenticated_archive.is_symlink():
            if bounded_file(authenticated_archive, 64 * 1024**2) != raw:
                raise Conflict('retained authenticated controller archive differs')
        else:
            atomic_write(authenticated_archive, raw)
        fault_hook('controller_retained')
        result = install(authenticated_archive, data_home=destination,
            expected_archive_sha256=statement['controller_archive_sha256'], expected_version=version)
        fault_hook('installed')
        result = {**result, 'request_id': request_id, 'distribution_verification': {
            **receipt, 'controller_archive_authenticated': True, 'other_release_assets_verified': False}}
        document = {'schema_version': 1, 'record_type': 'signed-release-installation',
                    'request_id': request_id, 'installation': result}
        atomic_write(records / 'result.json', canonical(document))
        atomic_write(records.parent.parent / 'signed-release-installation.json', canonical(document))
        return result
