"""Windows-only, owner-local IPC with OS process identity and bounded I/O.

No TCP/HTTP listener, network credential or inferred GUI endpoint. A startup record
is local same-user trust, not an attestation against a malicious same-user process.
"""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import time

k = C.WinDLL('kernel32', use_last_error=True)
a = C.WinDLL('advapi32', use_last_error=True)
INVALID = C.c_void_p(-1).value
MAX_FRAME = 256 * 1024

class FT(C.Structure):
    _fields_ = [('low', W.DWORD), ('high', W.DWORD)]

class OVERLAPPED(C.Structure):
    _fields_ = [('Internal', C.c_size_t), ('InternalHigh', C.c_size_t),
                ('Offset', W.DWORD), ('OffsetHigh', W.DWORD), ('hEvent', W.HANDLE)]

class SA(C.Structure):
    _fields_ = [('nLength', W.DWORD), ('lpSecurityDescriptor', C.c_void_p), ('bInheritHandle', W.BOOL)]

class JOB_BASIC_LIMIT(C.Structure):
    _fields_ = [('PerProcessUserTimeLimit', C.c_longlong), ('PerJobUserTimeLimit', C.c_longlong),
                ('LimitFlags', W.DWORD), ('MinimumWorkingSetSize', C.c_size_t), ('MaximumWorkingSetSize', C.c_size_t),
                ('ActiveProcessLimit', W.DWORD), ('Affinity', C.c_size_t), ('PriorityClass', W.DWORD), ('SchedulingClass', W.DWORD)]

class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_ulonglong) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]

class JOB_EXTENDED_LIMIT(C.Structure):
    _fields_ = [('BasicLimitInformation', JOB_BASIC_LIMIT), ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', C.c_size_t), ('JobMemoryLimit', C.c_size_t),
                ('PeakProcessMemoryUsed', C.c_size_t), ('PeakJobMemoryUsed', C.c_size_t)]

def proto(dll, name, result, args):
    fn = getattr(dll, name); fn.restype = result; fn.argtypes = args; return fn

CloseHandle = proto(k, 'CloseHandle', W.BOOL, [W.HANDLE])
OpenProcess = proto(k, 'OpenProcess', W.HANDLE, [W.DWORD, W.BOOL, W.DWORD])
GetProcessTimes = proto(k, 'GetProcessTimes', W.BOOL, [W.HANDLE] + [C.POINTER(FT)] * 4)
QueryFullProcessImageNameW = proto(k, 'QueryFullProcessImageNameW', W.BOOL, [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)])
GetCurrentProcess = proto(k, 'GetCurrentProcess', W.HANDLE, [])
OpenProcessToken = proto(a, 'OpenProcessToken', W.BOOL, [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)])
GetTokenInformation = proto(a, 'GetTokenInformation', W.BOOL, [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)])
ConvertSidToStringSidW = proto(a, 'ConvertSidToStringSidW', W.BOOL, [C.c_void_p, C.POINTER(W.LPWSTR)])
ConvertStringSecurityDescriptorToSecurityDescriptorW = proto(a, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', W.BOOL, [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)])
LocalFree = proto(k, 'LocalFree', C.c_void_p, [C.c_void_p])
CreateNamedPipeW = proto(k, 'CreateNamedPipeW', W.HANDLE, [W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, C.POINTER(SA)])
CreateFileW = proto(k, 'CreateFileW', W.HANDLE, [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE])
CreateDirectoryW = proto(k, 'CreateDirectoryW', W.BOOL, [W.LPCWSTR, C.POINTER(SA)])
GetNamedSecurityInfoW = proto(a, 'GetNamedSecurityInfoW', W.DWORD, [W.LPWSTR, C.c_int, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(C.c_void_p), C.POINTER(C.c_void_p), C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)])
GetSecurityDescriptorControl = proto(a, 'GetSecurityDescriptorControl', W.BOOL, [C.c_void_p, C.POINTER(W.WORD), C.POINTER(W.DWORD)])
GetAclInformation = proto(a, 'GetAclInformation', W.BOOL, [C.c_void_p, C.c_void_p, W.DWORD, C.c_int])
GetAce = proto(a, 'GetAce', W.BOOL, [C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)])
ConnectNamedPipe = proto(k, 'ConnectNamedPipe', W.BOOL, [W.HANDLE, C.POINTER(OVERLAPPED)])
DisconnectNamedPipe = proto(k, 'DisconnectNamedPipe', W.BOOL, [W.HANDLE])
GetNamedPipeServerProcessId = proto(k, 'GetNamedPipeServerProcessId', W.BOOL, [W.HANDLE, C.POINTER(W.DWORD)])
CreateEventW = proto(k, 'CreateEventW', W.HANDLE, [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR])
WaitForSingleObject = proto(k, 'WaitForSingleObject', W.DWORD, [W.HANDLE, W.DWORD])
CancelIoEx = proto(k, 'CancelIoEx', W.BOOL, [W.HANDLE, C.POINTER(OVERLAPPED)])
GetOverlappedResult = proto(k, 'GetOverlappedResult', W.BOOL, [W.HANDLE, C.POINTER(OVERLAPPED), C.POINTER(W.DWORD), W.BOOL])
ReadFile = proto(k, 'ReadFile', W.BOOL, [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.POINTER(OVERLAPPED)])
WriteFile = proto(k, 'WriteFile', W.BOOL, [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.POINTER(OVERLAPPED)])
CreateJobObjectW = proto(k, 'CreateJobObjectW', W.HANDLE, [C.c_void_p, W.LPCWSTR])
SetInformationJobObject = proto(k, 'SetInformationJobObject', W.BOOL, [W.HANDLE, C.c_int, C.c_void_p, W.DWORD])
AssignProcessToJobObject = proto(k, 'AssignProcessToJobObject', W.BOOL, [W.HANDLE, W.HANDLE])

