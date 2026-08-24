; Inno Setup script for Media Toolkit.
;
; Per-user install into %LOCALAPPDATA%\Programs, so it never needs administrator
; rights and never triggers a UAC prompt. User data (settings, downloads,
; transcripts, Whisper models, the on-demand CUDA pack) lives separately under
; %LOCALAPPDATA%\Media Toolkit and is left alone on upgrade -- the uninstaller
; asks before removing it.

#define AppName        "Media Toolkit"
#define AppExeName     "MediaToolkit.exe"
#define AppPublisher   "Media Toolkit"
#define AppVersion     "1.1.1"
#define SourceDir      "..\dist\MediaToolkit"

[Setup]
AppId={{8E4C1A22-6F3B-4A7D-9C15-2B7E5D3A9F41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
AllowNoIcons=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
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

[Files]
Source: "{#SourceDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\bin\*";       DestDir: "{app}\bin";       Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md";             DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";     Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty;     Name: "{app}"

[Code]
function DataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\{#AppName}');
end;

// Offer to remove downloads, transcripts, models and settings on uninstall.
// Whisper models alone can be several GB, so leaving them silently is rude --
// but so is deleting someone's downloads without asking.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Dir := DataDir();
    if DirExists(Dir) then
    begin
      if MsgBox('Also delete your settings, downloaded media, transcripts and'
              + ' Whisper models?' + #13#10 + #13#10 + Dir + #13#10 + #13#10
              + 'Choose No to keep them for a future reinstall.',
              mbConfirmation, MB_YESNO) = IDYES then
        DelTree(Dir, True, True, True);
    end;
  end;
end;
