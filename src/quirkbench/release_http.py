"""Native verified HTTPS acquisition with raw framing and monotonic read bounds."""
from contextlib import contextmanager
import http.client
import ipaddress
import json
import math
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit

from .contracts import Conflict,ContractError,digest,sha256
from .http_bounds import _DeadlineSocket

RESOLVER = """import json,socket,sys
rows=socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),0,socket.SOCK_STREAM,socket.IPPROTO_TCP)
if not 0<len(rows)<=64:raise SystemExit(2)
raw=json.dumps(rows,separators=(',',':')).encode()
if len(raw)>16384:raise SystemExit(2)
sys.stdout.buffer.write(raw)
"""


class _DeadlineWrites:
    def __init__(self,sock,deadline,clock, *,operation='release acquisition'):self.sock=sock;self.deadline=deadline;self.clock=clock;self.operation=operation
    def __getattr__(self,name):return getattr(self.sock,name)
    def sendall(self,data):
        view=memoryview(data)
        while view:
            self.sock.settimeout(_remaining(self.deadline,self.clock,operation=self.operation))
            written=self.sock.send(view[:16384])
            _remaining(self.deadline,self.clock,operation=self.operation)
            if type(written) is not int or not 0<written<=min(16384,len(view)):raise OSError('bounded HTTPS write made no progress')
            view=view[written:]


def _remaining(deadline,clock, *,operation='release acquisition'):
    now=clock()
    if (type(now) not in (int,float) or not math.isfinite(now) or now<0
            or type(deadline) not in (int,float) or not math.isfinite(deadline)):
        raise Conflict(operation+' monotonic deadline unavailable')
    remaining=deadline-now
    if remaining<=0:raise Conflict(operation+' total deadline expired')
    return min(15,remaining)


def _resolve(host,port,deadline,clock, *,run=subprocess.run,operation='release acquisition'):
    if (not isinstance(host,str) or not 0<len(host)<=253 or any(ord(c)<33 for c in host)
            or type(port) is not int or not 0<port<=65535):raise ContractError('invalid release acquisition host/port')
    try:
        answer=run([sys.executable,'-I','-S','-c',RESOLVER,host,str(port)],capture_output=True,check=False,
                   timeout=_remaining(deadline,clock,operation=operation),stdin=subprocess.DEVNULL,close_fds=True)
    except subprocess.TimeoutExpired as exc:raise Conflict(operation+' DNS deadline expired') from exc
    _remaining(deadline,clock,operation=operation)
    if answer.returncode or not isinstance(answer.stdout,bytes) or not 0<len(answer.stdout)<=16384:
        raise ContractError('bounded release DNS resolution failed')
    try:rows=json.loads(answer.stdout)
    except (ValueError,UnicodeError) as exc:raise ContractError('invalid bounded DNS output') from exc
    if not isinstance(rows,list) or not 0<len(rows)<=64:raise ContractError('invalid bounded DNS address count')
    addresses=[]
    for row in rows:
        if (not isinstance(row,list) or len(row)!=5 or type(row[0]) is not int or row[0] not in (socket.AF_INET,socket.AF_INET6)
                or type(row[1]) is not int or row[1]!=socket.SOCK_STREAM or type(row[2]) is not int or row[2]!=socket.IPPROTO_TCP
                or not isinstance(row[3],str) or len(row[3])>253 or not isinstance(row[4],list)):
            raise ContractError('invalid native DNS TCP address')
        address=row[4]
        if (len(address)!=(2 if row[0]==socket.AF_INET else 4) or not isinstance(address[0],str)
                or type(address[1]) is not int or address[1]!=port
                or any(type(item) is not int or not 0<=item<2**32 for item in address[2:])):
            raise ContractError('invalid bounded DNS socket address')
        try:ip=ipaddress.ip_address(address[0])
        except ValueError as exc:raise ContractError('DNS socket address must be a literal IP') from exc
        if '%' in address[0] or ip.version!=(4 if row[0]==socket.AF_INET else 6):raise ContractError('DNS address family differs')
        addresses.append((row[0],row[1],row[2],tuple(address)))
    return addresses


def _connect(address,deadline,clock, *,source_address=None,resolve=_resolve,socket_factory=socket.socket,operation=None):
    host,port=address;last=None
    context=operation or 'release acquisition'
    for family,kind,protocol,destination in resolve(host,port,deadline,clock,**({'operation':context} if operation is not None and resolve is _resolve else {})):
        timeout=_remaining(deadline,clock,operation=context);sock=socket_factory(family,kind,protocol)
        try:
            sock.settimeout(timeout)
            if source_address is not None:sock.bind(source_address)
            sock.connect(destination)
            # HTTPSConnection wraps this socket with native TLS immediately;
            # its timeout is the remaining budget, preserving original-host SNI.
            sock.settimeout(_remaining(deadline,clock,operation=context));return sock
        except BaseException as exc:
            sock.close()
            if not isinstance(exc,OSError):raise
            last=exc
    if last is not None:raise last
    raise ContractError('release DNS returned no TCP addresses')