def own_lifetime_job():
    """Broker + descendants only. OS reaps owned child on broker crash, never a PID/name search.

    Deliberately keep this non-inheritable handle until process exit; explicitly
    closing it while the owner is alive would terminate the owner itself.
    """
    handle = CreateJobObjectW(None, None); check(handle)
    info = JOB_EXTENDED_LIMIT(); info.BasicLimitInformation.LimitFlags = 0x2000
    try:
        check(SetInformationJobObject(handle, 9, C.byref(info), C.sizeof(info)))
        check(AssignProcessToJobObject(handle, GetCurrentProcess()))
        return handle
    except BaseException:
        CloseHandle(handle); raise

def check(ok):
    if not ok: raise C.WinError(C.get_last_error())
    return ok

def sha_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()

def process_identity(pid):
    handle = OpenProcess(0x1000 | 0x00100000, False, pid)
    check(handle)
    try:
        wait_state=WaitForSingleObject(handle, 0)
        if wait_state==0:
            error=ProcessLookupError('process_has_exited');error.winerror=1168;raise error
        if wait_state!=0x102:
            if wait_state==0xFFFFFFFF:raise C.WinError(C.get_last_error())
            raise RuntimeError('process_wait_state_unverified')
        times = [FT() for _ in range(4)]
        check(GetProcessTimes(handle, *[C.byref(t) for t in times]))
        size = W.DWORD(32768); path = C.create_unicode_buffer(size.value)
        check(QueryFullProcessImageNameW(handle, 0, path, C.byref(size)))
        return {'pid': pid, 'creation_filetime': (times[0].high << 32) | times[0].low,
                'exe': str(Path(path.value).resolve()), 'exe_sha256': sha_file(path.value), 'owner_sid': owner_sid(handle)}
    finally: CloseHandle(handle)

def verify_identity(expected):
    actual = process_identity(expected['pid'])
    if actual != expected: raise RuntimeError('process_identity_mismatch: PID, creation time or executable changed')
    return actual

def owner_sid(process_handle=None):
    token = W.HANDLE(); check(OpenProcessToken(process_handle or GetCurrentProcess(), 8, C.byref(token)))
    try:
        needed = W.DWORD()
        GetTokenInformation(token, 1, None, 0, C.byref(needed))
        buf = C.create_string_buffer(needed.value)
        check(GetTokenInformation(token, 1, buf, needed, C.byref(needed)))
        sid = C.cast(buf, C.POINTER(C.c_void_p))[0]
        text = W.LPWSTR(); check(ConvertSidToStringSidW(sid, C.byref(text)))
        try: return text.value
        finally: LocalFree(C.cast(text, C.c_void_p))
    finally: CloseHandle(token)

