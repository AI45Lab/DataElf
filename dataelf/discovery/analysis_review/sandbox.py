"""Linux-enforced reviewer computation: snapshot read-only, scratch writable.

No shell tool is exposed. Even arbitrary Python in the compute tool inherits
chroot + privilege drop (or supported Landlock) and a seccomp denylist.
Unavailable isolation fails closed; there is no unrestricted fallback.
"""
import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import struct
import sys


def isolate(snapshot, scratch):
    if sys.platform != 'linux' or platform.machine() not in ('x86_64','amd64'):
        raise RuntimeError('REVIEW_SANDBOX_UNAVAILABLE: supported Linux x86_64 required')
    libc=ctypes.CDLL(None,use_errno=True)
    def call(number,*args):
        result=libc.syscall(number,*args)
        if result < 0:raise OSError(ctypes.get_errno(),os.strerror(ctypes.get_errno()))
        return result
    abi=call(444,0,0,1)
    handled=(1<<13)-1
    if abi>=2:handled|=1<<13
    if abi>=3:handled|=1<<14
    attr=ctypes.create_string_buffer(struct.pack('Q',handled))
    fd=call(444,ctypes.byref(attr),8,0)
    read=(1<<0)|(1<<2)|(1<<3)
    paths=[(Path(snapshot).resolve(),read),(Path(scratch).resolve(),handled),
           (Path(sys.prefix).resolve(),read),(Path(sys.base_prefix).resolve(),read)]
    paths.extend((Path(p).resolve(),read) for p in ['/usr','/lib','/lib64','/etc/ld.so.cache','/etc/localtime'] if Path(p).exists())
    paths.append((Path('/dev/null'),(1<<1)|(1<<2)))
    try:
        for path,rights in paths:
            parent=os.open(path,os.O_PATH|os.O_CLOEXEC)
            try:
                if not path.is_dir():rights &= (1<<0)|(1<<1)|(1<<2)|(1<<14)
                rule=ctypes.create_string_buffer(struct.pack('Qi',rights,parent))
                call(445,fd,1,ctypes.byref(rule),0)
            finally:os.close(parent)
        if libc.prctl(38,1,0,0,0):raise OSError('no_new_privs failed')
        call(446,fd,0)
    finally:os.close(fd)
    install_seccomp(libc)


def install_seccomp(libc):
    class Filter(ctypes.Structure):
        _fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint32)]
    class Program(ctypes.Structure):
        _fields_=[('len',ctypes.c_ushort),('filter',ctypes.POINTER(Filter))]
    # Deny alternate syscall ABI, network, ptrace and cross-process tampering.
    instructions=[(0x20,0,0,4),(0x15,1,0,0xc000003e),(0x06,0,0,0x80000000),
                  (0x20,0,0,0),(0x35,0,1,0x40000000),(0x06,0,0,0x50000|errno.EPERM)]
    for nr in [41,53,56,57,58,59,62,101,161,165,200,234,272,304,308,310,311,321,322,424,435,438]:
        instructions.extend([(0x15,0,1,nr),(0x06,0,0,0x50000|errno.EPERM)])
    instructions.append((0x06,0,0,0x7fff0000))
    filters=(Filter*len(instructions))(*(Filter(*x) for x in instructions));program=Program(len(filters),filters)
    if libc.prctl(22,2,ctypes.byref(program),0,0):raise OSError('seccomp filter failed')


def main():
    request=json.load(sys.stdin)
    # Preload calculation libraries before entering a filesystem with no host
    # files at all. Optional libraries may have unavailable lazy imports; tools
    # report that explicitly rather than executing outside isolation.
    import csv, math, statistics, decimal, datetime, re, collections, itertools, sqlite3
    for name in ('numpy', 'pandas'):
        try:__import__(name)
        except ImportError:pass
    if request.get('jail'):
        if sys.platform != 'linux' or platform.machine() not in ('x86_64','amd64') or os.geteuid()!=0:
            raise RuntimeError('REVIEW_SANDBOX_UNAVAILABLE: chroot and privilege drop required')
        libc=ctypes.CDLL(None,use_errno=True)
        os.chroot(request['jail']);os.chdir('/scratch')
        os.setgroups([]);os.setgid(65534);os.setuid(65534)
        if libc.prctl(38,1,0,0,0):raise OSError('no_new_privs failed')
        install_seccomp(libc)
        os.closerange(3,65536)
        snapshot=Path('/input');scratch=Path('/scratch')
    else:
        snapshot=Path(request['snapshot']).resolve();scratch=Path(request['scratch']).resolve()
        os.chdir(scratch)
        isolate(snapshot,scratch)
    namespace={'SNAPSHOT':snapshot,'SCRATCH':scratch,'__name__':'__main__'}
    exec(compile(request['code'],'<reviewer-computation>','exec'),namespace)


if __name__=='__main__':main()
