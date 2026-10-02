from datetime import datetime, timedelta, timezone
import ipaddress
import pytest


@pytest.fixture
def signing_home():
    """Native fixture signing needs a short AF_UNIX socket, regardless of TMPDIR."""
    from pathlib import Path
    import shutil
    import subprocess
    import tempfile

    if not shutil.which('gpg') or not shutil.which('gpgconf'):
        pytest.skip('native gpg and gpgconf required for signing and agent cleanup')
    # /tmp is an intentional socket-length constraint, not a controller state
    # default. mkdtemp creates a unique private home; no user keyring is touched.
    with tempfile.TemporaryDirectory(prefix='qb-gpg-', dir='/tmp') as directory:
        home = Path(directory)
        try:
            yield home
        finally:
            subprocess.run(['gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'],
                           check=True, capture_output=True, timeout=15)

@pytest.fixture(scope='session')
def cert_files(tmp_path_factory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    folder=tmp_path_factory.mktemp('tls')
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
          .public_key(key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=1))
          .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
          .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
          .sign(key,hashes.SHA256()))
    certfile=folder/'cert.pem';keyfile=folder/'key.pem'
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    keyfile.chmod(0o600)
    return certfile,keyfile
