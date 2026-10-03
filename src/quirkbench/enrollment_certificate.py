"""Private immutable native clientAuth certificate for a bound enrollment key.

Local issuer groundwork only; this does not publish a credential registry entry
or return an enrollment success. The application must durably commit both channels
and its complete private reply before exposing the result.
"""
from __future__ import annotations

from datetime import datetime,timezone
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
import time

from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_setup import _durable_directory,_managed_path
from .controller_tls import _read,_openssl,inspect_identity,load_identity
from .enrollment import _document,_now,_snapshot,observe_clock
from .enrollment_proof import _bytes,validate_request,validate_public_key,row_request,_invitation
from .maintenance import private_lock
from .store import atomic_write


def _expiry(raw):
    try:
        text=raw.decode('ascii').strip()
        if not text.startswith('notAfter='):raise ValueError()
        return int(datetime.strptime(text[9:],'%b %d %H:%M:%S %Y GMT').replace(tzinfo=timezone.utc).timestamp())
    except (ValueError,UnicodeError) as exc:raise ContractError('invalid native client certificate expiry') from exc


def issue_certificate(controller,request,generation,device_id, *, days=30,run=subprocess.run,
                      tls_inspector=None,clock=time.time,fault_hook=None,guard=None):
    """Reuse exact private issuance across lost ACK; refuse changed key/CA/intent."""
    request=validate_request(request);identifier(generation);identifier(device_id)
    if type(days) is not int or not 1<=days<=365:raise ContractError('invalid repository credential lifetime')
    root=_managed_path(controller.root);fault_hook=fault_hook or (lambda _:None)
    from .controller_service import configuration
    with private_lock(root/'command.lock',shared=True):
        now=_now(clock);observe_clock(controller,now);config=configuration(root)
        snapshot=_snapshot(root,tls_inspector=tls_inspector)
        with controller.transaction() as db:
            if guard is not None:guard(db)
            bound=db.execute('SELECT * FROM enrollment_requests WHERE request_id=?',(request['request_id'],)).fetchone()
            if bound is None or bound['state']!='BOUND' or bound['generation'] is not None:
                raise Conflict('active pending enrollment binding required for certificate issuance')
            row_request(bound,request);invitation=_invitation(db,request,now)
            if any(invitation[key]!=value for key,value in snapshot.items()):
                raise Conflict('bound enrollment trust changed')
        ca_directory=Path(config['cert']).parent
        observed=(tls_inspector or inspect_identity)(ca_directory,run=run) if tls_inspector is None else tls_inspector(ca_directory)
        ca=_read(ca_directory,'ca.crt');ca_key=_read(ca_directory,'ca.key')
        identity=load_identity(_read(ca_directory,'identity.json'))
        if (digest(canonical(identity))!=observed['identity_sha256']
                or digest(ca)!=identity['files']['ca.crt'] or digest(ca_key)!=identity['files']['ca.key']
                or observed['certificate_sha256']!=snapshot['certificate_sha256']):
            raise Conflict('controller issuer bytes changed')
        public=_bytes(request['public_key'],44);validate_public_key(public,run=run)
        directory=_managed_path(root/'private/enrollment/generations'/generation);_durable_directory(directory)
        with private_lock(directory/'issuance.lock'):
            intent={'schema_version':1,'generation':generation,'device_id':device_id,
                    'request_digest':digest(canonical(request)),'key_sha256':digest(public),
                    'ca_sha256':digest(ca),'certificate_sha256':snapshot['certificate_sha256'],'days':days}
            marker=directory/'issuer-intent.json'
            if marker.exists() or marker.is_symlink():
                old=_document(_read(directory,marker.name))
                if (not isinstance(old,dict) or set(old)!=set(intent)|{'serial'}
                        or canonical({k:old[k] for k in intent})!=canonical(intent)):
                    raise Conflict('repository certificate generation has another immutable intent')
                serial=old['serial']
                if type(serial) is not int or not 0<serial<2**159:raise ContractError('invalid retained certificate serial')
            else:
                serial=secrets.randbits(158)+1
                atomic_write(marker,canonical({**intent,'serial':serial}))
            fault_hook('issuer_intent_retained')
            path=directory/'repository.crt'
            receipt_path=directory/'issuer-receipt.json';receipt=None
            if receipt_path.exists() or receipt_path.is_symlink():
                receipt=_document(_read(directory,receipt_path.name))
                if (not isinstance(receipt,dict) or set(receipt)!={'schema_version','intent_sha256','certificate_sha256','expires_at'}
                        or type(receipt['schema_version']) is not int or receipt['schema_version']!=1
                        or type(receipt['expires_at']) is not int or not 0<receipt['expires_at']<=4102444800):
                    raise ContractError('invalid private repository certificate receipt')
                sha256(receipt['intent_sha256']);sha256(receipt['certificate_sha256'])
                if (receipt['intent_sha256']!=digest(canonical({**intent,'serial':serial}))
                        or digest(_read(directory,path.name))!=receipt['certificate_sha256']):
                    raise Conflict('completed repository certificate differs from retained receipt')
            with tempfile.TemporaryDirectory(prefix='issuer-',dir=directory) as temporary:
                stage=Path(temporary)
                for name,raw in (('ca.crt',ca),('ca.key',ca_key),('public.der',public)):
                    atomic_write(stage/name,raw)
                pem=_openssl(['pkey','-pubin','-inform','DER','-in',str(stage/'public.der'),'-pubout'],run=run)
                atomic_write(stage/'public.pem',pem)
                atomic_write(stage/'extensions',b'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n')
                if path.exists() or path.is_symlink():certificate=_read(directory,path.name)
                else:
                    certificate=_openssl(['x509','-new','-force_pubkey',str(stage/'public.pem'),
                        '-subj','/CN='+device_id,'-CA',str(stage/'ca.crt'),'-CAkey',str(stage/'ca.key'),
                        '-set_serial',str(serial),'-days',str(days),'-extfile',str(stage/'extensions')],run=run)
                atomic_write(stage/'client.crt',certificate)
                _openssl(['verify','-no-CApath','-no-CAstore','-CAfile',str(stage/'ca.crt'),
                    '-purpose','sslclient',str(stage/'client.crt')],run=run)
                found=_openssl(['x509','-in',str(stage/'client.crt'),'-pubkey','-noout'],run=run)
                found_serial=_openssl(['x509','-in',str(stage/'client.crt'),'-serial','-noout'],run=run)
                try:
                    label,hexadecimal=found_serial.decode('ascii').strip().split('=',1)
                    exact_serial=label=='serial' and int(hexadecimal,16)==serial
                except (ValueError,UnicodeError):exact_serial=False
                if found!=pem or not exact_serial:
                    raise Conflict('repository certificate key/serial differs from retained issuance')
                expires=_expiry(_openssl(['x509','-in',str(stage/'client.crt'),'-enddate','-noout'],run=run))
                if not _now(clock)<expires:raise Conflict('repository client certificate expired; explicit lifecycle maintenance required')
                try:der=ssl.PEM_cert_to_DER_cert(certificate.decode('ascii'))
                except (ValueError,UnicodeError) as exc:raise ContractError('invalid native client certificate') from exc
                if not path.exists():atomic_write(path,certificate)
            fault_hook('repository_certificate_retained')
            expected_receipt={'schema_version':1,'intent_sha256':digest(canonical({**intent,'serial':serial})),
                              'certificate_sha256':digest(certificate),'expires_at':expires}
            if receipt is not None and canonical(receipt)!=canonical(expected_receipt):
                raise Conflict('repository certificate receipt differs from verified issuance')
            if receipt is None:atomic_write(receipt_path,canonical(expected_receipt))
            fault_hook('issuer_receipt_retained')
            now=_now(clock);observe_clock(controller,now)
            with controller.transaction() as db:
                if guard is not None:guard(db)
                if now<db.execute('SELECT last_seen FROM enrollment_clock').fetchone()[0]:
                    raise Conflict('enrollment clock moved backwards during certificate issuance')
                bound=db.execute('SELECT * FROM enrollment_requests WHERE request_id=?',(request['request_id'],)).fetchone()
                if bound is None or bound['state']!='BOUND' or bound['generation'] is not None:
                    raise Conflict('enrollment revoked or completed during certificate issuance')
                row_request(bound,request);_invitation(db,request,now)
            return {'generation':generation,'device_id':device_id,'certificate_pem':certificate.decode('ascii'),
                    'certificate_sha256':digest(der),'expires_at':expires,'ca_pem':ca.decode('ascii')}
