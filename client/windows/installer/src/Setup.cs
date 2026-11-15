using System;
using System.IO;
using System.IO.Compression;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace ANetSetup {
 // Finite, user-click-only client opener. No caller-supplied executable or URL.
 internal static class WorkBuddyClient {
  internal const string WindowsGuide="https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Installation-Win-Guide";
  static bool LocalDrive(DriveType kind){return kind==DriveType.Fixed||kind==DriveType.Removable||kind==DriveType.CDRom||kind==DriveType.Ram;}
  internal static bool LocalCandidateExists(string path,Func<string,DriveType> driveType,Func<string,bool> exists){
   try{string root=Path.GetPathRoot(path);if(String.IsNullOrEmpty(root)||!LocalDrive(driveType(root)))return false;return exists(path);}
   catch(ArgumentException){return false;}catch(IOException){return false;}catch(UnauthorizedAccessException){return false;}
  }
  static bool LocalCandidateExists(string path){return LocalCandidateExists(path,root=>new DriveInfo(root).DriveType,File.Exists);}
  internal static void NoReparsePath(string path){for(string p=path;!String.IsNullOrEmpty(p);p=Path.GetDirectoryName(p))if((File.Exists(p)||Directory.Exists(p))&&(File.GetAttributes(p)&FileAttributes.ReparsePoint)!=0)throw new InvalidDataException("workbuddy_reparse_path_refused");}
  internal static string Validate(string path){return Validate(path,root=>new DriveInfo(root).DriveType);}
  internal static string Validate(string path,Func<string,DriveType> driveType){
   if(String.IsNullOrWhiteSpace(path)||path!=path.Trim()||path.Length<4||!Char.IsLetter(path[0])||path[1]!=':'||path[2]!='\\'||path.IndexOfAny(new[]{'"','\r','\n','\0','/','|','<','>','?','*'})>=0||path.IndexOf(':',2)>=0)throw new InvalidDataException("workbuddy_absolute_local_path_required");
   string full=Path.GetFullPath(path);
   if(!String.Equals(path,full,StringComparison.OrdinalIgnoreCase)||!String.Equals(Path.GetFileName(full),"WorkBuddy.exe",StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("workbuddy_native_filename_required");
   // Reject mapped network/unknown drives before touching candidate metadata or bytes.
   DriveType kind=driveType(Path.GetPathRoot(full));
   if(!LocalDrive(kind))throw new InvalidDataException("workbuddy_local_drive_required");
   NoReparsePath(full);
   if(!File.Exists(full))throw new FileNotFoundException("workbuddy_client_not_found");
   using(var file=new FileStream(full,FileMode.Open,FileAccess.Read,FileShare.Read))using(var reader=new BinaryReader(file)){
    if(file.Length<64||reader.ReadUInt16()!=0x5a4d)throw new InvalidDataException("workbuddy_native_pe_required");
    file.Position=60;uint offset=reader.ReadUInt32();if(offset>1048576||offset+24>file.Length)throw new InvalidDataException("workbuddy_native_pe_required");
    file.Position=offset;if(reader.ReadUInt32()!=0x4550)throw new InvalidDataException("workbuddy_native_pe_required");file.Position=offset+22;ushort flags=reader.ReadUInt16();
    if((flags&2)==0||(flags&0x2000)!=0)throw new InvalidDataException("workbuddy_native_pe_required");
   }
   return full;
  }
  internal static IEnumerable<string> Discover(){
   string local=Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
   foreach(string p in new[]{Path.Combine(local,@"Programs\WorkBuddy\WorkBuddy.exe"),Path.Combine(local,@"WorkBuddy\WorkBuddy.exe"),Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),@"WorkBuddy\WorkBuddy.exe"),Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),@"WorkBuddy\WorkBuddy.exe")})if(LocalCandidateExists(p))yield return p;
   foreach(string folder in (Environment.GetEnvironmentVariable("PATH")??"").Split(';')){if(String.IsNullOrWhiteSpace(folder))continue;string p;try{p=Path.Combine(folder,"WorkBuddy.exe");}catch(ArgumentException){continue;}if(LocalCandidateExists(p))yield return p;}
   foreach(var hive in new[]{Registry.CurrentUser,Registry.LocalMachine})foreach(string sub in new[]{@"Software\Microsoft\Windows\CurrentVersion\Uninstall",@"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"}){
    using(var root=hive.OpenSubKey(sub)){if(root==null)continue;foreach(string name in root.GetSubKeyNames())using(var key=root.OpenSubKey(name)){
     if(key==null)continue;string display=key.GetValue("DisplayName") as string;
     if(display==null||!System.Text.RegularExpressions.Regex.IsMatch(display,@"\AWorkBuddy(?:\s+[0-9.]+)?\z",System.Text.RegularExpressions.RegexOptions.IgnoreCase))continue;
     string location=key.GetValue("InstallLocation") as string;if(String.IsNullOrEmpty(location))continue;string p;try{p=Path.Combine(location,"WorkBuddy.exe");}catch(ArgumentException){continue;}if(LocalCandidateExists(p))yield return p;
    }}
   }
  }
  internal static Dictionary<string,object> Open(IEnumerable<string> candidates,Action<ProcessStartInfo> start){return Open(candidates,start,root=>new DriveInfo(root).DriveType);}
  internal static Dictionary<string,object> Open(IEnumerable<string> candidates,Action<ProcessStartInfo> start,Func<string,DriveType> driveType){
   string client=null;foreach(string candidate in candidates){client=Validate(candidate,driveType);break;}
   var info=client==null?new ProcessStartInfo(WindowsGuide){UseShellExecute=true}:new ProcessStartInfo(client){UseShellExecute=false,WorkingDirectory=Path.GetDirectoryName(client)};
   info.CreateNoWindow=false;info.WindowStyle=ProcessWindowStyle.Normal;
   if(client==null)start(info);else using(var hold=new FileStream(client,FileMode.Open,FileAccess.Read,FileShare.Read)){Validate(client,driveType);start(info);}
   return new Dictionary<string,object>{{"schema","anet-bootstrap/1"},{"harness","workbuddy"},{"state","user_action_required"},{"launch_kind",client==null?"installation_guide":"local_client"},{"client_presence",client==null?"not_observed":"detected"},{"authentication_verified",false},{"native_use_verified",false},{"reason",client==null?"已打开官方 Windows 安装与登录说明。安装并登录 WorkBuddy 后，请重新检查 MCP 配置。":"已请求打开本机 WorkBuddy。请在客户端完成登录并重载 MCP；打开窗口不代表已登录或已调用 ANet。"}};
  }
 }
 static class Program {
  static readonly JavaScriptSerializer Json=new JavaScriptSerializer {MaxJsonLength=16*1024*1024};
  static string Self {get{return Assembly.GetExecutingAssembly().Location;}}
  static string Quote(string s){if(s.IndexOfAny(new[]{'"','\r','\n'})>=0)throw new Exception("unsafe_argument");return "\""+s+"\"";}
  static string Digest(Stream s){using(var h=SHA256.Create())return BitConverter.ToString(h.ComputeHash(s)).Replace("-","").ToLowerInvariant();}
  static string Hash(string p){using(var f=File.OpenRead(p))return Digest(f);}
  static Dictionary<string,object> Read(string p){return Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(p,Encoding.UTF8));}
  static string Text(Dictionary<string,object>d,string k){return d.ContainsKey(k)?Convert.ToString(d[k]):"";}
  static object Value(string id,string name,string state,string detail){return new {id=id,name=name,state=state,detail=detail};}
  static string Run(string exe,string args,int timeout,string input=null,bool allowError=false){
   var si=new ProcessStartInfo(exe,args){UseShellExecute=false,CreateNoWindow=true,RedirectStandardInput=input!=null,RedirectStandardOutput=true,RedirectStandardError=true,StandardOutputEncoding=Encoding.UTF8,StandardErrorEncoding=Encoding.UTF8};
   using(var p=Process.Start(si)){var output=p.StandardOutput.ReadToEndAsync();var err=p.StandardError.ReadToEndAsync();if(input!=null){using(var writer=new StreamWriter(p.StandardInput.BaseStream,new UTF8Encoding(false))){writer.WriteLine(input);}}if(!p.WaitForExit(timeout))throw new Exception("operation_unconfirmed_no_retry");string o=output.GetAwaiter().GetResult();err.GetAwaiter().GetResult();if(o.Length>2*1024*1024)throw new Exception("bounded_response_exceeded");if(p.ExitCode!=0&&!allowError)throw new Exception(o.Length>0?o:"finite_operation_failed");return o;}
  }
  static void NoReparse(string path){for(var p=new DirectoryInfo(Path.GetFullPath(path));p!=null;p=p.Parent)if(p.Exists&&(p.Attributes&FileAttributes.ReparsePoint)!=0)throw new Exception("reparse_path_refused");}
  static void Cleanup(string dir){
   string temp=Path.GetFullPath(Path.GetTempPath()).TrimEnd('\\')+"\\";
   if(!dir.StartsWith(temp+"ANetSetup-",StringComparison.OrdinalIgnoreCase)||!Directory.Exists(dir))return;
   NoReparse(dir);
   foreach(string file in Directory.GetFiles(dir,"*",SearchOption.AllDirectories)){try{File.Delete(file);}catch(IOException){}catch(UnauthorizedAccessException){}}
   try{Directory.Delete(dir,true);}catch(IOException){}catch(UnauthorizedAccessException){}
  }
  static string Extract(){
   var scratch=Path.Combine(Path.GetTempPath(),"ANetSetup-"+Guid.NewGuid().ToString("N"));NoReparse(Path.GetTempPath());
   long required=0;
   foreach(string resource in new[]{"runtime.zip","code.zip"})using(var data=Assembly.GetExecutingAssembly().GetManifestResourceStream(resource))using(var zip=new ZipArchive(data,ZipArchiveMode.Read))foreach(var e in zip.Entries){required+=e.Length;if(e.Length>700000000||required>1800000000)throw new Exception("payload_archive_limit");}
   if(new DriveInfo(Path.GetPathRoot(scratch)).AvailableFreeSpace<required+67108864)throw new IOException("insufficient_temporary_disk_space");
   var sid=WindowsIdentity.GetCurrent().User;var acl=new DirectorySecurity();acl.SetAccessRuleProtection(true,false);acl.SetOwner(sid);acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
   new DirectoryInfo(scratch).Create(acl);
   try{
   var seen=new HashSet<string>(StringComparer.OrdinalIgnoreCase);long total=0;
   foreach(string resource in new[]{"runtime.zip","code.zip"})using(var data=Assembly.GetExecutingAssembly().GetManifestResourceStream(resource)){
    string expected=resource=="runtime.zip"?PayloadPin.RuntimeSha256:PayloadPin.CodeSha256;
    if(data==null||Digest(data)!=expected)throw new Exception("embedded_payload_digest_mismatch");data.Position=0;
    using(var zip=new ZipArchive(data,ZipArchiveMode.Read)){
     foreach(var e in zip.Entries){string n=e.FullName;if(n.EndsWith("/"))continue;
      if(n.Length>240||n.Contains("\\")||n.Contains(":")||n.StartsWith("/")||n.Split('/').Length==0||!seen.Add(n))throw new Exception("payload_archive_name");
      foreach(string part in n.Split('/'))if(part==""||part=="."||part=="..")throw new Exception("payload_archive_component");
      total+=e.Length;if(e.Length>700000000||total>1800000000)throw new Exception("payload_archive_limit");
      string dest=Path.GetFullPath(Path.Combine(scratch,n));if(!dest.StartsWith(scratch+"\\",StringComparison.OrdinalIgnoreCase))throw new Exception("payload_archive_escape");
      Directory.CreateDirectory(Path.GetDirectoryName(dest));using(var src=e.Open())using(var dst=new FileStream(dest,FileMode.CreateNew,FileAccess.Write,FileShare.None)){src.CopyTo(dst);}
     }
    }
   }
   return scratch;
   }catch{Cleanup(scratch);throw;}
  }
  static string Engine(string action,string[] args){
   string dir=Extract();try{
    string more="";for(int i=0;i<args.Length;i++){
     string a=args[i];if(a=="--fixture-root"||a=="--node-root"||a=="--backend-root"){if(i+1>=args.Length)throw new Exception("missing_argument");more+=" "+a+" "+Quote(Path.GetFullPath(args[++i]));}
     else if(a=="--start-services"||a=="--fail-before-activate")more+=" "+a;
    }
    return Run(Path.Combine(dir,"python/python.exe"),"-B -X utf8 "+Quote(Path.Combine(dir,"frontend/install/install_engine.py"))+" --payload "+Quote(dir)+" --self-exe "+Quote(Self)+" --private-payload --action "+action+more,300000);
   }finally{
    // Remove only this invocation's private random scratch, never user state.
    Cleanup(dir);
   }
  }
  static Dictionary<string,object> Runtime(string path){NoReparse(path);var r=Read(path);if(Text(r,"schema")!="anet-desktop-runtime/1")throw new Exception("runtime_schema");foreach(string key in new[]{"python_exe","node_root","persistent_root","backend_root","bootstrap_exe"})if(!Path.IsPathRooted(Text(r,key)))throw new Exception("absolute_runtime_path_required");
   string receipt=Path.Combine(Path.GetDirectoryName(path),"receipt.json");var own=Read(receipt);var expected=(Dictionary<string,object>)own["runtime"];
   foreach(string key in new[]{"python_exe","broker_python_exe","node_root","anet_root","persistent_root","backend_root","bootstrap_exe","harness_root","harness_config_path","codex_exe","source_state_root"})if(Text(r,key)!=Text(expected,key))throw new Exception("runtime_receipt_mismatch");
   if(Hash(Text(r,"bootstrap_exe"))!=Text(own,"setup_sha256"))throw new Exception("bootstrap_hash_mismatch");return r;
  }
  static Dictionary<string,object> Mcp(string path,string action){
   var r=Runtime(path);string root=Text(r,"harness_root");
   if(!File.Exists(Path.Combine(root,"anet_harnesses/workbuddy_mcp.py")))return new Dictionary<string,object>{{"schema","anet-bootstrap/1"},{"harness","workbuddy"},{"integration","local_mcp"},{"state","needs_install"},{"configured",false},{"reason","workbuddy_mcp_module_not_installed"}};
   string harnessPath=Convert.ToBase64String(Encoding.UTF8.GetBytes(root));
   string entry="import sys,base64,runpy;sys.path.insert(0,base64.b64decode('"+harnessPath+"').decode('utf-8'));runpy.run_module('anet_harnesses.workbuddy_mcp',run_name='__main__')";
   var result=Json.Deserialize<Dictionary<string,object>>(Run(Text(r,"python_exe"),"-B -X utf8 -c "+Quote(entry)+" --runtime "+Quote(path),60000,Json.Serialize(new {action=action}),true));
   if(Text(result,"schema")!="anet-bootstrap/1"||Text(result,"harness")!="workbuddy"||Text(result,"integration")!="local_mcp")throw new Exception("mcp_response_contract_mismatch");return result;
  }
  static object Status(string path){
   var r=Runtime(path);var deps=new List<object>();string python=Text(r,"python_exe"),node=Text(r,"node_root"),backend=Text(r,"backend_root");
   bool taskServiceUpgrade=false;string expectedDigest="";
   try{
    var dependencies=Read(Path.Combine(Path.GetDirectoryName(path),"dependencies.json"));var expectedRelease=(Dictionary<string,object>)dependencies["harness_release"];expectedDigest=Text(expectedRelease,"package_sha256");
    var taskConfig=Read(Path.Combine(node,"config.json"));var requester=(Dictionary<string,object>)taskConfig["requester"];var harnessSources=(Dictionary<string,object>)requester["harness_sources"];var releasePolicy=(Dictionary<string,object>)harnessSources["release_policy"];
    taskServiceUpgrade=Text(expectedRelease,"schema")!="anet-harness-release-closure/2"||Text(harnessSources,"enabled")!="True"||Text(releasePolicy,"package_schema")!="anet-harness-release-closure/2"||Text(releasePolicy,"package_sha256")!=expectedDigest||Text(releasePolicy,"service_exe_sha256")!=Hash(python)||!File.Exists(Path.Combine(node,"app/anet_harnesses/package_release.py"));
   }catch(Exception){taskServiceUpgrade=true;}
   deps.Add(Value("python","Python 3.14.10",File.Exists(python)?"installed":"missing","应用专用运行时"));
   deps.Add(Value("taskd","任务服务",taskServiceUpgrade?"needs_task_service_upgrade":File.Exists(Path.Combine(node,"app/run_taskd.py"))?"installed":"missing",taskServiceUpgrade?"任务服务尚未升级到本客户端所需的来源协议；已有身份和配置已保留。":"任务服务的版本与本客户端来源协议匹配。"));
   deps.Add(Value("backend","来源与返回服务",File.Exists(Path.Combine(backend,"desktop_controller.py"))?"installed":"missing","复用已审阅的 r3 来源协议"));
   string anet=Text(r,"anet_root");bool paired=File.Exists(Path.Combine(anet,"portal/connection.json"));bool artifactReady=false;
   try{var nodeStatus=Read(Path.Combine(node,"status.json"));if(nodeStatus.ContainsKey("requester_artifacts")){var artifact=(Dictionary<string,object>)nodeStatus["requester_artifacts"];artifactReady=Text(artifact,"ready")=="True";}}catch(IOException){}catch(UnauthorizedAccessException){}
   deps.Add(Value("artifacts","自动文件交付",artifactReady?"ready":paired?"needs_preparation":"needs_pairing",artifactReady?"文件交付服务已就绪。":paired?"节点已配对，点击准备环境以核验文件服务。":"请先在 ANet 门户完成此节点的连接与配对。"));
   string config=Text(r,"harness_config_path");
   string harnessPath=Convert.ToBase64String(Encoding.UTF8.GetBytes(Text(r,"harness_root")));
   string entry="import sys,base64,runpy;sys.path.insert(0,base64.b64decode('"+harnessPath+"').decode('utf-8'));runpy.run_module('anet_harnesses.cli',run_name='__main__')";
   var probe=Json.Deserialize<Dictionary<string,object>>(Run(python,ProbeArguments(entry,config,node),60000));
   object harnesses=probe.ContainsKey("harnesses")?probe["harnesses"]:new object[0];
   foreach(object item in (IEnumerable)harnesses){var row=item as Dictionary<string,object>;if(row!=null&&Text(row,"id")=="workbuddy"){try{row["local_mcp"]=Mcp(path,"status");}catch(Exception){row["local_mcp"]=new {state="unverified",configured=false,reason="local_mcp_status_unconfirmed"};}}}
   return new {schema="anet-bootstrap/1",state=taskServiceUpgrade?"needs_task_service_upgrade":"authentication_required",task_service=new {requires_upgrade=taskServiceUpgrade,expected_package_sha256=expectedDigest,mutated=false},dependencies=deps,harnesses=harnesses,actions=new {ensure=true,repair=true},reason=taskServiceUpgrade?"现有任务服务需要单独升级后才能使用本客户端的多平台委托；身份、信任和付款设置均已保留。":"运行时已随客户端准备；首次使用需完成所选平台的官方登录。"};
  }
  internal static string ProbeArguments(string entry,string config,string node){return "-B -X utf8 -c "+Quote(entry)+" probe --json --config "+Quote(config)+" --node-root "+Quote(node);}
  internal static void ValidateOpenArguments(string[] args){
   if(args.Length!=6||args[0]!="--json"||args[1]!="open-workbuddy")throw new ArgumentException("finite_workbuddy_open_arguments_required");
   var values=new Dictionary<string,string>();for(int i=2;i<args.Length;i+=2){if((args[i]!="--runtime"&&args[i]!="--harness")||values.ContainsKey(args[i]))throw new ArgumentException("finite_workbuddy_open_arguments_required");values[args[i]]=args[i+1];}
   if(!values.ContainsKey("--runtime")||!values.ContainsKey("--harness")||values["--harness"]!="workbuddy")throw new ArgumentException("finite_workbuddy_harness_required");
   string raw=values["--runtime"];if(raw.Length<3||!Char.IsLetter(raw[0])||raw[1]!=':'||(raw[2]!='\\'&&raw[2]!='/'))throw new ArgumentException("absolute_runtime_path_required");
   WorkBuddyClient.NoReparsePath(Path.GetFullPath(raw));
  }
  static object Authenticate(string path,string id){
   var r=Runtime(path);string exe="",argument="";
   if(id=="codex"||id=="claude_code"){
    var config=Read(Text(r,"harness_config_path"));var harnesses=(Dictionary<string,object>)config["harnesses"];var row=(Dictionary<string,object>)harnesses[id];exe=Text(row,"executable");argument=id=="codex"?"login":"auth login";
    if(!Path.IsPathRooted(exe))throw new Exception("absolute_harness_path_required");NoReparse(exe);
   }
   else if(id=="workbuddy")exe="https://124.222.173.36:8888/#agents";
   else if(id=="deepseek_harness")exe="https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md";
   else throw new Exception("finite_harness_id_required");
   if(!exe.StartsWith("https://",StringComparison.Ordinal)&&!File.Exists(exe))throw new Exception("harness_not_installed");
   Process.Start(new ProcessStartInfo(exe,argument){UseShellExecute=true});return new {schema="anet-bootstrap/1",state="user_action_required",harness=id,reason="已打开官方登录或设置入口；完成后重新检查。",authentication_verified=false};
  }
  [STAThread] static int Main(string[] args){
   try{
    if(args.Length>0)Console.SetOut(new StreamWriter(Console.OpenStandardOutput(),new UTF8Encoding(false)){AutoFlush=true});
    if(args.Length==0){Application.EnableVisualStyles();Application.Run(new Wizard());return 0;}
    if(args.Length>=2&&args[0]=="--json"){
     string runtime="",harness="";for(int i=2;i<args.Length-1;i++){if(args[i]=="--runtime")runtime=Path.GetFullPath(args[i+1]);if(args[i]=="--harness")harness=args[i+1];}
     if(args[1]=="check-update"||args[1]=="download-update"||args[1]=="apply-update"){
      var u=Runtime(runtime);string more="";for(int i=2;i<args.Length;i++){if(args[i]=="--wait-pid"&&i+1<args.Length){int pid;if(!Int32.TryParse(args[++i],out pid)||pid<1)throw new Exception("wait_pid");more+=" --wait-pid "+pid;}else if(args[i]=="--restart")more+=" --restart";}
      string result=Run(Text(u,"python_exe"),"-B -X utf8 "+Quote(Path.Combine(Path.GetDirectoryName(runtime),"updater.py"))+" "+args[1]+" --runtime "+Quote(runtime)+more,args[1]=="download-update"?1800000:120000);Console.WriteLine(result);return 0;
     }
     if(args[1]=="status"){Console.WriteLine(Json.Serialize(Status(runtime)));return 0;}
     if(args[1]=="open-workbuddy"){ValidateOpenArguments(args);Runtime(runtime);Console.WriteLine(Json.Serialize(WorkBuddyClient.Open(WorkBuddyClient.Discover(),info=>{using(var process=Process.Start(info)){} })));return 0;}
     if(args[1]=="connect-mcp"||args[1]=="mcp-status"||args[1]=="verify-mcp"){
      if(harness!="workbuddy")throw new Exception("finite_mcp_harness_required");
      Console.WriteLine(Json.Serialize(Mcp(runtime,args[1]=="connect-mcp"?"connect":args[1]=="verify-mcp"?"verify":"status")));return 0;
     }
     if(args[1]=="authenticate"){Console.WriteLine(Json.Serialize(Authenticate(runtime,harness)));return 0;}
     if(args[1]!="ensure"&&args[1]!="repair")throw new Exception("finite_bootstrap_operation_required");
     var r=Runtime(runtime);var engineArgs=new List<string>{"--node-root",Text(r,"anet_root")==""?Text(r,"node_root"):Text(r,"anet_root"),"--backend-root",Text(r,"backend_root"),"--start-services"};
     var own=Read(Path.Combine(Path.GetDirectoryName(runtime),"receipt.json"));
     if(own.ContainsKey("fixture")&&own["fixture"] is bool&&(bool)own["fixture"]){
      string fixture=Text(own,"fixture_root");if(!Path.IsPathRooted(fixture))throw new Exception("fixture_runtime_root_required");fixture=Path.GetFullPath(fixture);NoReparse(fixture);
      if(!String.Equals(Path.GetFullPath(Text(r,"persistent_root")),Path.Combine(fixture,"Data","ANetDesktop"),StringComparison.OrdinalIgnoreCase)||!String.Equals(Path.GetFullPath(Text(own,"program_root")),Path.Combine(fixture,"Programs","ANetDesktop"),StringComparison.OrdinalIgnoreCase))throw new Exception("fixture_runtime_scope_mismatch");
      engineArgs.Add("--fixture-root");engineArgs.Add(fixture);
     }
     Engine(args[1]=="repair"?"repair":"install",engineArgs.ToArray());Console.WriteLine(Json.Serialize(Status(runtime)));return 0;
    }
    string action=args[0]=="--install"?"install":args[0]=="--repair"?"repair":args[0]=="--rollback"?"rollback":args[0]=="--uninstall"?"uninstall":"";if(action=="")throw new Exception("finite_installer_operation_required");
    Console.WriteLine(Engine(action,args));return 0;
   }catch(Exception ex){Console.WriteLine(Json.Serialize(new {schema="anet-bootstrap/1",state="failed",reason=ex.Message.Length>2048?"bounded_operation_failed":ex.Message,business_tasks_sent=0}));return 1;}
  }
  sealed class Wizard:Form {
   readonly Button install=new Button(),repair=new Button(),uninstall=new Button(),rollback=new Button(),open=new Button();readonly Label detail=new Label();
   public Wizard(){Text="ANet Desktop 安装";Size=new Size(660,440);StartPosition=FormStartPosition.CenterScreen;FormBorderStyle=FormBorderStyle.FixedDialog;MaximizeBox=false;BackColor=Color.FromArgb(14,22,39);ForeColor=Color.White;Font=new Font("Microsoft YaHei UI",10);
    var title=new Label{Text="连接你的智能体网络",Font=new Font("Microsoft YaHei UI",23,FontStyle.Bold),Location=new Point(36,32),AutoSize=true};Controls.Add(title);
    detail.Text="一次安装，准备 Python、ANet 与任务服务。\r\n已有节点、任务记录和来源绑定会保留。\r\n\r\n首次运行需要完成官方账号登录。\r\n安装程序未签名；上游依赖按官方签名或 SHA-256 核验。";detail.SetBounds(38,105,570,172);detail.ForeColor=Color.FromArgb(176,192,218);Controls.Add(detail);
    SetupButton(install,"安装并准备",36,290);SetupButton(repair,"修复",190,290);SetupButton(rollback,"回滚前端",300,290);SetupButton(uninstall,"卸载前端",430,290);SetupButton(open,"打开客户端",36,340);open.Enabled=false;
    install.Click+=async(s,e)=>await Action("install");repair.Click+=async(s,e)=>await Action("repair");rollback.Click+=async(s,e)=>await Action("rollback");uninstall.Click+=async(s,e)=>await Action("uninstall");
    open.Click+=(s,e)=>{string current=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"ANetDesktop/current.json");var r=Read(current);string front=Program.Text(r,"frontend_root");Process.Start(new ProcessStartInfo(Path.Combine(front,"ANetDesktop.exe"),"--runtime "+Quote(Path.Combine(front,"install/runtime.json"))){UseShellExecute=true});Close();};
   }
   void SetupButton(Button b,string text,int x,int y){b.Text=text;b.SetBounds(x,y,text.Length>4?145:105,36);b.BackColor=Color.FromArgb(39,87,184);b.ForeColor=Color.White;b.FlatStyle=FlatStyle.Flat;Controls.Add(b);}
   async Task Action(string action){foreach(Control c in Controls)if(c is Button)c.Enabled=false;detail.Text="正在核验安装包并准备运行环境…\r\n请保留此窗口，通常需要一到两分钟。";try{string value=await Task.Run(()=>Engine(action,new[]{"--start-services"}));var result=Json.Deserialize<Dictionary<string,object>>(value);detail.Text=Text(result,"state")=="installed"?"客户端与依赖已安装。\r\n打开客户端后完成官方登录。\r\n已有节点设置与任务记录均保留。":value;open.Enabled=action!="uninstall";}catch(Exception ex){detail.Text="安装未确认完成。\r\n"+ex.Message;}finally{install.Enabled=repair.Enabled=rollback.Enabled=uninstall.Enabled=true;}}
  }
 }
}