def create_private_directory(path):
    """Create a NEW isolated directory with owner+SYSTEM inheritance; never chmod an existing tree."""
    path = Path(path)
    if not path.is_absolute() or path.exists(): raise ValueError('private runtime directory must be new and absolute')
    descriptor = C.c_void_p()
    check(ConvertStringSecurityDescriptorToSecurityDescriptorW(
        'O:'+owner_sid()+'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;' + owner_sid() + ')', 1, C.byref(descriptor), None))
    security = SA(C.sizeof(SA), descriptor, False)
    try: check(CreateDirectoryW(str(path), C.byref(security)))
    finally: LocalFree(descriptor)
    return path


def verify_private_runtime_root(path):
    """Existing state is accepted only with the current owner's private ACL.

    Never adjusts an existing owner/ACL, follows a junction, or discovers another
    instance. The path is solely the common container of new per-launch roots.
    """
    path=Path(path)
    if not path.is_absolute() or not path.is_dir():raise ValueError('runtime_root_missing')
    for part in (path,*path.parents):
        info=part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('runtime_root_reparse_refused')
    class ACL_INFO(C.Structure):
        _fields_=[('count',W.DWORD),('used',W.DWORD),('free',W.DWORD)]
    def text_sid(pointer):
        text=W.LPWSTR();check(ConvertSidToStringSidW(pointer,C.byref(text)))
        try:return text.value
        finally:LocalFree(C.cast(text,C.c_void_p))
    owner=C.c_void_p();acl=C.c_void_p();sd=C.c_void_p()
    error=GetNamedSecurityInfoW(str(path),1,5,C.byref(owner),None,C.byref(acl),None,C.byref(sd))
    if error:raise C.WinError(error)
    try:
        if not owner or not acl:raise ValueError('runtime_root_null_security')
        sid=owner_sid();control=W.WORD();revision=W.DWORD()
        check(GetSecurityDescriptorControl(sd,C.byref(control),C.byref(revision)))
        if text_sid(owner)!=sid or not control.value&0x1000:raise ValueError('runtime_root_owner_or_protection')
        info=ACL_INFO();check(GetAclInformation(acl,C.byref(info),C.sizeof(info),2))
        entries=[]
        for i in range(info.count):
            ptr=C.c_void_p();check(GetAce(acl,i,C.byref(ptr)));head=C.string_at(ptr,8)
            if head[0]!=0 or head[1]&~0x13 or head[1]&3!=3 or int.from_bytes(head[4:8],'little')!=0x1f01ff:
                raise ValueError('runtime_root_acl_rights_or_flags')
            entries.append(text_sid(ptr.value+8))
        if len(entries)!=2 or set(entries)!={sid,'S-1-5-18'}:raise ValueError('runtime_root_acl_principals')
    finally:LocalFree(sd)
    return path


def ensure_private_runtime_root(path):
    path=Path(path)
    if not path.is_absolute():raise ValueError('runtime_root_absolute_required')
    for part in path.parents:
        if part.exists():
            info=part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                raise ValueError('runtime_root_reparse_refused')
    if not path.exists():
        try:create_private_directory(path)
        except (OSError,ValueError):
            if not path.exists():raise
    return verify_private_runtime_root(path)

def make_pipe(name):
    if not name.startswith('\\\\.\\pipe\\anet-source-') or '\\' in name[9:]:
        raise ValueError('only local anet-source pipe names allowed')
    descriptor = C.c_void_p()
    # Protected DACL: current operator + LocalSystem, never Everyone or network.
    sddl = 'D:P(A;;GA;;;SY)(A;;GA;;;' + owner_sid() + ')'
    check(ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, C.byref(descriptor), None))
    security = SA(C.sizeof(SA), descriptor, False)
    try:
        # DUPLEX | OVERLAPPED | FIRST_PIPE_INSTANCE; byte mode | REJECT_REMOTE_CLIENTS.
        handle = CreateNamedPipeW(name, 3 | 0x40000000 | 0x00080000, 8, 1, MAX_FRAME, MAX_FRAME, 0, C.byref(security))
        if handle == INVALID: raise C.WinError(C.get_last_error())
        return handle
    finally: LocalFree(descriptor)

