import argparse
import hashlib
import struct
import sys
import os
import base64
import shutil
import fnmatch
import tempfile
from collections import OrderedDict

if sys.version > '3':
    long = int

verbose = 0
quiet = False
CHUNK_SIZE = 4096

def unpack_asciiz(file):
    """Unpack a null-terminated string from file object."""
    string = b''
    byte = file.read(1)
    while byte != b'\0':
        string += byte
        byte = file.read(1)
    return string

def padding(hash_value, tlen):
    """Add padding to the hash value and return as long."""
    return long('0x0001' + 'ff'*(tlen - len(hash_value)//2 - 3 - 15) + '00' +
                '3021300906052b0e03021a05000414' + hash_value, 16)

def int_to_bytes(n, length, endian='little'):
    """Convert integer to tuple of bytes."""
    if sys.version > '3':
        return n.to_bytes(length, endian)
    else:
        s = '{:x}'.format(n)
        if len(s) > 2*length:
            raise OverflowError('int too big to convert')
        b = bytearray.fromhex(s.zfill(2*length))
        if endian == 'little':
            b.reverse()
        return tuple(b)

def bytes_to_int(b, endian='little'):
    """Convert tuple of bytes to integer."""
    if sys.version > '3':
        return int.from_bytes(b, endian)
    else:
        if endian == 'little':
            byte_tuple = reversed(b)
        else:
            byte_tuple = b
        s = '0x' + ''.join('{:02x}'.format(x) for x in byte_tuple)
        return long(s, 16)

def _parse_DER(der):
    """Parse a string containing DER encoded ASN.1 data

    return tuple of tuples, integers, strings.
    """
    i = 0
    r = ()
    while i < len(der):
        tag = struct.unpack('B', der[i:i+1])[0]
        i += 1
        l1 = struct.unpack('B', der[i:i+1])[0]
        l2 = l1 & 0x7f
        i += 1
        if l1 & 0x80 != 0:
            l = bytes_to_int(struct.unpack('{}B'.format(l2), der[i:i+l2]),
                             'big')
            i += l2
        else:
            l = l2
        if tag == 0x2:
            r += (bytes_to_int(struct.unpack('{}B'.format(l), der[i:i+l]),
                               'big'), )
        elif tag == 0x3:
            r += (_parse_DER(der[i+1:i+l]), )
        elif tag == 0x5:
            r += (None, )
        elif tag == 0x30:
            r += (_parse_DER(der[i:i+l]), )
        else:
            r += (der[i:i+l], )
        i += l
    return r

class PublicKey:
    """RSA Public Key class"""

    def __init__(self, name=b'', bitlen=1024, public_exponent=0, modulus=0):
        """Initialize PublicKey."""
        self.name = name
        self.bitlen = bitlen
        self.public_exponent = public_exponent
        self.modulus = modulus

    @classmethod
    def from_file(cls, file, form='bi'):
        """Initialize PublicKey from file."""
        if isinstance(file, str):
            with open(file, 'rb') as f:
                return cls._from_file(f, form)
        else:
            return cls._from_file(file, form)

    @classmethod
    def _from_file(cls, file, form):
        if form == 'bi':
            name = unpack_asciiz(file)
            bikey_length, bikey_type, bikey_version, bikey_alg = struct.unpack(
                '<IBB2xI', file.read(12))
            #ALG_ID:
            #http://msdn.microsoft.com/en-us/library/windows/desktop/aa375549(v=vs.85).aspx
            magic = struct.unpack('4s', file.read(4))
            bitlen, public_exponent = struct.unpack('<II', file.read(4 + 4))
            modulus = bytes_to_int(struct.unpack('{0}B'.format(bitlen//8),
                                                 file.read(bitlen//8)))
        elif form == 'der' or form == 'pem':
            if form == 'der':
                b = file.read()
            else:
                b = base64.b64decode(b''.join(file.readlines()[1:-1]))
            d = _parse_DER(b)
            modulus = d[0][1][0][0]
            public_exponent = d[0][1][0][1]
            bitlen = modulus.bit_length()
            name = os.path.basename(file.name).split('.')[0].encode()
        else:
            raise ValueError("{} is not a supported form".format(form))
        return cls(name, bitlen, public_exponent, modulus)

    def export(self, file=None):
        """Export PublicKey to file."""
        if file is None:
            file = '{}.bikey'.format(self.name.decode())
        if isinstance(file, str):
            with open(file, 'wb') as f:
                self._export(f)
        else:
            self._export(file)

    def _export(self, file):
        file.write(struct.pack('<{}ssIBB2xI4sII'.format(len(self.name)),
                               self.name, b'\0', self.bitlen//8 + 20, 6, 2,
                               0x2400, b'RSA1', self.bitlen,
                               self.public_exponent))
        file.write(struct.pack('{}B'.format(self.bitlen//8),
                               *int_to_bytes(self.modulus, self.bitlen//8,
                                             'little')))

    def dump(self):
        """Dump PublicKey values to console."""
        print("Name            : {}".format(self.name.decode()))
        print("Bits            : {}".format(self.bitlen))
        print("Modulus         : {:#x}".format(self.modulus))
        print("Public Exponent : {:#x}".format(self.public_exponent))

class PrivateKey:
    """RSA Private Key class"""

    def __init__(self, public_key=None, private_exponent=0, prime1=0, prime2=0,
                 exponent1=0, exponent2=0, coefficient=0):
        """Initialize PrivateKey."""
        if public_key is None:
            self.public_key = PublicKey()
        else:
            self.public_key = public_key
        self.private_exponent = private_exponent
        self.prime1 = prime1
        self.prime2 = prime2
        self.exponent1 = exponent1
        self.exponent2 = exponent2
        self.coefficient = coefficient

    @classmethod
    def from_file(cls, file, form="bi"):
        """Initialize PrivateKey from file."""
        if isinstance(file, str):
            with open(file, 'rb') as f:
                return cls._from_file(f, form)
        else:
            return cls._from_file(file, form)

    @classmethod
    def _from_file(cls, file, form):
        if form == 'bi':
            public_key = PublicKey.from_file(file, form)
            bitlen = public_key.bitlen
            prime1 = bytes_to_int(struct.unpack('{}B'.format(bitlen//16),
                                                file.read(bitlen//16)))
            prime2 = bytes_to_int(struct.unpack('{}B'.format(bitlen//16),
                                                file.read(bitlen//16)))
            exponent1 = bytes_to_int(struct.unpack('{}B'.format(bitlen//16),
                                                   file.read(bitlen//16)))
            exponent2 = bytes_to_int(struct.unpack('{}B'.format(bitlen//16),
                                                   file.read(bitlen//16)))
            coefficient = bytes_to_int(struct.unpack('{}B'.format(bitlen//16),
                                                     file.read(bitlen//16)))
            private_exponent = bytes_to_int(struct.unpack(
                '{}B'.format(bitlen//8), file.read(bitlen//8)))
        elif form == 'der' or form == 'pem':
            if form == 'der':
                b = file.read()
            else:
                pem_type = file.readline().strip()
                tmp = base64.b64decode(b''.join(file.readlines()[:-1]))
                if pem_type == b"-----BEGIN PRIVATE KEY-----":
                    b = _parse_DER(tmp)[0][2]
                elif pem_type == b"-----BEGIN RSA PRIVATE KEY-----":
                    b = tmp
                else:
                    raise ValueError("unknown PEM format")
            der = _parse_DER(b)
            modulus = der[0][1]
            public_exponent = der[0][2]
            bitlen = modulus.bit_length()
            public_key = PublicKey(
                os.path.basename(file.name).split('.')[0].encode(), bitlen,
                public_exponent, modulus)
            (private_exponent, prime1, prime2, exponent1, exponent2,
             coefficient) = der[0][3:9]
        else:
            raise ValueError("{} is not a supported form".format(form))
        return cls(public_key, private_exponent, prime1, prime2, exponent1,
                   exponent2, coefficient)

    def export(self, file=None):
        """Export PrivateKey to file."""
        if file is None:
            file = '{}.biprivatekey'.format(self.public_key.name.decode())
        if isinstance(file, str):
            with open(file, 'wb') as f:
                self._export(f)
        else:
            self._export(file)

    def _export(self, file):
        pkey = self.public_key
        bitlen = pkey.bitlen
        file.write(struct.pack('<{}ssIBB2xI4sII'.format(len(pkey.name)),
                               pkey.name, b'\0', bitlen//16*9 + 20, 7, 2,
                               0x2400, b'RSA2', bitlen, pkey.public_exponent))
        file.write(struct.pack('{}B'.format(bitlen//8),
                               *int_to_bytes(pkey.modulus, bitlen//8,
                                             'little')))
        file.write(struct.pack('{}B'.format(bitlen//16),
                               *int_to_bytes(self.prime1, bitlen//16,
                                             'little')))
        file.write(struct.pack('{}B'.format(bitlen//16),
                               *int_to_bytes(self.prime2, bitlen//16,
                                             'little')))
        file.write(struct.pack('{}B'.format(bitlen//16),
                               *int_to_bytes(self.exponent1, bitlen//16,
                                             'little')))
        file.write(struct.pack('{}B'.format(bitlen//16),
                               *int_to_bytes(self.exponent2, bitlen//16,
                                             'little')))
        file.write(struct.pack('{}B'.format(bitlen//16),
                               *int_to_bytes(self.coefficient, bitlen//16,
                                             'little')))
        file.write(struct.pack('{}B'.format(bitlen//8),
                               *int_to_bytes(self.private_exponent, bitlen//8,
                                             'little')))

    def dump(self):
        """Dump PrivateKey values to console."""
        self.public_key.dump()
        print("Private Exponent: {:#x}".format(self.private_exponent))
        print("Prime1          : {:#x}".format(self.prime1))
        print("Prime2          : {:#x}".format(self.prime2))
        print("Exponent1       : {:#x}".format(self.exponent1))
        print("Exponent2       : {:#x}".format(self.exponent2))
        print("Coefficient     : {:#x}".format(self.coefficient))

class Bisign:
    """Bisign class"""

    def __init__(self, pkey=None, sig1=0, sig2=0, sig3=0, version=3):
        """Initialize Bisign."""
        if pkey is None:
            self.public_key = PublicKey()
        elif isinstance(pkey, PrivateKey):
            self.public_key = pkey.public_key
        else:
            self.public_key = pkey
        self.version = version
        self.sig1 = sig1
        self.sig2 = sig2
        self.sig3 = sig3

    @classmethod
    def from_file(cls, file):
        """Initialize Bisign from file."""
        if isinstance(file, str):
            with open(file, 'rb') as f:
                return cls._from_file(f)
        else:
            return cls._from_file(file)

    @classmethod
    def _from_file(cls, file):
        public_key = PublicKey.from_file(file)
        len1 = struct.unpack('<I', file.read(4))[0]
        sig1 = bytes_to_int(struct.unpack('{}B'.format(len1), file.read(len1)))
        version, len2 = struct.unpack('<II', file.read(8))
        sig2 = bytes_to_int(struct.unpack('{}B'.format(len2), file.read(len2)))
        len3 = struct.unpack('<I', file.read(4))[0]
        sig3 = bytes_to_int(struct.unpack('{}B'.format(len3), file.read(len3)))
        return cls(public_key, sig1, sig2, sig3, version)

    def export(self, file):
        """Export Bisign to file."""
        if isinstance(file, str):
            with open(file, 'wb') as f:
                self._export(f)
        else:
            self._export(file)

    def _export(self, file):
        self.public_key.export(file)
        len123 = self.public_key.bitlen//8
        file.write(struct.pack('<I{}B'.format(len123), len123,
                               *int_to_bytes(self.sig1, len123, 'little')))
        file.write(struct.pack('<I', self.version))
        file.write(struct.pack('<I{}B'.format(len123), len123,
                               *int_to_bytes(self.sig2, len123, 'little')))
        file.write(struct.pack('<I{}B'.format(len123), len123,
                               *int_to_bytes(self.sig3, len123, 'little')))

    def dump(self):
        """Dump Bisign values to console."""
        self.public_key.dump()
        print("version         : {}".format(self.version))
        print("sig1            : {:#x}".format(self.sig1))
        print("sig2            : {:#x}".format(self.sig2))
        print("sig3            : {:#x}".format(self.sig3))

class PboInfo:
    """PboInfo class"""

    def __init__(self, filename, packing_method=0, original_size=0, reserved=0,
                 timestamp=0, data_size=-1, fp=None):
        """Initialize PboInfo."""
        self.filename = filename
        self.packing_method = packing_method
        self.original_size = original_size
        self.reserved = reserved
        self.timestamp = timestamp
        self.data_size = data_size
        self.fp = fp
        self.data_offset = -1

    def get_data_size(self):
        """Get the file size of the member."""
        if self.fp is None:
            return self.data_size
        else:
            return os.fstat(self.fp.fileno()).st_size

    def get_timestamp(self):
        """Get the timestamp of the member."""
        if self.fp is None:
            return self.timestamp
        else:
            return long(os.path.getmtime(self.fp.name))

    def check_name_hash(self):
        """Check whether member name needs to be hashed."""
        return self.get_data_size() > 0

    def check_file_hash(self, version):
        """Check whether member file needs to be hashed."""
        if version == 2:
            return (self.get_data_size() > 0 and
                    not self.filename.lower().endswith((
                        b'.paa', b'.jpg', b'.p3d', b'.tga', b'.rvmat', b'.lip',
                        b'.ogg', b'.wss', b'.png', b'.rtm', b'.pac', b'.fxy',
                        b'.wrp')))
        elif version == 3:
            return (self.get_data_size() > 0 and
                    self.filename.lower().endswith((
                        b'.sqf', b'.inc', b'.bikb', b'.ext', b'.fsm', b'.sqm',
                        b'.hpp', b'.cfg', b'.sqs', b'.h')))
        else:
            raise ValueError("Unknown signature version {}".format(version))

    def dump(self):
        """Dump PboInfo to console."""
        print(self.filename + "{0} Bytes @ {1:x}".format(self.get_data_size(),
                                                         self.data_offset))


class PboExtFile:
    """file object-like class"""

    def __init__(self, fileobj, pboinfo, mode):
        """Initialize PboExtFile."""
        self.name = pboinfo.filename
        self.fp = fileobj
        self.info = pboinfo
        self.pos = pboinfo.data_offset

    def close(self):
        """Close file."""
        pass
    def __enter__(self):
        return self
    def __exit__(self, exception_type, exception_value, traceback):
        self.close()
    def __del__(self):
        self.close()

    def read(self, n=-1):
        """Read from PboExtFile."""
        self.fp.seek(self.pos)
        data = b''
        read_size = self.info.data_offset + self.info.data_size - self.pos
        if n > -1:
            read_size = min(n, read_size)
        if read_size > 0:
            data = self.fp.read(read_size)
        self.pos = self.fp.tell()
        return data

    def seek(self, offset, whence=0):
        """Seek in PboExtFile."""
        if whence == 0:
            offset += self.info.data_offset
        elif whence == 1:
            offset += self.pos
        elif whence == 2:
            offset += self.info.data_offset + self.info.data_size
        else:
            raise IOError('Invalid argument')
        if offset < self.info.data_offset:
            raise IOError('Invalid argument')
        self.fp.seek(offset, 0)
        self.pos = self.fp.tell()

    def tell(self):
        """Tell within PboExtFile."""
        return self.pos - self.info.data_offset

class PboFile:
    """PBO file class"""

    def __init__(self, header=(b'\0', 0x56657273, 0, 0, 0, 0),
                 header_extension=None, filedict=None, filename=None, fp=None):
        """Initialize PboFile."""
        self.header = header
        if header_extension is None:
            self.header_extension = OrderedDict()
        else:
            self.header_extension = header_extension
        if filedict is None:
            self.filedict = OrderedDict()
        else:
            self.filedict = filedict
        self.filename = filename
        self.fp = fp

    @classmethod
    def from_file(cls, file):
        """Initialize PboFile from file."""
        if verbose > 3:
            print("Reading PBO from file:")
        filedict = OrderedDict()
        if isinstance(file, str):
            filename = file
            fp = open(file, 'rb')
        else:
            fp = file
            filename = file.name
        #header = unpack_asciiz(fp), *struct.unpack('<IIIII', fp.read(20))
        header = (unpack_asciiz(fp),) + struct.unpack('<IIIII', fp.read(20))
        header_extension = OrderedDict()
        s = unpack_asciiz(fp)
        while len(s) != 0:
            header_extension[s] = unpack_asciiz(fp)
            s = unpack_asciiz(fp)
        s = unpack_asciiz(fp)
        if verbose > 3:
            print("Reading PBOinfos")
        while len(s) != 0:
            filedict[s] = PboInfo(s, *struct.unpack('<IIIII', fp.read(20)))
            s = unpack_asciiz(fp)
        empty = fp.read(20)
        data_offset = fp.tell()
        for info in filedict.values():
            info.data_offset = data_offset
            data_offset += info.data_size
        if verbose > 3:
            print("Done")
        return cls(header, header_extension, filedict, filename=filename,
                   fp=fp)

    def export(self, file):
        """Export PboFile to file."""
        if isinstance(file, str):
            with open(file, 'wb') as f:
                self._export(f)
        else:
            self._export(file)

    def _export(self, file):
        hash1 = hashlib.sha1()
        header = struct.pack('<sIIIII', *self.header)
        for k, v in self.header_extension.items():
            header += struct.pack('{}ss{}ss'.format(len(k), len(v)), k, b'\0',
                                  v, b'\0')
        header += struct.pack('s', b'\0')
        for k, v in sorted(self.filedict.items()):
            header += struct.pack('<{}ssIIIII'.format(len(v.filename)),
                                  v.filename, b'\0', v.packing_method,
                                  v.original_size, v.reserved,
                                  v.get_timestamp(), v.get_data_size())
        header += struct.pack('<21s', b'\0'*21)
        hash1.update(header)
        file.write(header)
        for k, v in sorted(self.filedict.items()):
            with self.open(v) as f:
                data = f.read(CHUNK_SIZE)
                while len(data) > 0:
                    hash1.update(data)
                    file.write(data)
                    data = f.read(CHUNK_SIZE)
        if verbose > 3:
            print(hash1.hexdigest())
        file.write(struct.pack('<s20B', b'\0',
                               *int_to_bytes(long(hash1.hexdigest(), 16), 20,
                                             'big')))

    def add(self, name, file):
        """Add a file to the PboFile."""
        dst_name = name.replace(os.path.sep, '\\')
        if dst_name.encode() in self.filedict:
            raise KeyError("{0} exists in PBO".format(dst_name))
        else:
            self.filedict[dst_name.encode()] = PboInfo(dst_name.encode(),
                                                       fp=file)

    def delete(self, name):
        """Remove a file from the PboFile."""
        if isinstance(name, str):
            return self.filedict.pop(name)
        else:
            return self.filedict.pop(name.filename)

    def getinfo(self, name):
        """Get PboInfo by member name."""
        if name in self.filedict:
            return self.filedict[name]
        else:
            raise KeyError("{0} not found in PBO".format(name))

    def close(self):
        """Close the file handle."""
        if self.fp is not None:
            self.fp.close()
    def __enter__(self):
        return self
    def __exit__(self, exception_type, exception_value, traceback):
        self.close()
    def __del__(self):
        self.close()

    def namelist(self):
        """Return member names as list of strings."""
        return list(self.filedict.keys())

    def infolist(self):
        """Return members as list of PboInfos."""
        return list(self.filedict.values())

    def open(self, name, mode='rb'):
        """Open member as file-like object."""
        if isinstance(name, PboInfo):
            pboinfo = name
        else:
            pboinfo = self.getinfo(name)
        if pboinfo.fp is None:
            return PboExtFile(self.fp, pboinfo, mode)
        else:
            return pboinfo.fp

    def _namehash(self):
        """Create hash from member names."""
        namehash = hashlib.sha1()
        for info in sorted(self.infolist(), key=lambda v: v.filename.lower()):
            if info.check_name_hash():
                namehash.update(info.filename.lower())
        return namehash

    def _filehash(self, version):
        """Create hash from member data."""
        filehash = hashlib.sha1()
        nothing = True
        for info in self.infolist():
            if info.check_file_hash(version):
                nothing = False
                with self.open(info) as file:
                    rlen = info.data_size
                    while rlen > 0:
                        filehash.update(file.read(min(CHUNK_SIZE, rlen)))
                        rlen = info.data_size - file.tell()
        if nothing:
            if version == 2:
                filehash.update(b'nothing')
            elif version == 3:
                filehash.update(b'gnihton')
            else:
                raise ValueError("Unknown signature version {}".format(version))
        return filehash

    def hash1(self, file=None):
        """Calculate first hash value."""
        if verbose > 3:
            print("Calculating hash1:")
        if file is None:
            file = self.fp
        oldpos = file.tell()
        file.seek(-21, 2)
        end = file.tell()
        file.seek(0)
        hash1 = hashlib.sha1()
        rlen = end
        while rlen > 0:
            hash1.update(file.read(min(CHUNK_SIZE, rlen)))
            rlen = end - file.tell()
        file.seek(oldpos)
        if verbose > 3:
            print(hash1.hexdigest())
        return hash1

    def hash(self, file=None, version=3):
        """Calculate all 3 hash values."""
        if file is None:
            file = self.fp
        hash1 = self.hash1(file)
        namehash = self._namehash()
        if verbose > 3:
            print("Calculating hash2:")
        hash2 = hashlib.sha1()
        hash2.update(hash1.digest())
        hash2.update(namehash.digest())
        if b'prefix' in self.header_extension:
            hash2.update(self.header_extension[b'prefix'] + b'\\')
        if verbose > 3:
            print(hash2.hexdigest())
        filehash = self._filehash(version)
        if verbose > 3:
            print("Calculating hash3:")
        hash3 = hashlib.sha1()
        hash3.update(filehash.digest())
        hash3.update(namehash.digest())
        if b'prefix' in self.header_extension:
            hash3.update(self.header_extension[b'prefix'] + b'\\')
        if verbose > 3:
            print(hash3.hexdigest())
        return hash1, hash2, hash3

def _sign(args):
    sign(args.key, args.pbo, args.keyform, args.version)

def sign(key_path, pbo_path, keyform='bi', version=3):
    """Create signature file for private key & PBO."""
    pkey = PrivateKey.from_file(key_path, keyform)
    with PboFile.from_file(pbo_path) as p:
        hash1, hash2, hash3 = p.hash(None, version)
    if verbose > 1:
        print("hash1: 0x" + hash1.hexdigest())
        print("hash2: 0x" + hash2.hexdigest())
        print("hash3: 0x" + hash3.hexdigest())
    sig1 = pow(padding(hash1.hexdigest(), pkey.public_key.bitlen//8),
               pkey.private_exponent, pkey.public_key.modulus)
    sig2 = pow(padding(hash2.hexdigest(), pkey.public_key.bitlen//8),
               pkey.private_exponent, pkey.public_key.modulus)
    sig3 = pow(padding(hash3.hexdigest(), pkey.public_key.bitlen//8),
               pkey.private_exponent, pkey.public_key.modulus)
    bsign = Bisign(pkey, sig1, sig2, sig3, version)
    if verbose > 0:
        print("sig1: {:x}".format(sig1))
        print("sig2: {:x}".format(sig2))
        print("sig3: {:x}".format(sig3))
    bsign.export('{:s}.{:s}.bisign'.format(os.path.basename(pbo_path),
                                           pkey.public_key.name.decode()))
    if not quiet:
        print("Signature created")
    sys.exit(0)

def _verify(args):
    verify(args.key, args.pbo, args.sig, args.keyform, args.privin)

def verify(key_path, pbo_path, sig_path, keyform='bi', privin=False):
    """Verify signature for public key & PBO."""
    if privin:
        pkey = PrivateKey.from_file(key_path, keyform).public_key
    else:
        pkey = PublicKey.from_file(key_path, keyform)
    bsign = Bisign.from_file(sig_path)
    with PboFile.from_file(pbo_path) as p:
        hash1, hash2, hash3 = p.hash(None, bsign.version)
    verify1 = (padding(hash1.hexdigest(),
                       pkey.bitlen//8)) == pow(bsign.sig1,
                                               pkey.public_exponent,
                                               pkey.modulus)
    verify2 = (padding(hash2.hexdigest(),
                       pkey.bitlen//8)) == pow(bsign.sig2,
                                               pkey.public_exponent,
                                               pkey.modulus)
    verify3 = (padding(hash3.hexdigest(),
                       pkey.bitlen//8)) == pow(bsign.sig3,
                                               pkey.public_exponent,
                                               pkey.modulus)
    if verbose > 0:
        print("sig1: {}".format(verify1))
        print("sig2: {}".format(verify2))
        print("sig3: {}".format(verify3))
    if verify1 and verify2 and verify3:
        if not quiet:
            print("Signature verified")
        sys.exit(0)
    else:
        if not quiet:
            print("Signature verification failed")
        sys.exit(1)

def key(args):
    """Dump public or private key info to console."""
    if args.pubin:
        pkey = PublicKey.from_file(args.key, args.keyform)
    else:
        pkey = PrivateKey.from_file(args.key, args.keyform)
    if not quiet:
        pkey.dump()
    if args.privout and not args.pubin:
        pkey.export()
    if args.pubin and args.pubout:
        pkey.export()
    elif args.pubout:
        pkey.public_key.export()

def bisign(args):
    """Dump bisign or extract its public key."""
    bsign = Bisign.from_file(args.sig)
    if not quiet:
        bsign.dump()
    if args.pubout:
        bsign.public_key.export()
        if not quiet:
            print("Public key extracted")

def _pbo(args):
    pbo(args.file, args.include, args.exclude, create_pbo=args.create,
        extract_pbo=args.extract, info_pbo=args.info,
        list_pbo=args.list, files=args.files,
        header_extension=args.header_extension,
        recursion=args.recursion, pboprefixfile=args.pboprefixfile,
        update_timestamps=args.update_timestamps)

def pbo(pbo_path, include="*", exclude="", create_pbo=False,
        extract_pbo=False, info_pbo=False, list_pbo=False, files=None,
        header_extension=None, recursion=True, pboprefixfile=True,
        update_timestamps=False):
    """Create, list or extract PBO."""
    if files is None:
        files = []
    if header_extension is None:
        header_extension = []
    if create_pbo:
        pbo_dir = os.path.dirname(pbo_path)
        tmpfile = tempfile.mkstemp(dir=pbo_dir)
        os.close(tmpfile[0])
        with PboFile() as p:
            for f in files:
                if os.path.isfile(f):
                    if pboprefixfile and (f == '$PBOPREFIX$'):
                        with open(f, 'r') as fp:
                            p.header_extension[b'prefix'] = fp.readline().rstrip().encode()
                    else:
                        if (fnmatch.fnmatch(f.lower(), include.lower()) and not
                                fnmatch.fnmatch(f.lower(), exclude.lower())):
                            p.add(f, open(f, 'rb'))
                elif recursion and os.path.isdir(f):
                    files.extend([os.path.join(f, fn) for fn in files(f)])
            for k, v in header_extension:
                p.header_extension[k.encode()] = v.encode()
            with open(tmpfile[1], 'wb') as t:
                p.export(t)
        os.rename(tmpfile[1], pbo_path)
    else:
        with PboFile.from_file(pbo_path) as p:
            if list_pbo:
                for name in p.namelist():
                    if (fnmatch.fnmatch(name.decode().lower(), include.lower())
                            and not fnmatch.fnmatch(name.decode().lower(),
                                                    exclude.lower())):
                        print(name.decode())
            elif extract_pbo:
                pbo_d = pbo_path.replace(".pbo","")
                if not (os.path.exists(pbo_d) or pbo_d == ''):
                    os.makedirs(pbo_d)
                if pboprefixfile and (b'prefix' in p.header_extension):
                    with open('$PBOPREFIX$', 'w') as f:
                        f.write(p.header_extension[b'prefix'].decode())
                for info in p.infolist():
                    if (fnmatch.fnmatch(info.filename.decode().lower(),
                                        include.lower()) and not
                            fnmatch.fnmatch(info.filename.decode().lower(),
                                            exclude.lower())):
                        with p.open(info) as src:
                            print(src.name.decode())
                            dst_name = pbo_d+'\\'+src.name.decode().replace('\\',os.path.sep)
                            dst_dir = os.path.dirname(dst_name)
                            print(type(dst_dir))
                            if not (os.path.exists(dst_dir) or dst_dir == ''):
                                os.makedirs(dst_dir)
                            with open(dst_name, 'wb') as dst:
                                shutil.copyfileobj(src, dst)

            elif info_pbo:
                if len(p.header_extension) > 0:
                    width = max(len(k) for k in p.header_extension.keys())
                    print('Header extensions:')
                    print(18*'-')
                    for k, v in p.header_extension.items():
                        print('{:{width}}: {}'.format(k.decode(), v.decode(),
                                                      width=width))
            else:
                pass

########################################
# Созданные функции специально для AoR #
########################################

def open_pbo(pbo_path, pboprefixfile = True, include="*", exclude="", delete_pbo = False):
    with PboFile.from_file(pbo_path) as p:
        pbo_d = pbo_path.replace(".pbo","")
        if not (os.path.exists(pbo_d) or pbo_d == ''):
          os.makedirs(pbo_d)
        if pboprefixfile and (b'prefix' in p.header_extension):
            with open('$PBOPREFIX$', 'w') as f:
                        f.write(p.header_extension[b'prefix'].decode())
        for info in p.infolist():
            if (fnmatch.fnmatch(info.filename.decode().lower(),include.lower()) and not fnmatch.fnmatch(info.filename.decode().lower(),exclude.lower())):
                with p.open(info) as src:
                    dst_name = pbo_d+'\\'+src.name.decode().replace('\\',os.path.sep)
                    dst_dir = os.path.dirname(dst_name)
                    if not (os.path.exists(dst_dir) or dst_dir == ''):
                        os.makedirs(dst_dir)
                    with open(dst_name, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
    if delete_pbo:
        os.remove(pbo_path)
                        
def create_pbo(pbo_path,pboprefixfile=True,include="*", exclude="",delete_path = False):
    files = os.listdir(pbo_path)
    pbo_dir = os.path.dirname(pbo_path)
    tmpfile = tempfile.mkstemp(dir=pbo_dir)
    os.close(tmpfile[0])
    with PboFile() as p:
        for f in files:
            if os.path.isfile(f"{pbo_path}\\{f}"):
                if pboprefixfile and (f"{pbo_path}\\{f}" == '$PBOPREFIX$'):
                    with open(f"{pbo_path}\\{f}", 'r') as fp:
                        p.header_extension[b'prefix'] = fp.readline().rstrip().encode()
                else:
                    if (fnmatch.fnmatch(f"{pbo_path}\\{f}".lower(), include.lower()) and not fnmatch.fnmatch(f"{pbo_path}\\{f}".lower(), exclude.lower())):
                        p.add(f, open(f"{pbo_path}\\{f}", 'rb'))
            elif os.path.isdir(f"{pbo_path}\\{f}"):
                files.extend([os.path.join(f, fn) for fn in os.listdir(f"{pbo_path}\\{f}")])
        with open(tmpfile[1], 'wb') as t:
                p.export(t)
    if delete_path:
        shutil.rmtree(pbo_path)
    os.rename(tmpfile[1], f"{pbo_path}.pbo")
    