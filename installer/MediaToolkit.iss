; Inno Setup 6 script for Media Toolkit.
;
; Build it with tools\build.py, which runs PyInstaller first and passes
; /DAppVersion (from app\__init__.py) and /DSourceDir. Compiled by hand after
; "python -m PyInstaller MediaToolkit.spec", it reads the version from the
; built exe's version resource, which the spec writes from the same place.
;
; Per-user install into %LOCALAPPDATA%\Programs, so it never needs
; administrator rights and never shows a UAC prompt. There is deliberately no
; "install for all users" choice: the data folder and the downloaded packs
; are per user anyway, and an elevated install lands in the administrator's
; profile where other accounts cannot start it.
;
; User data (settings, speech models, logs, the on-demand GPU pack) lives in
; %LOCALAPPDATA%\Media Toolkit and survives upgrades. Downloaded videos and
; transcripts default to the user's Videos and Documents folders and are never
; touched by the uninstaller, not even the ones 1.1 saved inside the data folder.

#define AppName        "Media Toolkit"
#define AppExeName     "MediaToolkit.exe"
#define AppPublisher   "AnotherAH"
#define AppURL         "https://github.com/AnotherAH/media-toolkit"
#ifndef SourceDir
  #define SourceDir    "..\dist\MediaToolkit"
#endif
#ifndef AppVersion
  #define AppVersion   GetStringFileInfo(AddBackslash(SourceDir) + AppExeName, "ProductVersion")
#endif
#if AppVersion == ""
  #error Could not read the version. Build with tools\build.py or pass /DAppVersion=x.y.z
#endif

[Setup]
AppId={{8E4C1A22-6F3B-4A7D-9C15-2B7E5D3A9F41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
AppCopyright=Copyright (c) 2026 {#AppPublisher}. MIT License.
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
AllowNoIcons=yes
PrivilegesRequired=lowest
; License page: the MIT terms plus the end-user terms that some bundled
; runtime libraries (Intel, NVIDIA, Microsoft) ask to be passed on.
LicenseFile=terms.txt
OutputDir=..\dist
OutputBaseFilename=MediaToolkit-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
; Start Menu entry is created unconditionally - an installed app should be findable there.

[InstallDelete]
; An upgrade replaces the program files wholesale. Inno Setup never removes
; files a new version no longer ships, and a stale package folder left in
; _internal (1.1 shipped PyAV and mutagen there) could still be imported.
Type: filesandordirs; Name: "{app}\_internal"
Type: files;          Name: "{app}\README.md"

[Files]
Source: "{#SourceDir}\{#AppExeName}";            DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*";              DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\bin\*";                    DestDir: "{app}\bin"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\LICENSE.txt";              DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\THIRD-PARTY-NOTICES.txt";  DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";     Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty;     Name: "{app}\bin"
Type: dirifempty;     Name: "{app}"

[Code]
function DataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\{#AppName}');
end;

procedure DeleteItem(const Dir, Name: String; IsDir: Boolean);
begin
  if IsDir then
    DelTree(Dir + '\' + Name, True, True, True)
  else
    DelTree(Dir + '\' + Name, False, True, False);
end;

// Offer to remove what the app itself keeps in its data folder: settings,
// speech models (several GB), logs, sign-in data, caches and the GPU pack.
// Only those names are deleted. Anything else, and in particular the
// "downloads" and "transcripts" folders 1.1 created there, is left alone, and
// the folder itself goes only if nothing is left in it. Silent uninstalls keep
// everything (the default answer is No).
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Dir := DataDir();
    if DirExists(Dir) then
    begin
      if SuppressibleMsgBox('Also delete Media Toolkit''s settings, speech models and logs?'
              + #13#10 + #13#10 + Dir + #13#10 + #13#10
              + 'Your downloaded videos and transcripts are not deleted.'
              + ' Choose No to keep the settings and models for a future reinstall.',
              mbConfirmation, MB_YESNO, IDNO) = IDYES then
      begin
        DeleteItem(Dir, 'models', True);
        DeleteItem(Dir, 'runtime', True);
        DeleteItem(Dir, 'hf', True);
        DeleteItem(Dir, 'window', True);
        DeleteItem(Dir, 'login-profile', True);
        DeleteItem(Dir, 'config.json', False);
        DeleteItem(Dir, 'config.json.bak', False);
        DeleteItem(Dir, 'config.json.tmp', False);
        DeleteItem(Dir, 'config.corrupt-*.json', False);
        DeleteItem(Dir, '.backend-cache.json', False);
        DeleteItem(Dir, '.backend-cache.json.tmp', False);
        DeleteItem(Dir, 'cookies.txt', False);
        DeleteItem(Dir, 'cookies.txt.tmp', False);
        DeleteItem(Dir, 'history.json', False);
        DeleteItem(Dir, 'history.json.tmp', False);
        DeleteItem(Dir, 'instance.json', False);
        DeleteItem(Dir, 'instance.json.tmp', False);
        DeleteItem(Dir, 'instance.lock', False);
        DeleteItem(Dir, 'app.log', False);
        DeleteItem(Dir, 'app.log.*', False);
        DeleteItem(Dir, 'diagnose.txt', False);
        RemoveDir(Dir);
      end;
    end;
  end;
end;
