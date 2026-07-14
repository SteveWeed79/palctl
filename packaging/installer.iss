; Inno Setup script for palctl.
;
; Produces a single palctl-setup.exe that drops the frozen binaries into
; Program Files, adds Start-Menu shortcuts, and (optionally) registers the
; palctl daemon as an always-on Windows service. The Palworld *server* service
; is registered later by the first-run wizard, because the installer doesn't yet
; know where the server lives.
;
; Build order:  pyinstaller packaging\palctl.spec   ->   ISCC packaging\installer.iss
; (build.ps1 does both.)

#define AppName "palctl"
; AppVersion is injected by the release workflow (ISCC /DAppVersion=x.y.z)
; from palctl/__init__.py, so it can't drift from the code. The fallback
; marks ad-hoc local builds as such.
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#define AppPublisher "palctl"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
; A fixed AppId is what makes re-running the installer a clean in-place UPGRADE
; rather than a second parallel install: Inno recognises the existing palctl,
; installs to the same folder, and leaves the user's %APPDATA% config alone.
; Never change this GUID once released.
AppId={{8F2A6B14-3C9E-4D7A-BE85-1F0C6D9A2E37}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputBaseFilename=palctl-setup
OutputDir=Output
; The installer's own icon (and, via UninstallDisplayIcon below, the entry in
; Apps & features). Committed by packaging/make_icon.py; path is relative to
; this .iss file.
SetupIconFile=app-icon.ico
UninstallDisplayIcon={app}\palctl-gui.exe
Compression=lzma2
SolidCompression=yes
; Registering a Windows service needs elevation.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
WizardStyle=modern
; On upgrade, let Restart Manager close the GUI if it's holding a file.
CloseApplications=yes
RestartApplications=no

[Files]
; The whole PyInstaller onedir output.
Source: "..\dist\palctl\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\palctl"; Filename: "{app}\palctl-gui.exe"
Name: "{group}\palctl setup"; Filename: "{app}\palctl-gui.exe"; Comment: "Open palctl and its setup wizard"
Name: "{group}\Uninstall palctl"; Filename: "{uninstallexe}"
Name: "{autodesktop}\palctl"; Filename: "{app}\palctl-gui.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
; Off by default: the first-run wizard now chooses how the daemon runs in the
; background (password-free login startup by default, or a service). Registering
; a service here too would leave two daemons fighting for the control port. Tick
; this only for an unattended install with no wizard.
Name: "daemonservice"; Description: "Register the palctl background service now (advanced; the wizard normally handles this)"; Flags: unchecked
Name: "addtopath"; Description: "Add palctl to the PATH (use the ""palctl"" command in any terminal)"; Flags: unchecked

[Registry]
; Append {app} to the system PATH so palctl.exe works from any shell. Guarded
; by NeedsAddPath so a reinstall doesn't append a duplicate. Not removed on
; uninstall: safely editing PATH back out is riskier than one stale entry.
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; \
  ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; \
  Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
var
  ServiceWasRegistered: Boolean;

function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_LOCAL_MACHINE,
    'SYSTEM\CurrentControlSet\Control\Session Manager\Environment',
    'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  { Look for the dir bracketed by semicolons, case-insensitively. }
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

function ServiceExists(Name: string): Boolean;
var
  ResultCode: Integer;
begin
  { sc.exe query exits 0 for an existing service (running OR stopped), 1060 when
    it doesn't exist. }
  Result := False;
  if Exec(ExpandConstant('{sys}\sc.exe'), 'query ' + Name, '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := (ResultCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  { On an upgrade the palctl-daemon service holds palctl-daemon.exe open, which
    would block the file copy. Record whether it exists BEFORE stopping it, so
    the [Run] step can bring it back (see NeedsServiceRestart) — otherwise a
    wizard-registered service, whose owner never ticks the daemonservice task,
    would stay stopped until the next reboot. }
  ServiceWasRegistered := ServiceExists('palctl-daemon');
  Exec(ExpandConstant('{sys}\net.exe'), 'stop palctl-daemon', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;

function NeedsServiceRestart: Boolean;
begin
  { Restart the daemon ourselves only when it was already registered AND the
    user did not tick the daemonservice task — that task's [Run] step already
    re-registers and starts it, so we must not double up. }
  Result := ServiceWasRegistered and not WizardIsTaskSelected('daemonservice');
end;

[Run]
; Self-register the daemon service (downloads NSSM on first use).
Filename: "{app}\palctl-daemon.exe"; Parameters: "install-service"; Tasks: daemonservice; Flags: runhidden waituntilterminated; StatusMsg: "Registering the palctl service..."
; On an upgrade of an existing (wizard-registered) service, PrepareToInstall
; stopped it to free the exe; start it back so the watchdog/scheduler/bot don't
; stay dead until reboot. Skipped when the daemonservice task above already did it.
Filename: "{sys}\net.exe"; Parameters: "start palctl-daemon"; Check: NeedsServiceRestart; Flags: runhidden waituntilterminated; StatusMsg: "Restarting the palctl background service..."
; Offer to launch the GUI (which runs the first-run wizard) at the end.
Filename: "{app}\palctl-gui.exe"; Description: "Launch palctl"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the service before the files go, so nothing is left pointing at a
; deleted exe. runhidden so an already-absent service fails quietly.
Filename: "{app}\palctl-daemon.exe"; Parameters: "uninstall-service"; Flags: runhidden waituntilterminated; RunOnceId: "RemovePalctlService"
