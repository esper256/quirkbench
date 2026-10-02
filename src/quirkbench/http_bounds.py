"""Native HTTP streams with cumulative header and absolute read budgets."""
import io

MAX_HEADERS=32768

class BoundedHTTPError(OSError):
    pass


class _DeadlineRaw(io.RawIOBase):
    def __init__(self,sock,deadline,clock):
        self.sock=sock;self.deadline=deadline;self.clock=clock
        self.stream=sock.makefile('rb',buffering=0)
    def readable(self):return True
    def readinto(self,buffer):
        remaining=self.deadline-self.clock()
        if remaining<=0:raise BoundedHTTPError('enrollment exchange exceeded deadline')
        self.sock.settimeout(min(15,remaining))
        count=self.stream.readinto(buffer)
        if self.clock()>=self.deadline:raise BoundedHTTPError('enrollment exchange exceeded deadline')
        return count
    def close(self):
        try:self.stream.close()
        finally:super().close()


class _HeaderReader(io.BufferedReader):
    def __init__(self,raw):
        super().__init__(raw);self.header_bytes=0
    def readline(self,size=-1):
        line=super().readline(min(MAX_HEADERS+1,size) if size>=0 else MAX_HEADERS+1)
        self.header_bytes+=len(line)
        if self.header_bytes>MAX_HEADERS:
            raise BoundedHTTPError('enrollment response headers exceed byte limit')
        return line


class _DeadlineSocket:
    def __init__(self,sock,deadline,clock):self.sock=sock;self.deadline=deadline;self.clock=clock
    def makefile(self,*a,**k):return _HeaderReader(_DeadlineRaw(self.sock,self.deadline,self.clock))



class AcceptedSockets:
    """Bounded servers close established TLS sessions when their owner exits."""
    def __init__(self):
        import threading
        self.lock=threading.Lock();self.connections=set();self.closing=False
    def add(self,connection):
        with self.lock:
            if self.closing:
                connection.close();raise OSError('server publication ended')
            self.connections.add(connection)
    def discard(self,connection):
        with self.lock:self.connections.discard(connection)
    def close(self):
        import socket
        with self.lock:
            self.closing=True;connections=list(self.connections);self.connections.clear()
        for connection in connections:
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            connection.close()
