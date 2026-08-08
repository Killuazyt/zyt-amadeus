#define ProductName "Amadeus Desktop Pet"
#define Publisher "Killuazyt"
#define ProductExe "Amadeus.exe"
#define ProductAppId "8739af85-a7c4-54f5-a318-5d15264178e9"
#define ProductMutex "Local\AmadeusDesktopPet-8739af85-a7c4-54f5-a318-5d15264178e9"

#ifndef SourceDir
  #error SourceDir must identify the verified bundled PyInstaller onedir
#endif
#ifndef OutputDir
  #error OutputDir must identify the installer output directory
#endif
#ifndef AppVersion
  #error AppVersion must be provided by build-installer.ps1
#endif
#ifndef OutputBaseFilename
  #error OutputBaseFilename must be provided by build-installer.ps1
#endif
#ifndef NumericVersion
  #error NumericVersion must be provided by build-installer.ps1
#endif
#ifndef AppIcon
  #error AppIcon must identify the generated Kurisu application icon
#endif
#ifndef ProjectLicense
  #error ProjectLicense must identify the project MIT license
#endif

[Setup]
AppId={{{#ProductAppId}}
AppName={#ProductName}
AppVersion={#AppVersion}
AppPublisher={#Publisher}
AppCopyright=Copyright (c) 2026 {#Publisher}
DefaultDirName={localappdata}\Programs\Amadeus
DefaultGroupName={#ProductName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppMutex={#ProductMutex}
CloseApplications=no
RestartApplications=no
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=no
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
SetupIconFile={#AppIcon}
LicenseFile={#ProjectLicense}
UninstallDisplayIcon={app}\{#ProductExe}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=no
ChangesEnvironment=no
AllowNoIcons=no
VersionInfoVersion={#NumericVersion}
VersionInfoCompany={#Publisher}
VersionInfoDescription={#ProductName} installer
VersionInfoProductName={#ProductName}
VersionInfoProductVersion={#NumericVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Start Amadeus when I sign in"; GroupDescription: "Additional tasks:"; Flags: unchecked; Check: IsFreshInstall

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ProjectLicense}"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\amadeus_desktop\resources\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#ProductName}"; Filename: "{app}\{#ProductExe}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#ProductName}"; Filename: "{uninstallexe}"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Amadeus"; ValueData: """{app}\{#ProductExe}"""; Tasks: autostart; Check: IsFreshInstall

[Code]
var
  DeleteUserData: Boolean;
  CleanupAttempted: Boolean;

function IsFreshInstall: Boolean;
begin
  Result := not RegKeyExists(
    HKCU,
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\{' +
      '{#ProductAppId}' + '}_is1'
  );
end;

function CommandLineRequestsDataDeletion: Boolean;
begin
  Result := CompareText(ExpandConstant('{param:DELETEUSERDATA|0}'), '1') = 0;
end;

function InitializeUninstall: Boolean;
begin
  DeleteUserData := CommandLineRequestsDataDeletion;
  CleanupAttempted := False;
  if (not DeleteUserData) and (not UninstallSilent) then
    DeleteUserData := MsgBox(
      'Delete all Amadeus local data and saved Windows credentials?' + #13#10 + #13#10 +
      'Choose No to keep local data for a later reinstall.',
      mbConfirmation,
      MB_YESNO or MB_DEFBUTTON2
    ) = IDYES;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if (CurUninstallStep = usUninstall) and (not CleanupAttempted) then
  begin
    CleanupAttempted := True;
    if DeleteUserData then
    begin
      if not Exec(
        ExpandConstant('{app}\{#ProductExe}'),
        '--uninstall-cleanup=delete-data',
        ExpandConstant('{app}'),
        SW_HIDE,
        ewWaitUntilTerminated,
        ResultCode
      ) then
      begin
        if not UninstallSilent then
          MsgBox(
            'Amadeus could not start the local-data cleanup. Uninstall was stopped and the program was kept.',
            mbCriticalError,
            MB_OK
          );
        Abort;
      end;
      if ResultCode <> 0 then
      begin
        if not UninstallSilent then
          MsgBox(
            'Amadeus could not safely delete all local data. Uninstall was stopped and the program was kept.',
            mbCriticalError,
            MB_OK
          );
        Abort;
      end;
    end;
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Amadeus');
  end;
end;
