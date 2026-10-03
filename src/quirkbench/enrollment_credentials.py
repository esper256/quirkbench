"""Local complete enrollment generation publication in the existing registry.

Anonymous routing is deliberately separate: its handler must demand a fresh
same-key proof before invoking this local application and returning private bytes.
"""
from __future__ import annotations

import re
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

from .contracts import Conflict,ContractError,canonical,digest
from .controller_setup import _durable_directory,_managed_path
from .controller_tls import _read
from .credential_registry import record_generation_in_transaction
from .enrollment import _document,_now,_snapshot,observe_clock
from .enrollment_certificate import issue_certificate
from .enrollment_client import endpoint
from .enrollment_proof import row_request,_invitation,validate_request
from .enrollment_result import validate_result
from .maintenance import private_lock
from .setup_contracts import SetupUnavailable
from .store import atomic_write



def publication(controller, *, run=subprocess.run,tls_inspector=None):
    """Capture configured repository/OSTree trust; no invented default or test trust."""
    from .controller_service import configuration
    config=configuration(controller.root);snapshot=_snapshot(controller.root,tls_inspector=tls_inspector)
    repository=config.get('repository_endpoint');signing=config.get('composition_signing');roots=config.get('repositories')
    if repository is None or signing is None or not roots:
        raise SetupUnavailable('enrollment unavailable: configure repository publication and composition signing trust')
    if not isinstance(repository,dict) or set(repository)!={'url'}:
        raise ContractError('invalid enrollment repository endpoint')
    endpoint(repository['url'])
    controller_host,_=endpoint(snapshot['controller_url']);repository_host,repository_port=endpoint(repository['url'])
    if controller_host!=repository_host or repository_port==endpoint(snapshot['controller_url'])[1]:
        raise ContractError('repository endpoint must use the controller certificate host and a separate port')
    if not isinstance(roots,dict) or not 1<=len(roots)<=8:raise ContractError('invalid enrollment repository aliases')
    from .contracts import identifier
    from .state_reader import read_file
    for alias,value in roots.items():
        identifier(alias);path=Path(value)
        if (not path.is_absolute() or path.resolve()!=path or not path.is_relative_to(controller.root/'repositories')
                or not path.is_dir()):raise ContractError('enrollment repository must be initialized in configured controller state')
        read_file(path,'config',limit=65536)
    public_key=export_public_key(signing,run=run)
    fingerprint=signing['fingerprint']
    return {**snapshot,'repository_url':repository['url'],'repository_roots':roots,
            'repository_public_key':public_key,'signing_fingerprint':fingerprint.upper()}


def export_public_key(signing, *,run=subprocess.run):
    """Verify exact explicitly selected public signing identity; no key generation."""
    fingerprint=signing['fingerprint']
    if not isinstance(fingerprint,str) or not re.fullmatch(r'[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64}',fingerprint):
        raise ContractError('full configured composition signing fingerprint required')
    try:
        exported=run(['gpg','--batch','--no-options','--homedir',signing['home'],'--armor','--export',fingerprint],
            check=False,capture_output=True,timeout=15,stdin=subprocess.DEVNULL)
    except (OSError,subprocess.TimeoutExpired) as exc:raise SetupUnavailable('native GPG repository trust export unavailable') from exc
    if exported.returncode or not isinstance(exported.stdout,bytes) or not 0<len(exported.stdout)<=65536:
        raise ContractError('configured repository public key export failed')
    with tempfile.TemporaryDirectory(prefix='quirkbench-enrollment-trust-') as directory:
        stage=Path(directory);path=stage/'key.asc';atomic_write(path,exported.stdout)
        try:
            shown=run(['gpg','--batch','--no-options','--homedir',directory,'--with-colons',
                '--import-options','show-only','--dry-run','--import',str(path)],
                check=False,capture_output=True,timeout=15,stdin=subprocess.DEVNULL)
        except (OSError,subprocess.TimeoutExpired) as exc:raise SetupUnavailable('native GPG repository trust validation unavailable') from exc
        if shown.returncode or not isinstance(shown.stdout,bytes) or len(shown.stdout)>65536:
            raise ContractError('invalid exported repository public key')
        try:rows=[line.split(':') for line in shown.stdout.decode('ascii').splitlines()]
        except UnicodeError as exc:raise ContractError('invalid repository key identity output') from exc
        public=[index for index,row in enumerate(rows) if row[0]=='pub']
        if (len(public)!=1 or any(row[0]=='sec' for row in rows) or len(rows[public[0]])<=11
                or rows[public[0]][1] in {'r','e','d'}
                or 's' not in rows[public[0]][11].lower() or public[0]+1>=len(rows)
                or len(rows[public[0]+1])<=9 or rows[public[0]+1][0]!='fpr' or rows[public[0]+1][9].upper()!=fingerprint.upper()):
            raise Conflict('repository export differs from configured signing identity')
    return exported.stdout.decode('ascii')


