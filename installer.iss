; Inno Setup script for Snip Translator. Built by build.py, which passes /DAppVersion=<__version__>.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; AppId identifies the app across versions (upgrades replace it in place). Never change it.
AppId={{8F3C2B1A-6D4E-4A7B-9C1D-5E2F3A4B6C7D}
AppName=Snip Translator
AppVersion={#AppVersion}
AppVerName=Snip Translator {#AppVersion}
AppPublisher=George
VersionInfoVersion={#AppVersion}
; per-user install: no admin prompt
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Snip Translator
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=SnipTranslator-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\SnipTranslator.exe
UninstallDisplayName=Snip Translator
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
; the app holds this mutex while running: setup/uninstall asks you to close it first
AppMutex=SnipTranslatorSingleInstance

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"
Name: "startup"; Description: "Start Snip Translator when Windows starts"

[Files]
Source: "dist\SnipTranslator\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Snip Translator"; Filename: "{app}\SnipTranslator.exe"
Name: "{autodesktop}\Snip Translator"; Filename: "{app}\SnipTranslator.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SnipTranslator"; ValueData: """{app}\SnipTranslator.exe"""; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\SnipTranslator.exe"; Description: "Launch Snip Translator"; Flags: nowait postinstall skipifsilent

[Code]
// The tray's "Start with Windows" toggle can also set this value; remove it on uninstall either way.
// Settings, API key and log in %LOCALAPPDATA%\SnipTranslator are kept (reinstalling picks them up).
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'SnipTranslator');
end;
