using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading.Tasks;
namespace ANetDesktop {
 public static class Bootstrap {
  static readonly object gate=new object();static Task<Dictionary<string,object>> pendingMutation;static string pendingOperation,pendingHarness;
  public static Dictionary<string,object> Exchange(RuntimeConfig runtime,string operation,string harness=null){
   bool mcp=operation=="connect-mcp"||operation=="mcp-status"||operation=="verify-mcp"||operation=="open-workbuddy";
   if(operation!="status"&&operation!="ensure"&&operation!="repair"&&operation!="authenticate"&&operation!="check-update"&&operation!="download-update"&&operation!="apply-update"&&!mcp)throw new ArgumentException("bounded_bootstrap_operation_required");
   if(operation=="authenticate"&&harness!="codex"&&harness!="claude_code"&&harness!="workbuddy"&&harness!="deepseek_harness")throw new ArgumentException("bounded_harness_required");
   if(mcp&&harness!="workbuddy")throw new ArgumentException("bounded_mcp_harness_required");
   if(operation!="authenticate"&&!mcp&&harness!=null)throw new ArgumentException("unexpected_harness");
   if(operation=="status"||operation=="check-update"||operation=="mcp-status")return Invoke(runtime,operation,harness);
   Task<Dictionary<string,object>> original;
   lock(gate){if(pendingMutation!=null&&(pendingOperation!=operation||pendingHarness!=harness))throw new InvalidOperationException("original_bootstrap_operation_pending");if(pendingMutation==null){pendingOperation=operation;pendingHarness=harness;pendingMutation=Task.Run(()=>Invoke(runtime,operation,harness));}original=pendingMutation;}
   try{if(!original.Wait(300000))throw new TimeoutException("bootstrap_operation_unconfirmed_no_retry");return original.GetAwaiter().GetResult();}
   finally{if(original.IsCompleted)lock(gate){if(Object.ReferenceEquals(pendingMutation,original)){pendingMutation=null;pendingOperation=null;pendingHarness=null;}}}
  }
  static async Task<string> BoundedRead(StreamReader reader,int limit){var b=new StringBuilder();var buffer=new char[4096];bool overflow=false;int n;while((n=await reader.ReadAsync(buffer,0,buffer.Length))!=0){if(b.Length+n<=limit)b.Append(buffer,0,n);else overflow=true;}if(overflow)throw new InvalidDataException("bootstrap_response_too_large");return b.ToString();}
  static Dictionary<string,object> Invoke(RuntimeConfig runtime,string operation,string harness){
   if(String.IsNullOrEmpty(runtime.Bootstrap)||!File.Exists(runtime.Bootstrap))return new Dictionary<string,object>{{"schema","anet-bootstrap/1"},{"state","unavailable"},{"reason","安装组件尚未提供环境检查接口。"}};
   var info=new ProcessStartInfo(runtime.Bootstrap,"--json "+operation+(harness==null?"":" --harness "+harness)+" --runtime "+Data.Quote(runtime.Path)+(operation=="apply-update"?" --wait-pid "+Process.GetCurrentProcess().Id+" --restart":"")){UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,StandardOutputEncoding=Encoding.UTF8,StandardErrorEncoding=Encoding.UTF8};
   using(var p=Process.Start(info)){Task<string> output=BoundedRead(p.StandardOutput,1048576),err=BoundedRead(p.StandardError,65536);bool readOnly=operation=="status"||operation=="check-update"||operation=="mcp-status";if(readOnly&&!p.WaitForExit(20000))throw new TimeoutException("bootstrap_status_unconfirmed");if(!readOnly)p.WaitForExit();string text=output.GetAwaiter().GetResult();err.GetAwaiter().GetResult();var d=Data.Json.Deserialize<Dictionary<string,object>>(text);string schema=Data.Text(d,"schema");if(schema!="anet-bootstrap/1"&&!(operation.EndsWith("-update")&&schema=="anet-desktop-update/1"))throw new InvalidDataException("bootstrap_schema_mismatch");return d;}
  }
 }
}
