"""Create NEW private queue roots; verify existing ownership and ACL, never repair.

Windows creation uses an owner+SYSTEM protected inheritable DACL. Readback uses
native security APIs, not chmod or SDDL text equality. No existing ACL is changed.
"""
import os
from pathlib import Path
import stat


def plain(path):
    path=Path(path).absolute()
    for part in reversed((path,*path.parents)):
        if part.exists():
            info=part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                raise ValueError('artifact_path_reparse_refused')
    return path.resolve()


def open_new(path):
    """Create private bytes atomically, without relying on the process umask.

    O_EXCL preserves collision refusal. Windows inherits the private parent DACL.
    Existing files and their permissions are never changed.
    """
    fd=os.open(plain(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
    return os.fdopen(fd,'wb')


def write_new(path,data):
    with open_new(path) as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())


if os.name=='nt':
    import ctypes as C
    from ctypes import wintypes as W
    K=C.WinDLL('kernel32',use_last_error=True); A=C.WinDLL('advapi32',use_last_error=True)
    def proto(lib,name,result,args):
        fn=getattr(lib,name);fn.restype=result;fn.argtypes=args;return fn
    class SA(C.Structure):
        _fields_=[('length',W.DWORD),('descriptor',C.c_void_p),('inherit',W.BOOL)]
    class ACL_INFO(C.Structure):
        _fields_=[('count',W.DWORD),('used',W.DWORD),('free',W.DWORD)]
    close=proto(K,'CloseHandle',W.BOOL,[W.HANDLE])
    current_process=proto(K,'GetCurrentProcess',W.HANDLE,[])
    open_token=proto(A,'OpenProcessToken',W.BOOL,[W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)])
    token_info=proto(A,'GetTokenInformation',W.BOOL,[W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.POINTER(W.DWORD)])
    sid_text=proto(A,'ConvertSidToStringSidW',W.BOOL,[C.c_void_p,C.POINTER(W.LPWSTR)])
    local_free=proto(K,'LocalFree',C.c_void_p,[C.c_void_p])
    convert=proto(A,'ConvertStringSecurityDescriptorToSecurityDescriptorW',W.BOOL,[W.LPCWSTR,W.DWORD,C.POINTER(C.c_void_p),C.POINTER(W.DWORD)])
    create_directory=proto(K,'CreateDirectoryW',W.BOOL,[W.LPCWSTR,C.POINTER(SA)])
    named_security=proto(A,'GetNamedSecurityInfoW',W.DWORD,[W.LPWSTR,C.c_int,W.DWORD,C.POINTER(C.c_void_p),C.POINTER(C.c_void_p),C.POINTER(C.c_void_p),C.POINTER(C.c_void_p),C.POINTER(C.c_void_p)])
    sd_control=proto(A,'GetSecurityDescriptorControl',W.BOOL,[C.c_void_p,C.POINTER(W.WORD),C.POINTER(W.DWORD)])
    acl_info=proto(A,'GetAclInformation',W.BOOL,[C.c_void_p,C.c_void_p,W.DWORD,C.c_int])
    get_ace=proto(A,'GetAce',W.BOOL,[C.c_void_p,W.DWORD,C.POINTER(C.c_void_p)])
    def checked(ok):
        if not ok:raise C.WinError(C.get_last_error())
    def sid_string(pointer):
        value=W.LPWSTR();checked(sid_text(pointer,C.byref(value)))
        try:return value.value
        finally:local_free(C.cast(value,C.c_void_p))
    def owner_id():
        token=W.HANDLE();checked(open_token(current_process(),8,C.byref(token)))
        try:
            length=W.DWORD();token_info(token,1,None,0,C.byref(length))
            buf=C.create_string_buffer(length.value);checked(token_info(token,1,buf,length,C.byref(length)))
            return sid_string(C.cast(buf,C.POINTER(C.c_void_p))[0])
        finally:close(token)
    def security(path):
        owner=C.c_void_p();acl=C.c_void_p();descriptor=C.c_void_p()
        error=named_security(str(plain(path)),1,5,C.byref(owner),None,C.byref(acl),None,C.byref(descriptor))
        if error:raise C.WinError(error)
        try:
            if not owner or not acl:raise ValueError('null_owner_or_dacl')
            control=W.WORD();revision=W.DWORD();checked(sd_control(descriptor,C.byref(control),C.byref(revision)))
            info=ACL_INFO();checked(acl_info(acl,C.byref(info),C.sizeof(info),2));entries=[]
            for index in range(info.count):
                ptr=C.c_void_p();checked(get_ace(acl,index,C.byref(ptr)))
                head=C.string_at(ptr,8);typ,flags=head[0],head[1]
                if typ!=0:raise ValueError('non_allow_acl_entry')
                entries.append({'sid':sid_string(ptr.value+8),'mask':int.from_bytes(head[4:8],'little'),'flags':flags})
            return {'owner':sid_string(owner),'protected':bool(control.value&0x1000),'entries':entries}
        finally:local_free(descriptor)
    def create_new(path):
        path=plain(path)
        if path.exists() or not path.parent.is_dir():raise ValueError('new_private_root_required')
        sid=owner_id();descriptor=C.c_void_p()
        checked(convert('O:'+sid+'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;'+sid+')',1,C.byref(descriptor),None))
        try:checked(create_directory(str(path),C.byref(SA(C.sizeof(SA),descriptor,False))))
        finally:local_free(descriptor)
        verify(path,root=True)
        return path
    def verify(path,root=False):
        path=plain(path);info=security(path);sid=owner_id()
        allowed={sid,'S-1-5-18'}
        if root and (info['owner']!=sid or not info['protected']):raise ValueError('private_root_owner_or_protection')
        # Elevated Windows processes may assign the built-in Administrators
        # group as owner of inherited children. It is privileged; their actual
        # DACL must still grant only this operator and SYSTEM, never that group.
        if not root and info['owner'] not in (sid,'S-1-5-18','S-1-5-32-544'):
            raise ValueError('private_child_owner')
        if not info['entries'] or {e['sid'] for e in info['entries']}!=allowed:
            raise ValueError('unexpected_private_dacl_principal')
        for entry in info['entries']:
            if entry['mask']!=0x1f01ff or entry['flags']&~0x13:
                raise ValueError('private_dacl_rights_or_flags')
            if path.is_dir() and entry['flags']&3!=3:raise ValueError('private_directory_inheritance')
        return info
else:
    def owner_id():return str(os.geteuid())
    def create_new(path):
        path=plain(path)
        if path.exists() or not path.parent.is_dir():raise ValueError('new_private_root_required')
        path.mkdir(mode=0o700);verify(path,root=True);return path
    def verify(path,root=False):
        path=plain(path);info=path.stat()
        if info.st_uid!=os.geteuid() or info.st_mode&0o077:raise ValueError('private_posix_owner_or_mode')
        return {'owner':str(info.st_uid),'mode':stat.S_IMODE(info.st_mode)}
    security=verify