@contextmanager
def _response(url,deadline,clock, *,connection_factory=http.client.HTTPSConnection,
              method='GET',body=None,headers=None,context=None,expected_status=200,
              expected_peer_sha256=None,operation=None,before_request=None):
    """No redirect/proxy; the reviewed stream bounds status, headers and body."""
    deadline_context=operation or 'release acquisition'
    parts=urlsplit(url)
    if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ContractError('release acquisition requires a fixed HTTPS URL')
    remaining=_remaining(deadline,clock,operation=deadline_context)
    if expected_peer_sha256 is not None:sha256(expected_peer_sha256)
    if method not in ('GET','POST') or (method=='GET' and (body is not None or headers)):
        raise ContractError('invalid bounded HTTPS request method/body')
    if method=='POST':
        if not isinstance(body,bytes) or len(body)>1500000:raise ContractError('bounded HTTPS request body exceeds limit')
        if (not isinstance(headers,dict) or set(headers)-{'Content-Type','Cache-Control','Authorization','X-Device-ID','X-Evidence-Drain-ID'}
                or any(not isinstance(v,str) or not v.isascii() or any(ord(c)<32 or ord(c)==127 for c in v) for v in headers.values())):
            raise ContractError('invalid bounded HTTPS request headers')
    if context is None:
        context=ssl.create_default_context();context.hostname_checks_common_name=False
    if (not isinstance(context,ssl.SSLContext) or not context.check_hostname or context.verify_mode!=ssl.CERT_REQUIRED
            or context.hostname_checks_common_name):raise ContractError('bounded HTTPS requires native original-host SAN verification')
    connection=connection_factory(parts.hostname,parts.port or 443,context=context,timeout=min(15,remaining))
    if connection_factory is http.client.HTTPSConnection:
        connection._create_connection=lambda address,timeout,source_address=None:_connect(address,deadline,clock,source_address=source_address,operation=operation)
    connection.response_class=lambda sock,**kw:http.client.HTTPResponse(_DeadlineSocket(sock,deadline,clock,**({'operation':operation} if operation is not None else {})),**kw)
    try:
        connection.connect()
        remaining=_remaining(deadline,clock,operation=deadline_context)
        if expected_peer_sha256 is not None:
            sock=getattr(connection,'sock',None)
            der=None if sock is None else sock.getpeercert(binary_form=True)
            if not isinstance(der,bytes) or not 0<len(der)<=65536 or digest(der)!=expected_peer_sha256:
                raise Conflict('controller trust changed before enrollment request')
            _remaining(deadline,clock,operation=deadline_context)
        if getattr(connection,'sock',None) is not None:connection.sock.settimeout(remaining)
        if before_request:before_request()
        _remaining(deadline,clock,operation=deadline_context)
        if method=='POST' and getattr(connection,'sock',None) is not None:
            connection.sock=_DeadlineWrites(connection.sock,deadline,clock,operation=deadline_context)
        connection.request(method,parts.path or '/',**({'body':body} if method=='POST' else {}),
            headers={'Accept-Encoding':'identity','Connection':'close',**(headers or {})})
        response=connection.getresponse()
        if expected_status is not None and response.status!=expected_status:raise ContractError('unexpected release acquisition response; redirects are refused')
        # Adapt the same context interface as injected software fixture responses.
        response.geturl=lambda:url
        try:yield response
        finally:response.close()
    finally:connection.close()


def _length(response,limit):
    lengths=response.headers.get_all('Content-Length',[])
    if (len(lengths)!=1 or not lengths[0].isdecimal() or not 0<int(lengths[0])<=limit
            or response.headers.get_all('Transfer-Encoding',[]) or response.headers.get_all('Content-Encoding',[])):
        raise ContractError('unambiguous bounded release response length/encoding required')
    return int(lengths[0])


def fetch_metadata(url,limit, *,clock=time.monotonic):
    if type(limit) is not int or not 0<limit<=64*1024**2:raise ContractError('invalid bounded release byte limit')
    deadline=clock()+45
    with _response(url,deadline,clock) as response:
        size=_length(response,limit);raw=bytearray()
        while len(raw)<size:
            if clock()>=deadline:raise Conflict('release metadata deadline expired')
            chunk=response.read1(min(65536,size-len(raw)))
            if clock()>=deadline:raise Conflict('release metadata deadline expired')
            if not chunk:raise ContractError('incomplete release metadata')
            raw.extend(chunk)
        return bytes(raw)
