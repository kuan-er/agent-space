"""Read-only Windows source handles retained across a single build.

FILE_SHARE_READ denies byte writes, deletion and path replacement. Creation of
another NTFS hardlink remains possible and is separately checked after build.
Only one temporary CRT descriptor is used while hashing each native handle.
"""
import ctypes, hashlib, msvcrt, os
from ctypes import wintypes as W
from pathlib import Path

K=ctypes.WinDLL('kernel32',use_last_error=True)
K.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,ctypes.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
K.CreateFileW.restype=W.HANDLE
K.CloseHandle.argtypes=[W.HANDLE];K.CloseHandle.restype=W.BOOL
K.DuplicateHandle.argtypes=[W.HANDLE,W.HANDLE,W.HANDLE,ctypes.POINTER(W.HANDLE),W.DWORD,W.BOOL,W.DWORD]
K.DuplicateHandle.restype=W.BOOL
K.GetCurrentProcess.restype=W.HANDLE
INVALID=ctypes.c_void_p(-1).value

def no_reparse(path):
 p=Path(path)
 if not p.is_absolute():raise ValueError('absolute_input_required')
 for part in (p,*p.parents):
  s=part.lstat()
  if getattr(s,'st_file_attributes',0)&0x400:raise ValueError('reparse_input_refused: '+str(p))

def metadata(s):
 return {'bytes':s.st_size,'device':s.st_dev,'file_id':s.st_ino,'mtime_ns':s.st_mtime_ns,'nlink':s.st_nlink}

def inspect_handle(handle):
 duplicate=W.HANDLE();current=K.GetCurrentProcess()
 if not K.DuplicateHandle(current,handle,current,ctypes.byref(duplicate),0,False,2):raise ctypes.WinError(ctypes.get_last_error())
 try:fd=msvcrt.open_osfhandle(int(duplicate.value),os.O_RDONLY|os.O_BINARY)
 except BaseException:K.CloseHandle(duplicate);raise
 with os.fdopen(fd,'rb') as f:
  before=metadata(os.fstat(f.fileno()));h=hashlib.sha256()
  for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
  after=metadata(os.fstat(f.fileno()))
 if before!=after:raise ValueError('held_input_changed_during_hash')
 return {'sha256':h.hexdigest(),**after}

class HeldInputs:
 def __init__(self,records,acl_reader):self.records=records;self.acl_reader=acl_reader;self.handles=[]
 def __enter__(self):
  try:
   seen=set()
   for row in self.records:
    path=Path(row['path']);key=str(path).casefold()
    if key in seen:continue
    seen.add(key);no_reparse(path)
    handle=K.CreateFileW(str(path),0x80000000,1,None,3,0x08000000,None)
    if handle==INVALID:raise ctypes.WinError(ctypes.get_last_error())
    self.handles.append(handle);actual=inspect_handle(handle)
    if any(actual[k]!=row[k] for k in ('bytes','device','file_id','mtime_ns','nlink','sha256')):raise ValueError('held_input_pin_drift: '+str(path))
    no_reparse(path)
    if metadata(path.stat())!={k:actual[k] for k in metadata(path.stat())}:raise ValueError('held_input_path_identity_drift')
    if self.acl_reader(path)!=row['security_sddl']:raise ValueError('held_input_acl_drift: '+str(path))
   return self
  except BaseException:self.close();raise
 def close(self):
  for h in reversed(self.handles):K.CloseHandle(h)
  self.handles=[]
 def __exit__(self,*exc):self.close()