def complete_bound(controller,request, *, run=subprocess.run,tls_inspector=None,clock=time.time,
                   fault_hook=None,guard=None):
    """Local writer API: private complete reply first, both-channel registry last."""
    request=validate_request(request);root=_managed_path(controller.root);fault_hook=fault_hook or (lambda _:None)
    with private_lock(root/'command.lock',shared=True):
        now=_now(clock);observe_clock(controller,now)
        context=publication(controller,run=run,tls_inspector=tls_inspector);context_digest=digest(canonical(context))
        now=_now(clock);observe_clock(controller,now)
        directory=_managed_path(root/'private/enrollment/replies'/digest(request['request_id'].encode()))
        _durable_directory(directory)
        with private_lock(directory/'completion.lock'):
            with controller.transaction() as db:
                if guard is not None:guard(db)
                if now<db.execute('SELECT last_seen FROM enrollment_clock').fetchone()[0]:
                    raise Conflict('enrollment clock moved backwards during publication')
                bound=db.execute('SELECT * FROM enrollment_requests WHERE request_id=?',(request['request_id'],)).fetchone()
                if bound is None or bound['state']=='REVOKED':raise Conflict('active enrollment request/key binding required')
                row_request(bound,request);_invitation(db,request,now)
                from .retarget_invitation import reply_authority,validate_reply_authority
                retarget=reply_authority(db,request)
                if bound['state']=='COMPLETE':
                    raw=_read(directory,'result.json');result=validate_result(_document(raw),request)
                    validate_reply_authority(result,request,retarget)
                    if (digest(raw)!=bound['result_sha256'] or context_digest!=bound['publication_digest']
                            or result['credential_generation']['generation']!=bound['generation']):
                        raise Conflict('complete private enrollment differs from durable binding/publication')
                    live=db.execute('SELECT * FROM credential_generations WHERE generation=?',(bound['generation'],)).fetchone()
                    if live is None or live['revoked'] or not now<live['expires_at']:
                        raise Conflict('enrollment credential generation revoked or expired')
                    if record_generation_in_transaction(db,result['credential_generation'])['revoked']:
                        raise Conflict('enrollment credential generation revoked')
                    return result
            marker=directory/'intent.json'
            expected={'schema_version':1,'request_digest':digest(canonical(request)),'publication_digest':context_digest}
            if marker.exists() or marker.is_symlink():
                intent=_document(_read(directory,marker.name))
                if (not isinstance(intent,dict) or set(intent)!=set(expected)|{'generation','device_id','device_token'}
                        or type(intent['schema_version']) is not int or any(intent.get(k)!=v for k,v in expected.items())):
                    raise Conflict('enrollment completion has another immutable publication intent')
                from .contracts import identifier
                identifier(intent['generation']);identifier(intent['device_id'])
                if not isinstance(intent['device_token'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',intent['device_token']):
                    raise ContractError('invalid private enrollment token')
            else:
                intent={**expected,'generation':'generation-'+secrets.token_hex(16),'device_id':'target-'+secrets.token_hex(16),
                        'device_token':secrets.token_urlsafe(32)}
                atomic_write(marker,canonical(intent))
            fault_hook('credential_intent_retained')
            cert=issue_certificate(controller,request,intent['generation'],intent['device_id'],run=run,
                tls_inspector=tls_inspector,clock=clock,guard=guard)
            result_path=directory/'result.json'
            if result_path.exists() or result_path.is_symlink():result=validate_result(_document(_read(directory,result_path.name)),request)
            else:
                generation={'schema_version':1,'generation':intent['generation'],'device_id':intent['device_id'],
                    'media_instance_id':request['media_instance_id'],'system_uuid':request['target_binding']['system_uuid'],
                    'device_token_sha256':digest(intent['device_token'].encode()),
                    'repository_certificate_sha256':cert['certificate_sha256'],'expires_at':cert['expires_at']}
                result=validate_result({'schema_version':2 if retarget is not None else 1,'record_type':'enrollment-result',
                    'request_id':request['request_id'],'request_digest':expected['request_digest'],
                    'device_id':intent['device_id'],'media_instance_id':request['media_instance_id'],
                    'target_binding':request['target_binding'],'controller_url':context['controller_url'],
                    'credential_generation':generation,'controller_ca_pem':cert['ca_pem'],'device_token':intent['device_token'],
                    'repository_certificate_pem':cert['certificate_pem'],'repository_remotes':{
                        alias:{'url':context['repository_url']+'/'+alias,'public_key':context['repository_public_key']}
                        for alias in context['repository_roots']},
                    **({'retarget_invitation':retarget} if retarget is not None else {})},request)
                atomic_write(result_path,canonical(result))
            if (result['credential_generation']['generation']!=intent['generation'] or result['device_id']!=intent['device_id']
                    or result['device_token']!=intent['device_token'] or result['controller_url']!=context['controller_url']
                    or result['controller_ca_pem']!=cert['ca_pem'] or result['repository_certificate_pem']!=cert['certificate_pem']
                    or result['credential_generation']['expires_at']!=cert['expires_at']
                    or result['repository_remotes']!={alias:{'url':context['repository_url']+'/'+alias,
                        'public_key':context['repository_public_key']} for alias in context['repository_roots']}):
                raise Conflict('private enrollment result differs from retained completion intent')
            validate_reply_authority(result,request,retarget)
            fault_hook('credential_reply_retained')
            now=_now(clock);observe_clock(controller,now)
            # Recheck publication after native work, then atomically grant the
            # exact two-channel generation and complete request in the same DB.
            if publication(controller,run=run,tls_inspector=tls_inspector)!=context:
                raise Conflict('enrollment publication changed during credential preparation')
            persisted=_read(directory,result_path.name)
            validate_result(_document(persisted),request)
            if persisted!=canonical(result):
                raise Conflict('private enrollment reply changed before credential publication')
            now=_now(clock);observe_clock(controller,now)
            with controller.transaction() as db:
                if guard is not None:guard(db)
                bound=db.execute('SELECT * FROM enrollment_requests WHERE request_id=?',(request['request_id'],)).fetchone()
                if bound is None or bound['state']!='BOUND':raise Conflict('pending enrollment changed during credential preparation')
                row_request(bound,request);_invitation(db,request,now)
                validate_reply_authority(result,request,reply_authority(db,request))
                if now<db.execute('SELECT last_seen FROM enrollment_clock').fetchone()[0] or not now<result['credential_generation']['expires_at']:
                    raise Conflict('enrollment clock changed or credentials expired during preparation')
                if record_generation_in_transaction(db,result['credential_generation'])['revoked']:
                    raise Conflict('credential generation already revoked')
                db.execute("UPDATE enrollment_requests SET state='COMPLETE',generation=?,result_sha256=?,publication_digest=? WHERE request_id=?",
                    (intent['generation'],digest(persisted),context_digest,request['request_id']))
            fault_hook('credential_generation_committed')
            return result
