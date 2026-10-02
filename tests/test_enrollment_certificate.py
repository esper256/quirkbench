"""Native clientAuth issuance persists exact bytes without credential activation."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import ssl
import subprocess
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID

from quirkbench import enrollment_certificate as certificates
from quirkbench import enrollment_proof as proof
from quirkbench.contracts import ContractError,Conflict,digest
from quirkbench.enrollment import create_code,revoke_code
from test_enrollment import issuer
from test_setup_service import initialized
from test_enrollment_proof import ProofCommands,request,signed_nonce,args


class CertificateCommands(ProofCommands):
    def __call__(self,argv,**kw):
        def arg(flag):return argv[argv.index(flag)+1]
        if argv[1]=='x509' and '-new' in argv:
            ca=x509.load_pem_x509_certificate(Path(arg('-CA')).read_bytes())
            private=serialization.load_pem_private_key(Path(arg('-CAkey')).read_bytes(),password=None)
            public=serialization.load_pem_public_key(Path(arg('-force_pubkey')).read_bytes())
            assert Path(arg('-extfile')).read_bytes()==b'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n'
            now=datetime.now(timezone.utc)
            cert=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,arg('-subj').removeprefix('/CN='))]))
                .issuer_name(ca.subject).public_key(public).serial_number(int(arg('-set_serial')))
                .not_valid_before(now-timedelta(seconds=1)).not_valid_after(now+timedelta(days=int(arg('-days'))))
                .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
                .add_extension(x509.KeyUsage(True,False,False,False,False,False,False,False,False),critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(public),critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()),critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),critical=False)
                .sign(private,None))
            return subprocess.CompletedProcess(argv,0,cert.public_bytes(serialization.Encoding.PEM),b'')
        if argv[1]=='verify' and '-purpose' in argv and arg('-purpose')=='sslclient':
            cert=x509.load_pem_x509_certificate(Path(argv[-1]).read_bytes())
            ca=x509.load_pem_x509_certificate(Path(arg('-CAfile')).read_bytes())
            ca.public_key().verify(cert.signature,cert.tbs_certificate_bytes)
            assert cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value==x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH])
            assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            return subprocess.CompletedProcess(argv,0,b'verified',b'')
        if argv[1]=='x509' and any(flag in argv for flag in ('-serial','-enddate','-dates')):
            cert=x509.load_pem_x509_certificate(Path(arg('-in')).read_bytes())
            out=('serial='+format(cert.serial_number,'X') if '-serial' in argv else
                'notAfter='+cert.not_valid_after_utc.strftime('%b %d %H:%M:%S %Y GMT'))
            if '-dates' in argv:out='notBefore='+cert.not_valid_before_utc.strftime('%b %d %H:%M:%S %Y GMT')+'\n'+out
            return subprocess.CompletedProcess(argv,0,(out+'\n').encode(),b'')
        return super().__call__(argv,**kw)


@pytest.fixture
def bound(issuer):
    c,kwargs=issuer;kwargs={**kwargs,'clock':time.time}
    code=create_code(c,'target','pair',**kwargs);key=ed25519.Ed25519PrivateKey.generate();req=request(code,key)
    nonce,sig=signed_nonce(c,req,key,kwargs)
    proof.reserve_redemption(c,req,nonce['challenge_id'],code['code'],sig,**args(kwargs))
    return c,req,code,kwargs


def issue(c,req,kwargs,**patch):
    return certificates.issue_certificate(c,req,'generation-fixture','target-fixture',
        tls_inspector=kwargs['tls_inspector'],run=CertificateCommands(),**patch)


@pytest.mark.parametrize('boundary',['issuer_intent_retained','repository_certificate_retained','issuer_receipt_retained'])
def test_durable_certificate_replay_retains_key_ca_and_leaf_without_registry_success(bound,boundary):
    c,req,code,kwargs=bound
    def fail(stage):
        if stage==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):issue(c,req,kwargs,fault_hook=fail)
    result=issue(c,req,kwargs);assert issue(c,req,kwargs)==result
    cert=x509.load_pem_x509_certificate(result['certificate_pem'].encode())
    assert cert.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)==proof._bytes(req['public_key'],44)
    assert result['certificate_sha256']==digest(ssl.PEM_cert_to_DER_cert(result['certificate_pem']))
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==0
        assert db.execute('SELECT state FROM enrollment_requests').fetchone()[0]=='BOUND'


def test_unbound_revoked_or_changed_intent_cannot_issue_or_replace_certificate(bound):
    c,req,code,kwargs=bound;first=issue(c,req,kwargs)
    with pytest.raises(Conflict):issue(c,{**req,'request_id':'unbound'},kwargs)
    with pytest.raises(Conflict):issue(c,req,kwargs,days=31)
    revoke_code(c,code['record']['code_id'])
    with pytest.raises(Conflict):issue(c,req,kwargs)
    assert (c.root/'private/enrollment/generations/generation-fixture/repository.crt').read_text()==first['certificate_pem']


def test_revocation_during_native_issuance_cannot_publish_credential_result(bound):
    c,req,code,kwargs=bound
    def revoke(stage):
        if stage=='repository_certificate_retained':revoke_code(c,code['record']['code_id'])
    with pytest.raises(Conflict,match='revoked or completed during'):issue(c,req,kwargs,fault_hook=revoke)


@pytest.mark.parametrize('change',['missing','changed','boolean_intent','boolean_receipt'])
def test_completed_issuer_never_regenerates_or_accepts_changed_leaf(bound,change):
    import json
    from quirkbench.contracts import canonical
    c,req,code,kwargs=bound;issue(c,req,kwargs)
    directory=c.root/'private/enrollment/generations/generation-fixture'
    if change=='missing':(directory/'repository.crt').unlink()
    elif change=='changed':(directory/'repository.crt').write_bytes(b'replaced certificate')
    else:
        path=directory/('issuer-intent.json' if change=='boolean_intent' else 'issuer-receipt.json')
        value=json.loads(path.read_bytes());value['schema_version']=True;path.write_bytes(canonical(value))
    with pytest.raises((Conflict,ContractError,OSError)):issue(c,req,kwargs)
    if change=='missing':assert not (directory/'repository.crt').exists()


def test_issuer_rejects_known_backward_clock_before_files(bound):
    c,req,code,kwargs=bound
    with pytest.raises(Conflict,match='clock moved backwards'):issue(c,req,kwargs,clock=lambda:1000)
    assert not (c.root/'private/enrollment/generations').exists()


def test_issuer_binds_captured_ca_to_verified_identity_digest(bound,monkeypatch):
    import json
    from quirkbench.contracts import canonical
    c,req,code,kwargs=bound;read=certificates._read
    def changed(directory,name):
        raw=read(directory,name)
        if name=='ca.crt':return b'coordinated new CA'
        if name=='ca.key':return b'coordinated new key'
        if name=='identity.json':
            value=json.loads(raw);value['files']['ca.crt']=digest(b'coordinated new CA')
            value['files']['ca.key']=digest(b'coordinated new key');return canonical(value)
        return raw
    monkeypatch.setattr(certificates,'_read',changed)
    with pytest.raises(Conflict,match='issuer bytes changed'):issue(c,req,kwargs)
    assert not (c.root/'private/enrollment/generations').exists()
