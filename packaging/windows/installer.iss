#define MyAppName "Discount Parser"
#define MyAppVersion "0.1.14"
#define MyAppExeName "DiscountParser.exe"
#define MyWorkerExeName "DiscountParserWorker.exe"
#define MyDesktopShortcutName "Discount Parser.lnk"

[Setup]
AppId={{E9D2A6B6-4F2B-4C7A-90EE-44C33AC43FD2}
AppMutex=DiscountParserMutex_{{E9D2A6B6-4F2B-4C7A-90EE-44C33AC43FD2}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\DiscountParser
; DP-WIN-001 physical recovery: Discount Parser has one supported per-user
; installation directory. Never inherit a historical /DIR test path or an old
; uninstall-registry directory, otherwise Setup can succeed in that old path
; while a stale worker survives in the real product directory.
UsePreviousAppDir=no
DisableDirPage=yes
DefaultGroupName={#MyAppName}
OutputDir=output
OutputBaseFilename=DiscountParser-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
; DP-WIN-001: never restart a stale pre-upgrade process after payload replacement.
; Interactive Setup launches the freshly installed UI from [Run] instead.
RestartApplications=no

[InstallDelete]
; DP-WIN-001: the worker is product-owned and must never survive an upgrade as
; a stale binary. Inno Setup includes [InstallDelete] files in CloseApplications
; / Restart Manager in-use detection before deletion, so a running old worker is
; closed first, the old image is deleted, and then [Files] installs the new one.
Type: files; Name: "{app}\{#MyWorkerExeName}"

[Files]
; `notimestamp` is deliberate DP-CI-001 reproducibility policy: source mtimes
; must not change the installer bytes between otherwise identical builds.
Source: "..\..\delivery\app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs notimestamp

[Tasks]
; DP-WIN-P0.2: the Desktop shortcut is optional. It is created from [Code]
; so a shell/ACL failure cannot roll back an otherwise valid per-user install.
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные ярлыки:"

[Icons]
; Keep the primary launch path installer-managed and independent from Desktop.
Name: "{group}\Discount Parser"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"

[Code]
function DesktopShortcutPath(): String;
begin
  Result := ExpandConstant('{userdesktop}\{#MyDesktopShortcutName}');
end;

procedure StopProductProcess(ImageName: String);
var
  ResultCode: Integer;
begin
  if Exec(
    ExpandConstant('{sys}\taskkill.exe'),
    '/F /T /IM ' + ImageName,
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode) then
  begin
    Log('DP-FB4: taskkill ' + ImageName + ' exit=' + IntToStr(ResultCode));
  end
  else
    Log('DP-FB4: taskkill could not start for ' + ImageName);
end;

function ProbeUnlockedFile(FilePath: String): Boolean;
var
  Probe: TFileStream;
begin
  Result := True;
  if not FileExists(FilePath) then
    exit;
  try
    Probe := TFileStream.Create(FilePath, fmOpenReadWrite or fmShareExclusive);
    try
      Result := True;
    finally
      Probe.Free;
    end;
  except
    Result := False;
  end;
end;

function WaitForUnlockedFile(FilePath: String; TimeoutMs: Cardinal): Boolean;
var
  Started: Cardinal;
begin
  Started := GetTickCount();
  repeat
    if ProbeUnlockedFile(FilePath) then
    begin
      Result := True;
      exit;
    end;
    Sleep(100);
  until GetTickCount() - Started >= TimeoutMs;
  Result := ProbeUnlockedFile(FilePath);
end;

procedure StopProductProcessesBestEffort();
var
  WorkerPath: String;
  AppPath: String;
begin
  AppPath := ExpandConstant('{app}\{#MyAppExeName}');
  WorkerPath := ExpandConstant('{app}\{#MyWorkerExeName}');

  if FileExists(AppPath) and not ProbeUnlockedFile(AppPath) then
    StopProductProcess('{#MyAppExeName}');
  if FileExists(WorkerPath) and not ProbeUnlockedFile(WorkerPath) then
    StopProductProcess('{#MyWorkerExeName}');

  if FileExists(AppPath) and not WaitForUnlockedFile(AppPath, 10000) then
    Log('DP-FB4: app executable is still locked after stop attempt');
  if FileExists(WorkerPath) and not WaitForUnlockedFile(WorkerPath, 10000) then
    Log('DP-FB4: worker executable is still locked after stop attempt');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    StopProductProcessesBestEffort();
end;

function DesktopShortcutExists(): Boolean;
begin
  Result := FileExists(DesktopShortcutPath());
end;

procedure RemoveDesktopShortcutBestEffort();
begin
  try
    if DesktopShortcutExists() then
    begin
      DeleteFile(DesktopShortcutPath());
      Log('DP-WIN-P0.2: removed old Desktop shortcut');
    end;
  except
    Log('DP-WIN-P0.2: failed to remove old Desktop shortcut, continuing');
  end;
end;

procedure CreateDesktopShortcutBestEffort();
var
  LinkPath: String;
begin
  if not WizardIsTaskSelected('desktopicon') then
  begin
    Log('DP-WIN-P0.2: Desktop shortcut task not selected; preserving existing shortcut');
    exit;
  end;

  LinkPath := DesktopShortcutPath();
  RemoveDesktopShortcutBestEffort();
  try
    CreateShellLink(
      LinkPath,
      'Discount Parser',
      ExpandConstant('{app}\{#MyAppExeName}'),
      '',
      ExpandConstant('{app}'),
      '',
      0,
      SW_SHOWNORMAL
    );
    Log('DP-WIN-P0.2: Desktop shortcut created');
  except
    Log('DP-WIN-P0.2: Desktop shortcut creation failed, continuing install');
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    CreateDesktopShortcutBestEffort();
end;

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить Discount Parser"; Flags: nowait postinstall skipifsilent
