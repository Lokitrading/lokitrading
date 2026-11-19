; Windows installer for the Loki desktop app (Inno Setup 6). Built by .github/workflows/desktop-app.yml:
;   iscc /DAppVersion=2.84.0 /DSourceDir=dist\Loki /DOutputDir=installer desktop\loki.iss
; Installs for the current user only (no administrator prompt), adds Start-menu and desktop
; shortcuts, and uninstalls cleanly. Your saved data lives in %LOCALAPPDATA%\Loki and is kept
; when Loki is updated or uninstalled.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\Loki"
#endif
#ifndef OutputDir
  #define OutputDir "..\installer"
#endif

[Setup]
AppId={{6F1C2B8E-4C55-4E0B-9A7E-10C1D0A1B2C3}
AppName=Loki
AppVersion={#AppVersion}
AppVerName=Loki {#AppVersion}
AppPublisher=Loki
DefaultDirName={localappdata}\Programs\Loki
DefaultGroupName=Loki
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=Loki-Setup-Windows
SetupIconFile=..\loki.ico
UninstallDisplayIcon={app}\Loki.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; a new version replaces the old program files completely (your data folder is untouched)
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\Loki"; Filename: "{app}\Loki.exe"
Name: "{group}\Uninstall Loki"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Loki"; Filename: "{app}\Loki.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Loki.exe"; Description: "Open Loki now"; Flags: nowait postinstall skipifsilent