def operation(handle, fn, deadline, *args, connecting=False):
    ov = OVERLAPPED(); ov.hEvent = CreateEventW(None, True, False, None); check(ov.hEvent)
    count = W.DWORD()
    try:
        ok = fn(handle, C.byref(ov)) if connecting else fn(handle, *args, C.byref(count), C.byref(ov))
        if not ok:
            error = C.get_last_error()
            if connecting and error == 535: return 0
            if error != 997: raise C.WinError(error)
            milliseconds = max(0, int((deadline - time.monotonic()) * 1000))
            wait = WaitForSingleObject(ov.hEvent, milliseconds)
            if wait != 0:
                CancelIoEx(handle, C.byref(ov))
                # Reap cancellation before OVERLAPPED/buffer lifetime ends.
                GetOverlappedResult(handle, C.byref(ov), C.byref(count), True)
                if wait == 0x102: raise TimeoutError('local_pipe_operation_timeout')
                raise C.WinError(C.get_last_error())
            check(GetOverlappedResult(handle, C.byref(ov), C.byref(count), False))
        return count.value
    finally: CloseHandle(ov.hEvent)

def read_exact(handle, length, deadline):
    result = bytearray()
    while len(result) < length:
        buf = C.create_string_buffer(length - len(result))
        count = operation(handle, ReadFile, deadline, buf, len(buf))
        if not count: raise EOFError('local pipe disconnected')
        result.extend(buf.raw[:count])
    return bytes(result)

def recv_json(handle, timeout=30):
    deadline = time.monotonic() + timeout
    length, = struct.unpack('<I', read_exact(handle, 4, deadline))
    if not 0 < length <= MAX_FRAME: raise ValueError('invalid IPC frame size')
    result = json.loads(read_exact(handle, length, deadline))
    if not isinstance(result, dict): raise ValueError('IPC object required')
    return result

def send_json(handle, value, timeout=30):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_FRAME: raise ValueError('IPC response exceeds bounded summary limit')
    raw = struct.pack('<I', len(raw)) + raw
    deadline = time.monotonic() + timeout
    while raw:
        buf = C.create_string_buffer(raw)
        count = operation(handle, WriteFile, deadline, buf, len(raw))
        if not count: raise EOFError('local pipe disconnected')
        raw = raw[count:]

def connect_checked(record, timeout=10):
    name = record['pipe']
    if not name.startswith('\\\\.\\pipe\\anet-source-') or '\\' in name[9:]: raise ValueError('local pipe only')
    deadline = time.monotonic() + timeout
    while True:
        handle = CreateFileW(name, 0xC0000000, 0, None, 3, 0x40000000, None)
        if handle != INVALID: break
        error = C.get_last_error()
        if error not in (2, 231) or time.monotonic() >= deadline: raise C.WinError(error)
        time.sleep(.025)
    try:
        pid = W.DWORD(); check(GetNamedPipeServerProcessId(handle, C.byref(pid)))
        if pid.value != record['broker']['pid']: raise RuntimeError('pipe_server_pid_mismatch: zero request bytes sent')
        verify_identity(record['broker'])
        if record['broker'].get('owner_sid') != owner_sid(): raise RuntimeError('pipe_server_owner_mismatch: zero request bytes sent')
        return handle
    except BaseException:
        CloseHandle(handle); raise

def request(record, op, *, timeout=30, **args):
    handle = connect_checked(record, min(10, timeout))
    try:
        # Check the actual pipe's OS server before transmitting source or prompt.
        send_json(handle, {'protocol': 'anet-source/1', 'instance': record['instance'], 'op': op, **args}, timeout)
        reply = recv_json(handle, timeout)
        send_json(handle, {'received': True}, min(3, timeout))
        if reply.get('instance') != record['instance'] or reply.get('codex') != record['codex']:
            raise RuntimeError('broker_instance_mismatch')
        if not reply.get('ok'): raise RuntimeError(reply.get('error', 'broker_rejected'))
        return reply['result']
    finally: CloseHandle(handle)
