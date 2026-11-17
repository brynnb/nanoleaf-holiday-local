#!/usr/bin/env python3
"""NL71K2 local HTTP LTPDUv3 pairing and OpenAPI recovery.

HTTP transport, group=2048, numeric password bytes and inner endpoints are
from static inspection of Nanoleaf Desktop 3.0.2.
SRP/session encoding adapted from Jaano/nanoleaf_lights nl_ltpdu_v3.py at
f272066d0f8290f050cbdd9dca966c4dddaedd08, reviewed before execution.
No cloud access. No credentials logged. Requests are serialized, without
automatic retries after encrypted exchanges (the counter may be uncertain).

MIT License -- Copyright (c) 2026 Jaano
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import binascii
import hashlib
import hmac
import http.client
import re
import secrets
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

MODEL = 'NL71K2'
FIRMWARE = '5.2.59'
N = int('''ac6bdb41324a9a9bf166de5e1389582faf72b6651987ee07fc3192943db56050a37329
cbb4a099ed8193e0757767a13dd52312ab4b03310dcd7f48a9da04fd50e8083969edb
767b0cf6095179a163ab3661a05fbd5faaae82918a9962f0b93b855f97993ec975eeaa
80d740adbf4ff747359d041d5c33ea71d281e446b14773bca97b43a23fb801676bd2
07a436c6481f1d2b9078717461a5b9d32e688f87748544523b524b0d57d5ea77a277
5d2ecfa032cfbdbf52fb3786160279004e57ae6af874e7303ce53299ccc041c7bc308
d82a5698f3a8d0c38271ae35f8e9dbfbb694b5c803d89f7ae435de236d525f54759b
65e372fcd68ef20fa7111f9e4aff73'''.replace('\n', ''), 16)


def ib(i):
    return i.to_bytes(max(1, (i.bit_length() + 7) // 8), 'big')


def sha(*b):
    return hashlib.sha256(b''.join(b)).digest()


def tlv(tag, data, tag_size=1):
    return tag.to_bytes(tag_size, 'big') + len(data).to_bytes(2, 'big') + data


def fields(data, tag_size=1, length_size=2):
    result = {}
    p = 0
    while p < len(data):
        h = tag_size + length_size
        if p + h > len(data):
            raise ValueError('Truncated TLV header')
        t = int.from_bytes(data[p:p+tag_size], 'big')
        n = int.from_bytes(data[p+tag_size:p+h], 'big')
        p += h
        if p + n > len(data) or t in result:
            raise ValueError('Invalid or duplicate TLV')
        result[t] = data[p:p+n]
        p += n
    return result


class SecureCipher:
    def __init__(self, version, shared):
        if version not in (0x300, 0x301, 0x302):
            raise ValueError('Unsupported secure version')
        material = sha(shared)
        self.version, self.counter, self.tail = version, 0, material[16:24]
        self.ctr = Cipher(algorithms.AES(material[:16]), modes.CTR(b'\0'*8+self.tail)).encryptor() if version == 0x300 else None
        self.ccm = AESCCM(material[:16], tag_length=4) if not self.ctr else None

    def crypt(self, data, decrypt=False):
        if self.ctr:
            if decrypt:
                result = self.ctr.update(data)
                if len(result) < 2 or not hmac.compare_digest(binascii.crc_hqx(result[:-2], 0).to_bytes(2, 'big'), result[-2:]):
                    raise ValueError('CRC mismatch; session must be discarded')
                result = result[:-2]
            else:
                result = self.ctr.update(data+binascii.crc_hqx(data, 0).to_bytes(2, 'big'))
        else:
            nonce = self.counter.to_bytes(5 if self.version == 0x301 else 4, 'big') + self.tail
            result = (self.ccm.decrypt if decrypt else self.ccm.encrypt)(nonce, data, None)
        self.counter += 1
        return result


class Session:
    def __init__(self, host, expected_id=None, timeout=15):
        self.expected_id = expected_id
        self.client_id = 65535
        self.cipher = None
        self.identified = False
        self.auth_stage = None
        # NL71K2 rejects the proof on a new HTTP connection. Keep the nlinfo,
        # SRP and encrypted requests on the same connection, as the vendor does.
        self.connection = http.client.HTTPConnection(host, 12566, timeout=timeout)

    def identify(self):
        client_id = self.client_id
        self.client_id = 65535
        info = fields(self.exchange('nlinfo',method='GET'),length_size=1)
        self.client_id = client_id
        if (info.get(0) != MODEL.encode() or info.get(2) != FIRMWARE.encode()
                or not re.fullmatch(rb'[0-9a-fA-F]{16}', info.get(1, b''))
                or (self.expected_id is not None and info[1].decode().upper() != self.expected_id)):
            self.connection.close()
            raise ValueError(f'Expected {MODEL} running firmware {FIRMWARE} with the selected device ID')
        self.identified = True
        return info

    def exchange(self, endpoint, data=b'', method='POST'):
        c = self.connection
        try:
            c.request(method, '/'+endpoint, body=data, headers={'ClientId':str(self.client_id), 'Content-Type':'application/octet-stream'})
            r = c.getresponse()
            body = r.read(65537)
            if len(body)>65536:
                raise ValueError('Oversized device response')
            if r.status != 200:
                raise RuntimeError(f'{endpoint}: HTTP {r.status}; {len(body)} response bytes')
            self.client_id = int(r.getheader('ClientId', str(self.client_id)))
            return body
        except BaseException:
            c.close()
            raise

    def handshake(self, username, password, auth_type):
        # A fresh connection must first GET nlinfo; starting with a saved
        # ClientId directly returned HTTP 400 on the actual 5.2.59 device.
        if self.expected_id is None:
            raise ValueError('Select a device before authenticating')
        if not self.identified:
            self.identify()
        a = int.from_bytes(secrets.token_bytes(256), 'big')
        A = ib(pow(2, a, N))
        self.auth_stage = 'challenge'
        f = fields(self.exchange('nlsecure', tlv(1,bytes([auth_type]))+tlv(3,A)+tlv(7,b'\x02')))
        salt, B = f[2], ib(int.from_bytes(f[3], 'big'))
        b = int.from_bytes(B,'big')
        u = int.from_bytes(sha(A,B),'big')
        if b % N == 0 or not u:
            raise ValueError('Invalid SRP challenge')
        k = int.from_bytes(sha(ib(N),b'\x02'),'big')
        x = int.from_bytes(sha(salt,sha(username.encode(),b':',password)),'big')
        K = sha(ib(pow((b-k*pow(2,x,N))%N, a+u*x, N)))
        xor = bytes(x^y for x,y in zip(sha(ib(N)),sha(b'\x02')))
        proof = sha(xor,sha(username.encode()),salt,A,B,K)
        self.auth_stage = 'proof'
        reply = fields(self.exchange('nlsecure',tlv(4,proof)))
        if not hmac.compare_digest(reply.get(4,b''), sha(A,proof,K)):
            raise ValueError('Server proof did not verify; stopping')
        if len(f.get(17,b'')) != 2:
            raise ValueError('Missing secure protocol version')
        version = int.from_bytes(f[17],'big')
        if version != 0x302:
            raise ValueError('This tool only supports LTPDU secure version 0302')
        self.cipher = SecureCipher(version,K)
        self.auth_stage = 'authenticated'

    def encrypted(self, endpoint, data):
        try:
            return self.cipher.crypt(self.exchange(endpoint,self.cipher.crypt(data)),True)
        except BaseException:
            self.cipher = None
            raise

    def request(self, endpoint, data=b'', method=0):
        plain = tlv(1,endpoint.encode(),2)+tlv(2,data,2)+tlv(4,bytes([method]),2)
        result = fields(self.encrypted('nlltpdu',plain),2)
        if result.get(1) != endpoint.encode() or not result.get(3) or result[3][0] != 0:
            raise ValueError(f'{endpoint}: request failed; status {result.get(3,b"?")[:1].hex()}')
        return result[3][1:]
