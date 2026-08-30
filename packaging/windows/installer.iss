#define MyAppName "Discount Parser"
#define MyAppVersion "0.1.16"
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
  ProbePath: String;
begin
  Result := True;
  if not FileExists(FilePath) then
    Exit;

  ProbePath := FilePath + '.upgrade-probe';
  if FileExists(ProbePath) then
    DeleteFile(ProbePath);

  if not RenameFile(FilePath, ProbePath) then
  begin
    Result := False;
    Exit;
  end;

  if not RenameFile(ProbePath, FilePath) then
  begin
    Log('DP-FB4: critical: could not restore upgrade probe file ' + FilePath);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  AppExe: String;
  WorkerExe: String;
begin
  Result := '';
  AppExe := ExpandConstant('{app}\{#MyAppExeName}');
  WorkerExe := ExpandConstant('{app}\{#MyWorkerExeName}');

  // Restart Manager normally closes the product, but customer upgrade evidence
  // showed that a stale/background image can survive long enough for the file
  // replacement to fail with Windows error 5. Kill only our two owned images,
  // wait for handles to drain, then probe both files before Setup starts copying.
  StopProductProcess('{#MyWorkerExeName}');
  StopProductProcess('{#MyAppExeName}');
  Sleep(1200);

  if not ProbeUnlockedFile(AppExe) then
  begin
    Result := 'Не удалось подготовить Discount Parser к обновлению. Перезагрузите Windows и снова запустите установщик до запуска программы.';
    Exit;
  end;

  if not ProbeUnlockedFile(WorkerExe) then
  begin
    Result := 'Не удалось подготовить фоновый процесс Discount Parser к обновлению. Перезагрузите Windows и снова запустите установщик.';
    Exit;
  end;
end;

procedure RemoveDesktopShortcutBestEffort();
var
  ShortcutPath: String;
begin
  ShortcutPath := DesktopShortcutPath();

  if FileExists(ShortcutPath) then
  begin
    if DeleteFile(ShortcutPath) then
      Log('DP-WIN-P0.2: removed existing Discount Parser desktop shortcut')
    else
      Log('DP-WIN-P0.2: warning: could not remove existing desktop shortcut; continuing installation');
  end;
end;

procedure CreateDesktopShortcutBestEffort();
var
  TargetPath: String;
  ShortcutPath: String;
  CreatedShortcut: String;
begin
  TargetPath := ExpandConstant('{app}\{#MyAppExeName}');
  ShortcutPath := DesktopShortcutPath();

  // DP-CUST-009: on an upgrade Inno Setup can remember that the optional
  // desktop task was not selected. The previous implementation deleted an
  // existing shortcut *before* checking that task and then exited, making the
  // customer's working icon disappear. If the task is not selected, preserve
  // whatever shortcut the user already has. If it is selected, refresh it.
  if not WizardIsTaskSelected('desktopicon') then
  begin
    Log('DP-CUST-009: desktop shortcut task not selected; preserving existing shortcut');
    Exit;
  end;

  RemoveDesktopShortcutBestEffort();

  if not FileExists(TargetPath) then
  begin
    Log('DP-WIN-P0.2: desktop shortcut skipped because installed executable is missing: ' + TargetPath);
    Exit;
  end;

  try
    CreatedShortcut := CreateShellLink(
      ShortcutPath,
      '{#MyAppName}',
      TargetPath,
      '',
      ExpandConstant('{app}'),
      '',
      0,
      SW_SHOWNORMAL);
    Log('DP-WIN-P0.2: created desktop shortcut: ' + CreatedShortcut);
  except
    Log('DP-WIN-P0.2: warning: desktop shortcut creation failed; installation continues: ' + GetExceptionMessage);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    CreateDesktopShortcutBestEffort();
end;

[Run]
Filename: "{app}\{#MyWorkerExeName}"; Parameters: "migrate"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Description: "Открыть Discount Parser"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Desktop link is created manually from [Code], therefore it is explicitly
; owned and removed here. `files` intentionally does not remove a directory
; that merely collides with the .lnk name (used by the resilience gate).
Type: files; Name: "{userdesktop}\{#MyDesktopShortcutName}"
Type: filesandordirs; Name: "{app}\_internal"