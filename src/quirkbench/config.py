"""Editable setup and stable connection credentials."""

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import secrets
import tomllib
from urllib.parse import urlparse

import asyncssh
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .files import atomic_write


@dataclass
class Config:
    url: str = "https://localhost:7443"
    ssh_port: int = 7444
    mcp_port: int = 7442
    bind: str = "0.0.0.0"

    def validate(self):
        for name, expected in (("url", str), ("bind", str), ("ssh_port", int), ("mcp_port", int)):
            if type(getattr(self, name)) is not expected:
                raise ValueError(f"{name} must be a {expected.__name__}.")
        parsed = urlparse(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Controller address must be an HTTPS address without a path or password."
            )
        for port in (parsed.port or 443, self.ssh_port, self.mcp_port):
            if not 1 <= port <= 65535:
                raise ValueError("Ports must be between 1 and 65535.")
        if len({parsed.port or 443, self.ssh_port, self.mcp_port}) != 3:
            raise ValueError("HTTPS, target SSH and MCP need different ports.")
        self.url = self.url.rstrip("/")
        return self

    @classmethod
    def load(cls, root: Path):
        try:
            data = tomllib.loads((root / "config.toml").read_text())
            unknown = set(data) - set(cls.__dataclass_fields__)
            if unknown:
                raise ValueError("Unknown configuration keys: " + ", ".join(sorted(unknown)))
            return cls(**data).validate()
        except FileNotFoundError:
            raise ValueError("Controller is not configured. Run quirkbench setup.") from None
        except ValueError as error:
            raise ValueError(f"{root / 'config.toml'}: {error}") from None


def setup(root: Path, config: Config):
    config.validate()
    root.mkdir(parents=True, exist_ok=True)
    keypath = root / "controller.key"
    if not keypath.exists():
        key = asyncssh.generate_private_key("ssh-ed25519")
        atomic_write(keypath, key.export_private_key())
    pub = asyncssh.read_private_key(keypath).export_public_key()
    atomic_write(root / "controller.pub", pub, 0o644)
    if not (root / "mcp-token").exists():
        atomic_write(root / "mcp-token", secrets.token_urlsafe(32) + "\n")
    host = urlparse(config.url).hostname
    certpath = root / "controller.crt"
    # Config changes are normal. Keep TLS key and refresh its certificate only if needed.
    tlskey = root / "tls.key"
    if not tlskey.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        atomic_write(
            tlskey,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
    key = serialization.load_pem_private_key(tlskey.read_bytes(), password=None)
    try:
        address = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        address = x509.DNSName(host)
    refresh = True
    if certpath.exists():
        cert = x509.load_pem_x509_certificate(certpath.read_bytes())
        refresh = (
            address
            not in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        )
    if refresh:
        now = datetime.now(timezone.utc)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([address]), critical=False)
            .sign(key, hashes.SHA256())
        )
        atomic_write(certpath, cert.public_bytes(serialization.Encoding.PEM), 0o644)
    atomic_write(
        root / "config.toml",
        "\n".join(f"{k} = {json.dumps(v)}" for k, v in asdict(config).items()) + "\n",
    )
    return config
