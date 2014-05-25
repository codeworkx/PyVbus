#!/usr/bin/env python2

import ssl
import socket
from time import sleep


MODE_COMMAND = 0
MODE_DATA = 1

DEBUG_HEXDUMP = 0b0001
DEBUG_COMMAND = 0b0010
DEBUG_PROTOCOL = 0b0100

RECOVER_TIME = 1
_HIGHEST_BIT = 0x7F
_FILTER = ''.join([(len(repr(chr(x))) == 3) and chr(x) or '.' for x in range(256)])
_FRAMESIZE = 60
_PAYLOADMAP = {
    # See http://tubifex.nl/wordpress/wp-content/uploads/2013/05/VBus-Protokollspezification_en_270111.pdf#53
    # Did not implement mask
    # Offset, size, factor
    'temp1': (0, 2, 0.1),
    'temp2': (2, 2, 0.1),
    'temp3': (4, 2, 0.1),
    'temp4': (6, 2, 0.1),
    'temp5': (8, 2, 0.1),
    'temprps': (10, 2, 0.1),
    'presrps': (12, 2, 0.1),
    'tempvfs': (14, 2, 0.1),
    'flowvfs': (16, 2, 1),
    'flowv40': (18, 2, 1),
    'unit': (20, 1, 1),
    'pwm1': (22, 1, 1),  # Strange padding?
    'pwm2': (23, 1, 1),
    'pump1': (24, 1, 1),
    'pump2': (25, 1, 1),
    'pump3': (26, 1, 1),
    'pump4': (27, 1, 1),
    'opsec1': (28, 4, 1),
    'opsec2': (32, 4, 1),
    'opsec3': (36, 4, 1),
    'opsec4': (40, 4, 1),
    'error': (44, 2, 1),
    'tatus': (46, 2, 1)
}


class _TERM:
   PURPLE = '\033[95m'
   CYAN = '\033[96m'
   DARKCYAN = '\033[36m'
   BLUE = '\033[94m'
   GREEN = '\033[92m'
   YELLOW = '\033[93m'
   RED = '\033[91m'
   BOLD = '\033[1m'
   UNDERLINE = '\033[4m'
   END = '\033[0m'


def _hexdump(src, length=16):
    result = []
    for i in xrange(0, len(src), length):
        s = src[i:i+length]
        hexa = ' '.join(["%02X" % ord(x) for x in s])
        printable = s.translate(_FILTER)
        result.append("%04X   %-*s   %s\n" % (i, length*3, hexa, printable))
    return "Len %iB\n%s" % (len(src), ''.join(result))


class VBUSException(Exception):
    def __init__(self, *args):
        super.__init__(*args)


class VBUSResponse(object):
    def __init__(self, line):
        assert len(line) > 2
        self.positive = line[0] == "+"
        spl = line[1:].split(":", 1)
        self.type = spl[0]
        self.message = None if len(spl) == 1 else spl[1][:1]


class VBUSPayload(object):
    def __init__(self, raw):
        pass


