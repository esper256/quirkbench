"""Read-only verification of acquired signed archive provenance."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from .contracts import Conflict, ContractError, canonical, digest, identifier, sha256
from .controller_install import _verified_archive, installation_record
from .controller_release import bounded_file, verify_statement
from .filesystem import _managed_path
from .product_contracts import _depth, _pairs
from .release_trust import load_bundle, ReleaseUnavailable
from .state_config import _config_home


def _document(path, fields, limit=65536):
    raw = bounded_file(path, limit)
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ContractError('nonfinite installed release JSON')))
        _depth(value)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError('invalid installed release JSON') from exc
    if not isinstance(value, dict) or set(value) != fields or raw != canonical(value):
        raise ContractError('invalid installed release record fields/canonical bytes')
    return value


def verify_request(request_id, *, config_home=None, trust_bundle=None, run=subprocess.run):
    """Current publisher trust and captured signed archive provenance.

    No cache dependency, state initialization, download, activation or qualification.
    Explicit trust injection never updates the packaged production trust configuration.
    """
    identifier(request_id)
    records = _managed_path(_config_home(config_home) / 'quirkbench/release-install' / request_id)
    intent = _document(records / 'intent.json', {'schema_version','version','request_id','release_base_url',
        'trust_bundle_sha256'}, limit=16384)
    if (type(intent['schema_version']) is not int or intent['schema_version'] != 1
            or intent['request_id'] != request_id or not isinstance(intent['version'],str)
            or not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,63}',intent['version'])):
        raise ContractError('invalid installed release intent')
    sha256(intent['trust_bundle_sha256'])
    from .controller_install import installation_settings,_home
    locations=installation_settings(config_home=config_home)
    data_home=_home(locations.get('data_home'),'XDG_DATA_HOME','.local/share')
    trust = load_bundle(trust_bundle)
    if (intent['trust_bundle_sha256'] != trust['bundle_sha256']
            or intent['release_base_url'] != trust['bundle']['release_base_url']):
        raise Conflict('installed release trust differs; explicit trust maintenance required')
    raw = bounded_file(records / 'release.json', 16384)
    sig = bounded_file(records / 'release.sig', 65536)
    accepted = _document(records / 'metadata.json', {'schema_version','statement_sha256','signature_sha256'}, limit=4096)
    if accepted != {'schema_version':1,'statement_sha256':digest(raw),'signature_sha256':digest(sig)} or type(accepted['schema_version']) is not int:
        raise Conflict('durable release evidence differs from accepted metadata')
    receipt = verify_statement(raw, sig, trust['public_key'], trust['bundle']['publisher_fingerprint'],
        download_directory=records, run=run,
        expected_public_key_sha256=trust['bundle']['public_key_sha256'])
    statement=receipt['statement']
    if statement['controller_version'] != intent['version']:
        raise Conflict('installed release version differs from its request')
    document = _document(records / 'result.json', {'schema_version','record_type','request_id','installation'})
    if (type(document['schema_version']) is not int or document['schema_version'] != 1
            or document['record_type'] != 'signed-release-installation' or document['request_id'] != request_id
            or not isinstance(document['installation'],dict)):
        raise ContractError('invalid signed installation result')
    installation=document['installation']
    runtime=data_home / 'quirkbench/controller' / (intent['version'] + '-' + statement['controller_archive_sha256'])
    retained=_managed_path(data_home / 'quirkbench/controller-archives')
    manifest, files, archive_digest=_verified_archive(retained / (statement['controller_archive_sha256'] + '.tar.gz'),
        expected_archive_sha256=statement['controller_archive_sha256'],expected_version=intent['version'])
    if archive_digest != statement['controller_archive_sha256'] or manifest['version'] != intent['version']:
        raise Conflict('retained controller archive differs from signed release identity')
    record={'schema_version':1,'version':manifest['version'],'archive_sha256':archive_digest,
            'signed':False,'qualified':False}
    # Signed provenance describes the acquired archive, not continuously
    # attested local code. Ordinary local edits do not invalidate that history.
    if installation_record(runtime)['archive_sha256'] != archive_digest:
        raise Conflict('local installation record differs from release provenance')
    expected={**record,'request_id':request_id,'distribution_verification':{
        **receipt,'controller_archive_authenticated':True,'other_release_assets_verified':False}}
    if installation != expected or record['archive_sha256'] != statement['controller_archive_sha256']:
        raise Conflict('signed installation result differs from acquired archive provenance')
    # Equality alone accepts boolean/integer substitution in Python; canonical bytes
    # retain the strict types across every nested historical receipt field.
    if canonical(installation) != canonical(expected):
        raise ContractError('invalid signed installation result types')
    return {'installation':{**record,'runtime_root':str(runtime)},'verification':receipt,'request_id':request_id}


def inspect_selected(runtime, *, config_home=None, trust_bundle=None, run=subprocess.run):
    path=_config_home(config_home) / 'quirkbench/signed-release-installation.json'
    if not path.exists():
        raise ReleaseUnavailable('signed installed release identity is unavailable')
    selected=_document(path, {'schema_version','record_type','request_id','installation'})
    if (type(selected['schema_version']) is not int or selected['schema_version'] != 1
            or selected['record_type'] != 'signed-release-installation'):
        raise ContractError('invalid signed installation selection')
    result=verify_request(selected['request_id'], config_home=config_home, trust_bundle=trust_bundle, run=run)
    if Path(runtime).expanduser().resolve() != Path(result['installation']['runtime_root']).resolve() or canonical(selected['installation']) != canonical({
        **{key:value for key,value in result['installation'].items() if key!='runtime_root'},'request_id':result['request_id'],'distribution_verification':{
        **result['verification'],'controller_archive_authenticated':True,'other_release_assets_verified':False}}):
        raise Conflict('selected runtime differs from authenticated release installation')
    return result
