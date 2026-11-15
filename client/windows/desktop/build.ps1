param([string]$OutputRoot='')
$ErrorActionPreference='Stop'
$anetUiRoot=Split-Path -Parent $MyInvocation.MyCommand.Path
if(-not $OutputRoot){$OutputRoot=Join-Path $anetUiRoot 'out'}
$OutputRoot=[System.IO.Path]::GetFullPath($OutputRoot)
[System.IO.Directory]::CreateDirectory($OutputRoot)|Out-Null
$anetUiCompiler=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$anetWpfRoot=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\WPF'
$anetUiRefs=@('/r:System.Core.dll','/r:System.Web.Extensions.dll','/r:System.Xaml.dll','/r:System.Windows.Forms.dll',"/r:$anetWpfRoot\WindowsBase.dll","/r:$anetWpfRoot\PresentationCore.dll","/r:$anetWpfRoot\PresentationFramework.dll")
$anetUiRefs+=@("/resource:$anetUiRoot\ui\MainWindow.xaml,ANetDesktop.MainWindow.xaml")
$anetUiSources=@("$anetUiRoot\ui\Client.cs","$anetUiRoot\ui\ConfigWindow.cs","$anetUiRoot\ui\Bootstrap.cs","$anetUiRoot\ui\Updates.cs")
& $anetUiCompiler /nologo /codepage:65001 /target:winexe /main:ANetDesktop.Program /optimize+ /utf8output $anetUiRefs "/out:$OutputRoot\ANetDesktop.exe" $anetUiSources
if($LASTEXITCODE -ne 0){throw 'ANet configuration client build failed'}
if(Test-Path "$anetUiRoot\tests\ConfigUiCheck.cs"){
 & $anetUiCompiler /nologo /codepage:65001 /target:exe /main:ANetDesktop.ConfigUiCheck /optimize+ /utf8output $anetUiRefs "/out:$OutputRoot\ANetDesktop.ConfigUiCheck.exe" $anetUiSources "$anetUiRoot\tests\ConfigUiCheck.cs"
 if($LASTEXITCODE -ne 0){throw 'ANet configuration client checks build failed'}
}
