using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace ANetDesktop {
 public static class Data {
  public static JavaScriptSerializer Json {get{return new JavaScriptSerializer{MaxJsonLength=1048576};}}
  public static string Text(Dictionary<string,object> d,string k){object v;return d!=null&&d.TryGetValue(k,out v)&&v!=null?Convert.ToString(v):"";}
  public static bool Flag(Dictionary<string,object> d,string k){object v;return d!=null&&d.TryGetValue(k,out v)&&v is bool&&(bool)v;}
  public static Dictionary<string,object> Map(object v){return v as Dictionary<string,object>;}
  public static Dictionary<string,object> Child(Dictionary<string,object> d,string k){object v;return d!=null&&d.TryGetValue(k,out v)?Map(v):null;}
  public static IEnumerable<Dictionary<string,object>> Rows(Dictionary<string,object>d,string k){object v;if(d==null||!d.TryGetValue(k,out v)||!(v is IEnumerable)||v is string)yield break;foreach(object x in (IEnumerable)v){var row=Map(x);if(row!=null)yield return row;}}
  public static string Quote(string v){if(v.IndexOfAny(new[]{'"','\r','\n'})>=0)throw new ArgumentException("invalid_path");int n=0;for(int i=v.Length-1;i>=0&&v[i]=='\\';i--)n++;return "\""+v+new string('\\',n)+"\"";}
  public static Dictionary<string,object> Read(string p){return Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(p,Encoding.UTF8));}
 }
 public sealed class RuntimeConfig {
  public string Path,Python,Node,Persistent,Bootstrap,Package,HarnessRoot;
  public static RuntimeConfig Load(string path,string package){var d=Data.Read(path);if(Data.Text(d,"schema")!="anet-desktop-runtime/1")throw new InvalidDataException("runtime_schema_mismatch");var r=new RuntimeConfig{Path=System.IO.Path.GetFullPath(path),Python=Absolute(d,"python_exe"),Node=Absolute(d,"node_root"),Persistent=Absolute(d,"persistent_root"),Package=package,Bootstrap=Data.Text(d,"bootstrap_exe")};r.HarnessRoot=Data.Text(d,"harness_root")==""?System.IO.Path.Combine(package,"harnesses"):Absolute(d,"harness_root");if(r.Bootstrap!="")r.Bootstrap=Absolute(d,"bootstrap_exe");return r;}
  static string Absolute(Dictionary<string,object>d,string k){string p=Data.Text(d,k);if(!System.IO.Path.IsPathRooted(p)||p.IndexOfAny(new[]{'"','\r','\n'})>=0)throw new InvalidDataException("absolute_runtime_path_required");return System.IO.Path.GetFullPath(p);}
 }
 public interface IClient {Dictionary<string,object> Exchange(Dictionary<string,object> request);}
 public sealed class ConfigClient:IClient {
  readonly RuntimeConfig runtime; readonly object gate=new object();
  public ConfigClient(RuntimeConfig r){runtime=r;}
  public Dictionary<string,object> Exchange(Dictionary<string,object> request){lock(gate){
   string entry="import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('anet_setup',run_name='__main__')";
   var info=new ProcessStartInfo(runtime.Python,"-B -u -c "+Data.Quote(entry)+" "+Data.Quote(runtime.HarnessRoot)+" rpc --runtime "+Data.Quote(runtime.Path)){UseShellExecute=false,CreateNoWindow=true,RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true,StandardOutputEncoding=Encoding.UTF8,StandardErrorEncoding=Encoding.UTF8,WorkingDirectory=runtime.HarnessRoot};
   info.EnvironmentVariables["PYTHONIOENCODING"]="utf-8";info.EnvironmentVariables["PYTHONPATH"]=runtime.HarnessRoot;
   using(var p=Process.Start(info)){using(var writer=new StreamWriter(p.StandardInput.BaseStream,new UTF8Encoding(false))){writer.WriteLine(Data.Json.Serialize(request));}Task<string> output=ReadBounded(p.StandardOutput,1048576),error=ReadBounded(p.StandardError,65536);p.WaitForExit();string text=output.GetAwaiter().GetResult();error.GetAwaiter().GetResult();var result=Data.Json.Deserialize<Dictionary<string,object>>(text);if(result==null)throw new InvalidDataException("empty_response");return result;}
  }}
  static async Task<string> ReadBounded(StreamReader reader,int limit){var b=new StringBuilder();var buffer=new char[4096];int n;bool overflow=false;while((n=await reader.ReadAsync(buffer,0,buffer.Length))>0){if(b.Length+n<=limit)b.Append(buffer,0,n);else overflow=true;}if(overflow)throw new InvalidDataException("response_too_large");return b.ToString();}
 }
}
