"""Enrollment scope/certificate/secret checks before any native staging write."""
import datetime
import ssl
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from quirkbench import preparation_payload as payload
from quirkbench.contracts import digest,ContractError
from quirkbench.commission import CommissionError
from test_preparation_plan import selected
from test_prepared_media import factory


@pytest.fixture
def inputs(selected,tmp_path):
    plan,fd=selected
    key=ed25519.Ed25519PrivateKey.generate();name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'disposable-test')])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
        .not_valid_after(now+datetime.timedelta(days=1)).sign(key,None))
    pem=cert.public_bytes(serialization.Encoding.PEM).decode()
    plan['controller']['certificate_sha256']=digest(ssl.PEM_cert_to_DER_cert(pem))
    invitation={'record':{'schema_version':2,'record_type':'enrollment-code','code_id':'code-fixture',
        'request_id':'prepare-fixture','request_digest':'d'*64,'name':plan['target'],**plan['controller'],
        'created_at':1,'expires_at':None},'code':'a'*43}
    work=tmp_path/'components';work.mkdir()
    return plan,work,invitation,pem


@pytest.mark.parametrize('fault',['target','controller','certificate','code','expiry'])
def test_invalid_enrollment_never_reaches_filesystem_tools(inputs,fault):
    plan,work,invitation,pem=inputs
    if fault=='target':invitation['record']['name']='different-target'
    elif fault=='controller':invitation['record']['certificate_sha256']='e'*64
    elif fault=='certificate':plan['controller']['certificate_sha256']='e'*64;invitation['record']['certificate_sha256']='e'*64
    elif fault=='code':invitation['code']='bad code'
    else:invitation['record']['schema_version']=1;invitation['record']['expires_at']=91
    with pytest.raises((CommissionError,ContractError)):
        payload.populate(plan,work,invitation,pem,deadline=time.monotonic()+3,
            runner=lambda *a,**kw:pytest.fail('untrusted enrollment reached filesystem tools'))
    assert not (work/'partition-6').exists()
