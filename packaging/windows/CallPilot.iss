; AppVersion is supplied by CI from pyproject.toml. Never maintain a second version.
#ifndef AppVersion
  #error AppVersion must be supplied from pyproject.toml
#endif

[Setup]
AppId={{C405F9F7-7D68-4AA6-A6D3-5ADFC8266EA4}
AppName=CallPilot
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\CallPilot
DefaultGroupName=CallPilot
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=CallPilot-{#AppVersion}-setup
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\CallPilot.exe
SetupIconFile=..\CallPilot.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Start CallPilot when I sign in"; Flags: unchecked

[Files]
Source: "..\..\dist\CallPilot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\CallPilot\CallPilot"; Filename: "{app}\CallPilot.exe"
Name: "{userprograms}\CallPilot\Uninstall CallPilot"; Filename: "{uninstallexe}"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CallPilot"; ValueData: """{app}\CallPilot.exe"" --login"; Tasks: autostart; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "CallPilot"; Flags: deletevalue; Check: NotAutoStart

[Run]
Filename: "{app}\CallPilot.exe"; Description: "Launch CallPilot"; Flags: nowait postinstall skipifsilent

[Code]
function NotAutoStart: Boolean;
begin
  Result := not WizardIsTaskSelected('autostart');
end;

function StopInstalledApp: Boolean;
var
  Code: Integer;
  Exe: String;
begin
  Exe := ExpandConstant('{app}\CallPilot.exe');
  Result := True;
  if FileExists(Exe) then
    Result := Exec(Exe, '--shutdown', ExpandConstant('{app}'), SW_HIDE,
      ewWaitUntilTerminated, Code) and (Code = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not StopInstalledApp then
    Result := 'CallPilot could not stop gracefully. Quit it and retry; no files were replaced.';
end;

function InitializeUninstall: Boolean;
begin
  Result := StopInstalledApp;
  if not Result then
    MsgBox('CallPilot could not stop gracefully. Quit it and retry.', mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    MsgBox('User data has been kept in ' + ExpandConstant('{localappdata}\CallPilot') +
      '. You may delete this folder manually.', mbInformation, MB_OK);
end;
