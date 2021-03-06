#!/usr/bin/env python3

import ssl
import socket
from time import sleep
from functools import reduce

MODE_COMMAND = 0
MODE_DATA = 1

DEBUG_HEXDUMP = 0b0001
DEBUG_COMMAND = 0b0010
DEBUG_PROTOCOL = 0b0100
DEBUG_ALL = 0b1111

RECOVER_TIME = 1
_FRAME_COUNT = 9
_HIGHEST_BIT = 0x7F
_FILTER = ''.join([(len(repr(chr(x))) == 3) and chr(x) or '.' for x in range(256)])
_PAYLOADSIZE = 54
_PAYLOADMAP = {

    # Sonnenkraft SKSC2 HE 0x4214
    # Offset, size, factor
    'temp1': (0, 2, 0.1), # Temperature S1
    'temp2': (2, 2, 0.1), # Temperature S2
    'temp3': (4, 2, 0.1), # Temperature S3
    'temp4': (6, 2, 0.1), # Temperature S4
    'rel1': (8, 1, 1), # Relais 1
    'rel2': (9, 1, 1), # Relais 2
    'error': (10, 1, 1), # Error mask
    'rel1oph': (12, 2, 1), # Operating hours Relais 1
    'rel2oph': (14, 2, 1), # Operating hours Relais 2
    'heat': (16, 6, 1), # Amount of heat
    'temp5': (24, 2, 0.1), # Temperature VFD1
    'flow5': (26, 2, 1), # Volumetric flow rate VFD1
    'voltage': (32, 1, 0.1), # Voltage

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
    for i in range(0, len(src), length):
        s = src[i:i + length]
        hexa = ' '.join(["%02X" % x for x in s])
        result.append("%04X   %-*s \n" % (i, length * 3, hexa))
    return "Len %iB\n%s" % (len(src), ''.join(result))


class VBUSException(Exception):
    def __init__(self, *args):
        super.__init__(*args)


class VBUSResponse(object):
    """
    A response object that is generated by
    the VBUSConnection when in COMMAND mode.
    """
    def __init__(self, line):
        assert len(line) > 2
        self.positive = str(chr(line[0])) == '+'
        
        # Convert byte-object to string
        str_line = ''
        for b in line:
            str_line += str(chr(b))

        print('< ', str_line)
        self.type = str_line


class VBUSConnection(object):
    def __init__(self, host, port=7053, password="", debugmode=0b0000):
        """

        :param host: The IP/DNS of the vbus host
        :param port: The port the vbus is listening to
        :param password: The optional password. Use "" or None for no password
        :param debugmode: The debug flags to use

        :type host: str
        :type port: int
        :type password: str
        :type debugmode: int
        """
        password = "" if password in [None, False] else password
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
        """
        Connects to the VBUS. It will try to authenticate 
        if a password has been set.

        :raise VBUSException:
        :type sslsock: bool
        :param sslsock: Use ssl?
        """
        assert not self._sock
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sslsock:  # Unlikely that we'll ever connect to the VBUS using an ssl socket but "why not?"
            self._sock = ssl.wrap_socket(self._sock)
        self._sock.connect((self.host, self.port))
        assert VBUSResponse(self._lrecv()).type == "+HELLO"
        if self.password:
            if not self.authenticate():
                raise VBUSException("Could not authenticate")

    def authenticate(self):
        """
        Authenticate with the server using the set password. This
        will return if the authentication attempt was acccepted.
        :rtype : bool
        """
        assert self.password
        assert self._mode == MODE_COMMAND
        self._lsend("PASS %s" % self.password)
        resp = VBUSResponse(self._lrecv())
        return resp.positive

    def data(self, payloadmap=_PAYLOADMAP, framecount=_FRAME_COUNT, payloadsize=_PAYLOADSIZE):
        """
        Listen to the server and get some data.

        :param payloadmap:
        :param payloadsize: The size of a
        :param framecount: The amount of desired frames
        :return: The requested data

        :type payloadmap: dict
        :type payloadsize: int
        :type framecount: int
        :rtype : dict
        """
        payloadmap = payloadmap.copy()
        #assert isinstance(payloadmap, dict)
        assert isinstance(payloadsize, int)
        assert self._sock
        if self._mode is not MODE_DATA:
            self._lsend("DATA")

            resp = VBUSResponse(self._lrecv())
            if not resp.positive:
                raise VBUSException("Could create a data stream: %s" % resp.message)
            self._mode = MODE_DATA

        while True:
            for data in self._brecv().split(b"\xaa"):
                if len(data) > 5:

                    # Wait till we get the correct protocol               
                    if self._getbytes(data, 4, 5) is not 0x10:
                        continue

                    if self.debugmode & DEBUG_PROTOCOL:
                        print('-----------------------------------------')
                        print("Src: 0X%02X" % self._getbytes(data, 0, 2))
                        print("Dst: 0X%02X" % self._getbytes(data, 2, 4))
                        print('Protocol version:', hex(data[4]))

                    if len(data) > 8:
                        if self.debugmode & DEBUG_PROTOCOL:
                            print("Cmd: 0X%02X" % self._getbytes(data, 5, 7))

                        # Are we getting a payload?             
                        if self._getbytes(data, 5, 7) is not 0x100:
                           continue

                        if self.debugmode & DEBUG_PROTOCOL:
                            print("Source map: 0X%02X" % self._getbytes(data, 2, 4))

                        # Is the checksum valid?
                        if self._checksum(data[0:8]) is not data[8]:
                            if self.debugmode & DEBUG_PROTOCOL:    
                                print("Invalid checksum: got %d expected %d" % \
                                      (self._checksum(data[0:8]), data[8]))
                            continue

                        # Check payload length
                        frames = data[7]
                        if self.debugmode & DEBUG_PROTOCOL:
                            print('Frames:', frames)
                        p_end = 9 + (6 * frames)
                        payload = data[9:p_end]
                        if len(payload) is not 6 * frames:
                            if self.debugmode & DEBUG_PROTOCOL:
                                print("Unexpected payload length: %i != %i" % \
                                      (len(payload), 6 * frames))
                            continue

                        r = self._parsepayload(payload, payloadmap, payloadsize, framecount)
                        if r:
                            print(r)
                            return r

                # The vbus freaks out when you send too many requests
                # This can be solved by just waiting
                sleep(RECOVER_TIME)

    def getmode(self):
        """
        Returns the current mode
        """
        return self._mode

    def _parsepayload(self, payload, payloadmap, payloadsize, framecount):
        data = []
        if len(payload) is not payloadsize and False:
            if self.debugmode & DEBUG_PROTOCOL:
                print("Payload size mismatch: expected %i got %i", payloadsize, len(payload))
            return None

        if True in [i > _HIGHEST_BIT for i in payload]:
            if self.debugmode & DEBUG_PROTOCOL:
                print("Found highest byte discarding payload")
                print(' '.join(
                    "%02X" % i if i <= _HIGHEST_BIT else "%s%02X%s" % (_TERM.RED, i, _TERM.END)
                    for i in payload
                ))
            return None

        if (len(payload) / 6) != framecount:
            if self.debugmode & DEBUG_PROTOCOL:
                print("Invalid frame count %d (%d)" % (framecount, len(payload) / 6))
            return None

        for i in range(int(len(payload) / 6)):
            frame = payload[i * 6:i * 6 + 6]
            if self.debugmode & DEBUG_PROTOCOL:
                print("Frame %i: %s" % (i, ' '.join("%02X" % i for i in frame)))

            # Check frame checksum
            if frame[5] is not self._checksum(frame[:-1]):
                if self.debugmode & DEBUG_PROTOCOL:
                    print("Frame checksum mismatch: ", frame[5], self._checksum(frame[:-1]))
                return None

            septet = frame[4]
            for j in range(4):
                if septet & (1 << j):
                    data.append(frame[j] | 0x80)
                else:
                    data.append(frame[j])

        vals = {}
        for i, rng in payloadmap.items():
            vals[i] = self._getbytes(data, rng[0], rng[0] + rng[1])

            # Temperatures can be negative (using two's complement)
            if i.startswith('temp'):
                bits = (rng[1]) * 8
                if vals[i] >= 1 << (bits - 1):
                    vals[i] -= 1 << bits

            # Apply factor
            vals[i] = vals[i] * rng[2]

        if self.debugmode & DEBUG_PROTOCOL:
            print(vals)
        return vals

    @staticmethod
    def _checksum(data):
        crc = 0x7F
        for d in data:
            crc = (crc - d) & 0x7F
        return crc

    @staticmethod
    def _getbytes(data, begin, end):
        return sum([b << (i * 8) for i, b in enumerate(data[begin:end])])

    def _lrecv(self):
        c = b''
        s = b''
        while c != b'\n':
            c = self._sock.recv(1)
            if c == '':
                break
            if c != b'\n':
                s += c
        if self.debugmode & DEBUG_COMMAND:
            print("< %s" % s)
        return s

    def _brecv(self, n=1024):
        d = self._sock.recv(n)

        while d.count(b"\xaa") < 4:
            d += self._sock.recv(n)

        if self.debugmode & DEBUG_HEXDUMP:
            print(_hexdump(d))

        return d

    def _lsend(self, s):
        if self.debugmode & DEBUG_COMMAND:
            print("> " + s)
        msg = s + "\r\n"
        self._sock.send(msg.encode("utf-8"))

    def _bsend(self, s):
        if self.debugmode & DEBUG_HEXDUMP:
            print(_hexdump(s))
        self._sock.send(s)