class VBUSConnection(object):
    def __init__(self, host, port=7053, password="", debugmode=0b0000):
        assert isinstance(port, int)
        assert isinstance(host, str)
        assert isinstance(password, str)
        assert isinstance(debugmode, int)
        self.host = host
        self.port = port
        self.password = password or False
        self.debugmode = debugmode

        self._mode = MODE_COMMAND
        self._sock = None
        self._buffer = []

    def connect(self, sslsock=False):
        assert not self._sock
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sslsock:  # Unlikely that we'll ever connect to the VBUS using an ssl socket but "why not?"
            self._sock = ssl.wrap_socket(self._sock)
        self._sock.connect((self.host, self.port))
        assert VBUSResponse(self._lrecv()).type == "HELLO"
        if self.password:
            self.authenticate()

    def authenticate(self):
        assert self.password
        assert self._mode == MODE_COMMAND
        self._lsend("PASS %s" % self.password)
        resp = VBUSResponse(self._lrecv())
        if not resp.positive:
            raise VBUSException("Could not authenticate: %s" % resp.message)

    def data(self):
        assert self._sock
        if self._mode is not MODE_DATA:
            self._lsend("DATA")

            resp = VBUSResponse(self._lrecv())
            if not resp.positive:
                raise VBUSException("Could create a data stream: %s" % resp.message)
            self._mode = MODE_DATA

        while True:
            # Wait till we get the correct protocol
            for d in self._brecv().split(chr(0xAA)):
                # Check the protocol
                if self._getbytes(d, 4, 5) is not 0x10:
                    continue

                # Are we getting a payload?
                if self._getbytes(d, 5, 7) is not 0x100:
                    continue

                if self.debugmode & DEBUG_PROTOCOL:
                    print "Source map: 0X%02X" % self._getbytes(d, 2, 4)

                # Is the checksum valid?
                if self._checksum(d[0:8]) is not self._getbytes(d, 8, 9):
                    if self.debugmode & DEBUG_PROTOCOL:
                        print "Invalid checksum: got %02X expected %02X" % \
                              (self._checksum(d[0:8]), self._getbytes(d, 8, 9))
                    continue

                # Check payload length
                frames = self._getbytes(d, 7, 8)
                payload = d[9:9 + (6*frames)]
                if len(payload) is not 6*frames:
                    if self.debugmode & DEBUG_PROTOCOL:
                        print "Unexpected payload length: %i != %i" % \
                              (len(payload), 6*frames)
                    continue

                r = self._parsepayload(payload)
                if r:
                    return r
            # The vbus freaks out when you send too many requests
            # This can be solved by just waiting
            sleep(RECOVER_TIME)

    def getmode(self):
        return self._mode

    def _parsepayload(self, payload):
        data = []
        if len(payload) is not _FRAMESIZE and False:
            if self.debugmode & DEBUG_PROTOCOL:
                print "Payload size mismatch: expected %i got %i", _FRAMESIZE, len(payload)
            return None

        if True in [ord(i) > _HIGHEST_BIT for i in payload]:
            if self.debugmode & DEBUG_PROTOCOL:
                print "Found highest byte discarding payload"
                print ' '.join(
                    "%02X" % ord(i) if ord(i) <= _HIGHEST_BIT else "%s%02X%s" % (_TERM.RED, ord(i), _TERM.END)
                    for i in payload
                )
            return None

        for i in range(len(payload)/6):
            frame = payload[i*6:i*6+6]
            if self.debugmode & DEBUG_PROTOCOL:
                print "Frame: %s" % ' '.join("%02X" % ord(i) for i in frame)

            # Check frame checksum
            if ord(frame[5]) is not self._checksum(frame[:-1]):
                if self.debugmode & DEBUG_PROTOCOL:
                    print "Frame checksum mismatch: ", ord(frame[5]), self._checksum(frame[:-1])
                return None

            septet = ord(frame[4])
            for j in range(4):
                if septet & (1 << j):
                    data.append(chr(ord(frame[j]) | 0x80))
                else:
                    data.append(frame[j])

        vals = {}
        for i, rng in _PAYLOADMAP.items():
            vals[i] = self._getbytes(data, rng[0], rng[0] + rng[1])

            # Temperatures can be negative (using two's complement)
            if i.startswith('temp'):
                bits = (rng[1]) * 8
                if vals[i] >= 1 << (bits - 1):
                    vals[i] -= 1 << bits
            # Apply factor
            vals[i] *= rng[2]

        if self.debugmode & DEBUG_PROTOCOL:
            print vals
        return vals

    @staticmethod
    def _checksum(data):
        return reduce(lambda chk, b: ((chk - ord(b)) % 0x100) & 0x7F, data, 0x7F)

    @staticmethod
    def _getbytes(data, begin, end):
        return sum([ord(b) << (i*8) for i, b in enumerate(data[begin:end])])

    def _lrecv(self):
        c, s = '', ''
        while c != '\n':
            c = self._sock.recv(1)
            if c == '':
                break
            s += c
        s = s.strip('\r\n')
        if self.debugmode & DEBUG_COMMAND:
            print "< " + s
        return s

    def _brecv(self, n=1024):
        d = self._sock.recv(n)
        if self.debugmode & DEBUG_HEXDUMP:
            print _hexdump(d)
        return d

    def _lsend(self, s):
        if self.debugmode & DEBUG_COMMAND:
            print "> " + s
        self._sock.send(s + "\r\n")

    def _bsend(self, s):
        if self.debugmode & DEBUG_HEXDUMP:
            print _hexdump(s)
        self._sock.send(s)