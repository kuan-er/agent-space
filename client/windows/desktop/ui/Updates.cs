using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
namespace ANetDesktop {
 public sealed partial class Workbench {
  bool updateBusy,updateStaged,updateDownloading;DispatcherTimer updateTimer=new DispatcherTimer();
  void InitializeUpdates(){
   var preference=Path.Combine(runtime.Persistent,"update-preferences.json");try{if(File.Exists(preference))Find<CheckBox>("UpdateAuto").IsChecked=Data.Flag(Data.Read(preference),"automatic");}catch{}
   Find<CheckBox>("UpdateAuto").Click+=(s,e)=>{Directory.CreateDirectory(runtime.Persistent);File.WriteAllText(preference,Data.Json.Serialize(new{automatic=Find<CheckBox>("UpdateAuto").IsChecked==true}),new UTF8Encoding(false));};
   Click("UpdateCheck",()=>CheckUpdates(true));Click("UpdateApply",()=>{if(updateStaged&&!updateBusy){manualUpdateRequested=true;window.Close();return Task.FromResult(true);}return CheckUpdates(true);});
   updateTimer.Interval=TimeSpan.FromHours(6);updateTimer.Tick+=async(s,e)=>await CheckUpdates(false);updateTimer.Start();
   window.Loaded+=async(s,e)=>{await Task.Delay(1500);await CheckUpdates(false);};
  }
  async Task<bool> CheckUpdates(bool manual){if(updateBusy||environmentBusy)return false;if(String.IsNullOrEmpty(runtime.Bootstrap)){Find<TextBlock>("UpdateState").Text="安装后会自动检查更新。";return false;}updateBusy=true;Find<Button>("UpdateCheck").IsEnabled=false;
   try{var result=await Task.Run(()=>Bootstrap.Exchange(runtime,"check-update"));string state=Data.Text(result,"state"),version=Data.Text(result,"version");
    if(state=="staged"){updateDownloading=false;updateStaged=true;Find<TextBlock>("UpdateState").Text="新版本已准备，将在退出后自动安装。";}
    else if(state=="downloading"){updateDownloading=true;Find<TextBlock>("UpdateState").Text=DownloadProgress(result);}
    else if(state=="needs_space"){updateDownloading=false;Find<TextBlock>("UpdateState").Text=Data.Text(result,"reason");}
    else if(Data.Flag(result,"available")||state=="available"||state=="paused"){updateDownloading=false;Find<TextBlock>("UpdateState").Text=state=="paused"?"更新下载已暂停，已下载部分会保留。":"发现新版本 "+version+"。";if(manual||Find<CheckBox>("UpdateAuto").IsChecked==true){result=await Task.Run(()=>Bootstrap.Exchange(runtime,"download-update"));state=Data.Text(result,"state");updateStaged=state=="staged";updateDownloading=state=="downloading";Find<TextBlock>("UpdateState").Text=updateStaged?"更新已下载并校验，将在退出后安装。":updateDownloading?DownloadProgress(result):state=="needs_space"?Data.Text(result,"reason"):"更新尚未准备完成，下次会继续检查。";}}
    else {updateDownloading=false;Find<TextBlock>("UpdateState").Text=state=="current"||state=="up_to_date"?"当前已是最新版本。":"更新通道暂时不可用，将在下次启动时重试。";}
    Find<Button>("UpdateApply").IsEnabled=updateStaged;return true;
   }catch{Find<TextBlock>("UpdateState").Text="暂时无法检查更新，当前版本可继续使用。";return false;}
   finally{updateBusy=false;Find<Button>("UpdateCheck").IsEnabled=true;updateTimer.Interval=updateDownloading?TimeSpan.FromSeconds(10):TimeSpan.FromHours(6);}
  }
  static string DownloadProgress(Dictionary<string,object> value){long downloaded,total;if(Int64.TryParse(Data.Text(value,"downloaded_bytes"),out downloaded)&&Int64.TryParse(Data.Text(value,"total_bytes"),out total)&&total>0&&downloaded>=0&&downloaded<=total)return String.Format("正在后台下载更新：{0:0.0} / {1:0.0} MB。关闭应用后会继续。",downloaded/1048576.0,total/1048576.0);return "新版本正在后台下载，关闭应用后会继续。";}
  async Task ApplyPreparedUpdate(){updateTimer.Stop();if(updateBusy||!updateStaged)return;if(Find<CheckBox>("UpdateAuto").IsChecked!=true&&!manualUpdateRequested)return;
   try{var result=await Task.Run(()=>Bootstrap.Exchange(runtime,"apply-update"));if(Data.Text(result,"state")!="scheduled")Find<TextBlock>("UpdateState").Text="更新尚未排程，保留当前版本。";}catch{Find<TextBlock>("UpdateState").Text="更新尚未排程，下次继续。";}
  }
  bool manualUpdateRequested;
 }
}
