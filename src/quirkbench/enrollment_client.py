"""Secret-free certificate inspection and explicitly pinned enrollment HTTPS.

This adapter grants no enrollment/activation authority. The local screen must
compare the displayed full fingerprint out of band and supply its approval.
"""
from __future__ import annotations

import http.client
import ipaddress
import ssl
import time
from urllib.parse import urlsplit

from .contracts import Conflict, ContractError, canonical, digest, sha256
from .enrollment import _now
from .transport import TransportError, _strict_json
from .product_contracts import _depth
from .http_bounds import BoundedHTTPError
from .release_http import _connect, _response, _length, _remaining

MAX_BODY = 16384



def endpoint(url):
    if not isinstance(url,str) or len(url)>4096:
        raise ContractError('invalid enrollment endpoint')
    try:
        parts=urlsplit(url);port=parts.port
    except ValueError as exc:raise ContractError('invalid enrollment endpoint') from exc
    if (parts.scheme!='https' or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment or not port):
        raise ContractError('enrollment requires an exact HTTPS origin and port')
    # Resolve DNS only through the native socket/TLS stack, with SAN verification.
    if any(char.isspace() for char in url) or '%' in parts.netloc:
        raise ContractError('invalid enrollment endpoint host')
    try:
        address=ipaddress.ip_address(parts.hostname)
    except ValueError:address=None
    if address is not None and (address.is_unspecified or address.is_multicast):
        raise ContractError('enrollment requires a specific endpoint')
    return parts.hostname,port


def inspect_certificate(url, *, clock=time.time, monotonic=time.monotonic):
    """Unauthenticated public observation only: send no HTTP or secret bytes."""
    host,port=endpoint(url);_now(clock);deadline=monotonic()+45
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname=False;context.verify_mode=ssl.CERT_NONE
    try:
        with _connect((host,port),deadline,monotonic) as connection:
            connection.settimeout(_remaining(deadline,monotonic))
            with context.wrap_socket(connection,server_hostname=host) as tls:
                _remaining(deadline,monotonic)
                der=tls.getpeercert(binary_form=True)
                _remaining(deadline,monotonic)
    except Conflict as exc:
        raise TransportError(str(exc)) from exc
    except (OSError,ssl.SSLError) as exc:
        raise TransportError('controller certificate inspection failed') from exc
    if not der or len(der)>65536:
        raise ContractError('controller certificate is missing or exceeds limit')
    return {'controller_url':url,'certificate_sha256':digest(der),
            'certificate_pem':ssl.DER_cert_to_PEM_cert(der),'authenticated':False}


class PinnedEnrollmentClient:
    def __init__(self,url,certificate_pem,approved_fingerprint, *, clock=time.time,
                 monotonic=time.monotonic):
        self.host,self.port=endpoint(url);self.url=url;sha256(approved_fingerprint);_now(clock)
        if not isinstance(certificate_pem,str) or len(certificate_pem)>65536:
            raise ContractError('invalid inspected controller certificate')
        try:der=ssl.PEM_cert_to_DER_cert(certificate_pem)
        except (ValueError,ssl.SSLError) as exc:raise ContractError('invalid inspected controller certificate') from exc
        if digest(der)!=approved_fingerprint:
            raise ContractError('controller fingerprint differs from explicit approval')
        self.context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.context.hostname_checks_common_name=False
        # Only the approved leaf is trusted for this bounded bootstrap. Retain
        # native validity, purpose and endpoint SAN validation; install CA trust
        # only from a later authenticated, complete enrollment result.
        self.context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        self.context.load_verify_locations(cadata=certificate_pem)
        self.fingerprint=approved_fingerprint;self.clock=clock;self.monotonic=monotonic

    def post(self,path,document):
        if path not in ('/v1/enrollment/challenge','/v1/enrollment/redeem'):
            raise ContractError('unknown enrollment exchange route')
        raw=canonical(document)
        if len(raw)>MAX_BODY:raise ContractError('enrollment request exceeds byte limit')
        _now(self.clock);deadline=self.monotonic()+45
        try:
            # Shared bounded DNS/connect/TLS/write/read adapter compares the exact
            # approved native leaf before transmitting any request or proof.
            with _response(self.url+path,deadline,self.monotonic,method='POST',body=raw,
                    headers={'Content-Type':'application/json','Cache-Control':'no-store'},context=self.context,
                    expected_status=None,expected_peer_sha256=self.fingerprint) as response:
                if response.status!=200:
                    raise TransportError('enrollment HTTP '+str(response.status))
                if response.getheader('Content-Type','').split(';')[0]!='application/json':
                    raise TransportError('invalid enrollment response content type')
                length=_length(response,MAX_BODY)
                body=bytearray()
                while len(body)<length:
                    _remaining(deadline,self.monotonic)
                    chunk=response.read1(min(4096,length-len(body)))
                    _remaining(deadline,self.monotonic)
                    if not chunk:raise TransportError('incomplete enrollment response')
                    body.extend(chunk)
            try:
                value=_strict_json(bytes(body));_depth(value)
            except (ValueError,UnicodeError,RecursionError) as exc:
                raise ContractError('invalid bounded enrollment response JSON') from exc
            if set(value)!={'schema_version','data'} or type(value['schema_version']) is not int or value['schema_version']!=1:
                raise ContractError('invalid enrollment response envelope')
            if not isinstance(value['data'],dict) or set(value['data'])!={'value'} or not isinstance(value['data']['value'],dict):
                raise ContractError('invalid enrollment response result')
            return value['data']['value']
        except (BoundedHTTPError,Conflict) as exc:
            raise TransportError(str(exc)) from exc
        except (OSError,ssl.SSLError,http.client.HTTPException) as exc:
            raise TransportError('authenticated enrollment exchange failed; check endpoint, clock and approved trust') from exc
