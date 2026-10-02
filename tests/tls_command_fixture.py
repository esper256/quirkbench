"""Injected native OpenSSL command adapter backed by the test-only crypto extra."""
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

PEM = serialization.Encoding.PEM


class TLSCommands:
    def __init__(self): self.calls = []
    def __call__(self, argv, **kwargs):
        assert argv[0] == 'openssl' and kwargs['timeout'] == 15
        assert kwargs['env']['OPENSSL_CONF'] == '/dev/null'
        self.calls.append(argv)
        def arg(flag): return argv[argv.index(flag)+1]
        def key(path): return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
        def cert(path): return x509.load_pem_x509_certificate(Path(path).read_bytes())
        def name(raw): return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, raw.removeprefix('/CN='))])
        now = datetime.now(timezone.utc)
        out = b''
        if argv[1] == 'genpkey':
            assert arg('-algorithm') == 'ED25519'
            out = ed25519.Ed25519PrivateKey.generate().private_bytes(PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        elif argv[1] == 'req':
            assert Path(arg('-config')).read_bytes() == b'[req]\ndistinguished_name=dn\n[dn]\n'
            private = key(arg('-key')); subject = name(arg('-subj'))
            if '-x509' in argv:
                assert 'basicConstraints=critical,CA:TRUE' in argv
                out = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(private.public_key())
                    .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
                    .not_valid_after(now+timedelta(days=int(arg('-days'))))
                    .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
                    .add_extension(x509.KeyUsage(False,False,False,False,False,True,True,False,False),critical=True)
                    .add_extension(x509.SubjectKeyIdentifier.from_public_key(private.public_key()),critical=False)
                    .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(private.public_key()),critical=False)
                    .sign(private,None).public_bytes(PEM))
            else:
                host = next(v.removeprefix('subjectAltName=IP:') for v in argv if v.startswith('subjectAltName=IP:'))
                out = (x509.CertificateSigningRequestBuilder().subject_name(subject)
                    .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]),critical=False)
                    .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
                    .add_extension(x509.KeyUsage(True,False,False,False,False,False,False,False,False),critical=True)
                    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False)
                    .sign(private,None).public_bytes(PEM))
        elif argv[1] == 'x509' and '-req' in argv:
            csr=x509.load_pem_x509_csr(Path(arg('-in')).read_bytes()); ca=cert(arg('-CA')); private=key(arg('-CAkey'))
            builder=(x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca.subject).public_key(csr.public_key())
                .serial_number(int(arg('-set_serial'))).not_valid_before(now-timedelta(minutes=1))
                .not_valid_after(now+timedelta(days=int(arg('-days')))))
            for extension in csr.extensions: builder=builder.add_extension(extension.value,extension.critical)
            assert Path(arg('-extfile')).read_bytes()==b'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n'
            builder=builder.add_extension(x509.SubjectKeyIdentifier.from_public_key(csr.public_key()),critical=False)
            builder=builder.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()),critical=False)
            out=builder.sign(private,None).public_bytes(PEM)
        elif argv[1] == 'verify':
            assert '-no-CApath' in argv and '-no-CAstore' in argv
            ca=cert(arg('-CAfile')); supplied=cert(argv[-1])
            assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            ca.public_key().verify(supplied.signature,supplied.tbs_certificate_bytes)
            if '-no_check_time' not in argv:
                assert supplied.not_valid_before_utc <= now < supplied.not_valid_after_utc
            if '-verify_ip' in argv:
                assert ipaddress.ip_address(arg('-verify_ip')) in supplied.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)
            out=b'fixture verified\n'
        elif argv[1] == 'pkey':
            out=key(arg('-in')).public_key().public_bytes(PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        elif argv[1] == 'x509' and '-enddate' in argv:
            out=('notAfter='+cert(arg('-in')).not_valid_after_utc.strftime('%b %d %H:%M:%S %Y GMT')+'\n').encode()
        elif argv[1] == 'x509':
            out=cert(arg('-in')).public_key().public_bytes(PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        else: raise AssertionError('unexpected native TLS command')
        if '-out' in argv:
            Path(arg('-out')).write_bytes(out);out=b''
        return subprocess.CompletedProcess(argv,0,out,b'')
