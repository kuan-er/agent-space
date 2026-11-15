"""Existing connector lifecycle integration, limited to caller devices."""
import os
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from .network import read_private,write_private,private_existing,plain,NetworkError


def running(root):
    path=Path(root)/'portal/heartbeat.lock'
    if not path.exists():return False
    private_existing(path)
    with path.open('r+b') as stream:
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(stream,fcntl.LOCK_UN)
            return False
        except (BlockingIOError,OSError):return True


def inspect(runtime):
    root=plain(runtime['anet_root'])
    if not (root/'portal/connection.json').is_file():
        return {'state':'unconfigured','running':False,'detail':'尚未连接账户，请先登录。'}
    active=running(root)
    return {'state':'running' if active else 'stopped','running':active,'detail':'账户连接服务正在运行。' if active else '账户已保存，连接服务尚未运行。'}


def ensure(runtime,runner,new_connection=False,start_existing=False):
    root=plain(runtime['anet_root']);folder=root/'portal'
    state=read_private(folder/'connection.json') or {}
    current=inspect(runtime)
    if current['running']:return current
    if state.get('kind')!='device' and not (start_existing and (folder/'connector.py').is_file()):
        return {**current,'detail':'现有智能体的连接服务未运行，请在服务设置中启动。'}
    stable=folder/'connector.py'
    marker=folder/'client-companion.json'
    # Existing application-owned connectors are not upgraded or restarted by login.
    if stable.exists() and not new_connection and not start_existing and not read_private(marker):
        return {**current,'detail':'现有连接服务未运行，请在服务设置中启动；配置保持不变。'}
    if stable.exists():private_existing(stable)
    source=stable if stable.exists() else Path(__file__).with_name('companion')/'connector.py'
    if not source.is_file():raise NetworkError('companion_missing')
    write_private(marker,{'schema':'anet-client-companion/1','source':'bundled','state':'installing'})
    argv=[sys.executable,str(source),'install','--data-dir',str(root),'--anet',runner.binary]
    try:
        p=subprocess.run(argv,capture_output=True,timeout=45,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    except (OSError,subprocess.TimeoutExpired):raise NetworkError('companion_install_unverified') from None
    if p.returncode:raise NetworkError('companion_install_unverified')
    for _ in range(60):
        current=inspect(runtime)
        if current['running']:
            write_private(marker,{'schema':'anet-client-companion/1','source':'bundled','state':'running'})
            return current
        time.sleep(.5)
    raise NetworkError('companion_install_unverified')


def control(runtime,action):
    """Explicit service controls; same installed connector, identity, and startup label."""
    from .network import NativeRunner
    root=plain(runtime['anet_root']);stable=root/'portal/connector.py'
    if action=='status':return inspect(runtime)
    if action=='start':
        runner=NativeRunner(runtime);runner.run('up')
        return ensure(runtime,runner,start_existing=True)
    if action!='stop':raise NetworkError('request_invalid')
    if not stable.exists():return inspect(runtime)
    private_existing(stable)
    suffix=hashlib.sha256(str(root).encode()).hexdigest()[:8]
    if sys.platform=='darwin':
        path=Path.home()/'Library/LaunchAgents'/('org.agentspace.connector.'+suffix+'.plist')
        if path.exists():
            import plistlib
            doc=plistlib.loads(path.read_bytes());args=doc.get('ProgramArguments',[])
            if str(stable) not in args or str(root) not in args:raise NetworkError('companion_identity_mismatch')
            subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}',str(path)],capture_output=True,timeout=20)
    elif sys.platform.startswith('linux'):
        path=Path.home()/'.config/systemd/user'/('agent-space-'+suffix+'.service')
        if path.exists():
            text=path.read_text(encoding='utf-8')
            if str(stable) not in text or str(root) not in text:raise NetworkError('companion_identity_mismatch')
            p=subprocess.run(['systemctl','--user','stop',path.name],capture_output=True,timeout=20)
            if p.returncode:raise NetworkError('companion_stop_unverified')
    elif os.name=='nt':
        import ctypes
        from ctypes import wintypes
        parse=ctypes.windll.shell32.CommandLineToArgvW;parse.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(ctypes.c_int)];parse.restype=ctypes.POINTER(wintypes.LPWSTR)
        script="[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -and $_.CommandLine.Contains('connector.py') } | Select-Object ProcessId,CommandLine,@{Name='Created';Expression={$_.CreationDate.ToUniversalTime().ToString('o')}} | ConvertTo-Json -Compress"
        env=dict(os.environ);env.pop('PSModulePath',None)
        p=subprocess.run(['powershell.exe','-NoProfile','-Command',script],capture_output=True,timeout=20,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
        if p.returncode:raise NetworkError('companion_stop_unverified')
        rows=json.loads(p.stdout.decode('utf-8-sig')) if p.stdout.strip() else []
        if isinstance(rows,dict):rows=[rows]
        for row in rows:
            count=ctypes.c_int();pointer=parse(row['CommandLine'],ctypes.byref(count))
            if not pointer:continue
            try:args=[pointer[i] for i in range(count.value)]
            finally:ctypes.windll.kernel32.LocalFree(pointer)
            if len(args)<5 or Path(args[1]).resolve()!=stable or args[2]!='serve':continue
            if '--data-dir' not in args or args.index('--data-dir')+1>=len(args) or args[args.index('--data-dir')+1]!=str(root):continue
            # The OS must still report the same creation time and exact command.
            env.update(ANET_OWN_PID=str(row['ProcessId']),ANET_OWN_CREATED=row['Created'],ANET_OWN_COMMAND=row['CommandLine'])
            stop="$p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$env:ANET_OWN_PID); if ($p -and $p.CommandLine -ceq $env:ANET_OWN_COMMAND -and $p.CreationDate.ToUniversalTime().ToString('o') -ceq $env:ANET_OWN_CREATED) { Stop-Process -Id $p.ProcessId -ErrorAction Stop }"
            q=subprocess.run(['powershell.exe','-NoProfile','-Command',stop],capture_output=True,timeout=20,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
            if q.returncode:raise NetworkError('companion_stop_unverified')
    for _ in range(20):
        if not running(root):return inspect(runtime)
        time.sleep(.2)
    raise NetworkError('companion_stop_unverified')
