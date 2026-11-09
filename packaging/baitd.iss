; Inno Setup 6 script. Build from the repo root after PyInstaller:
;   iscc /DAppVersion=<version> packaging\baitd.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1D3C2A-BA17-4D0A-9C2E-0B41D7A30300}
AppName=Baitd
AppVersion={#AppVersion}
AppPublisher=Baitd
DefaultDirName={localappdata}\Programs\Baitd
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputBaseFilename=Baitd-Setup
OutputDir=..\dist-installer
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
UninstallDisplayName=Baitd
SetupIconFile=baitd.ico
UninstallDisplayIcon={app}\Baitd.exe

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Baitd\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Baitd"; Filename: "{app}\Baitd.exe"
Name: "{autodesktop}\Baitd"; Filename: "{app}\Baitd.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Baitd.exe"; Description: "{cm:LaunchProgram,Baitd}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\Baitd.exe"; Flags: nowait; Check: IsUpdate

[Code]
function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:UPDATE|0}') = '1';
end;
