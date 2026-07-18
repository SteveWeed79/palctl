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
; The addtopath task edits the system PATH; this broadcasts WM_SETTINGCHANGE so
; a newly-opened terminal sees `palctl` without a logoff/logon.
ChangesEnvironment=yes

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
  LoginStartupWasRegistered: Boolean;

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
  { The wizard's DEFAULT background mode is login startup: a plain
    palctl-daemon.exe process in the user's session, invisible to the service
    manager but holding the exe open just the same. Record its Run key (so
    the [Run] section can bring the daemon back), then kill the process.
    taskkill exits nonzero when there is no such process; that's fine.
    NB: no comment line may BEGIN with a bracket — ISCC reads a line-leading
    bracket as a section tag even inside a Code comment, and exactly that
    broke the 1.0.0 release build. }
  { Probe the ORIGINAL user's hive, not the elevated one: Setup runs elevated,
    and when a standard user elevates with a separate admin account, plain
    RegValueExists on HKCU reads the ADMIN's hive — missing the real user's
    Run key, so their daemon would be killed and never brought back. reg.exe
    run as the original user reads the right hive; it exits 0 when the value
    exists. }
  LoginStartupWasRegistered :=
    ExecAsOriginalUser(ExpandConstant('{sys}\reg.exe'),
      'query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v palctl-daemon',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM palctl-daemon.exe', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;

function DaemonServiceRunning: Boolean;
var
  ResultCode: Integer;
begin
  { findstr exits 0 only when the SCM reports the RUNNING state — sc.exe's own
    exit code is 0 for a stopped service too, so it can't be trusted alone. }
  Result := Exec(ExpandConstant('{cmd}'),
    '/C sc query palctl-daemon | findstr /C:"RUNNING"', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure RestartDaemonAfterUpgrade;
var
  ResultCode: Integer;
  Tries: Integer;
begin
  { The daemonservice task's [Run] entry re-registers and starts the service
    itself; doing anything here would double up. }
  if WizardIsTaskSelected('daemonservice') then
    exit;
  if ServiceWasRegistered then
  begin
    Exec(ExpandConstant('{sys}\net.exe'), 'start palctl-daemon', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
    { VERIFY the start instead of assuming it — net start's result used to be
      ignored, which left machines with NO daemon after an upgrade whenever
      the registration was stale (an old exe path or old arguments from a
      previous install) and could not actually start. Give a slow service a
      moment to reach RUNNING before concluding. }
    for Tries := 1 to 15 do
    begin
      if DaemonServiceRunning then
        exit;
      Sleep(1000);
    end;
    Log('palctl-daemon service did not reach RUNNING after the upgrade.');
    { A dead service registration must not leave the box daemon-less: if login
      startup is registered too — the wizard's default, and the common state
      after older palctl versions left a stale service behind — fall back to
      the login-mode daemon rather than exiting with nothing running. }
    if not LoginStartupWasRegistered then
      exit;
  end;
  if LoginStartupWasRegistered then
    { Relaunch the way the Run key would at login — as the original,
      non-elevated user, so it reads that user's config and DPAPI secrets
      (the Discord token). }
    ExecAsOriginalUser(ExpandConstant('{app}\palctl-daemon.exe'),
      'run --headless', '', SW_HIDE, ewNoWait, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RestartDaemonAfterUpgrade;
end;

[Run]
; Self-register the daemon service (downloads the WinSW wrapper on first use).
Filename: "{app}\palctl-daemon.exe"; Parameters: "install-service"; Tasks: daemonservice; Flags: runhidden waituntilterminated; StatusMsg: "Registering the palctl service..."
; The upgrade restart of an existing daemon (service or login-startup mode)
; happens in RestartDaemonAfterUpgrade above — in [Code], not here, because it
; VERIFIES the service actually reached RUNNING and falls back to the
; login-mode daemon when a stale registration can't start. Blind [Run] entries
; can't express "check, then fall back".
; Offer to launch the GUI (which runs the first-run wizard) at the end.
Filename: "{app}\palctl-gui.exe"; Description: "Launch palctl"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the service before the files go, so nothing is left pointing at a
; deleted exe. runhidden so an already-absent service fails quietly.
Filename: "{app}\palctl-daemon.exe"; Parameters: "uninstall-service"; Flags: runhidden waituntilterminated; RunOnceId: "RemovePalctlService"
; A login-mode daemon (and an open GUI) isn't a service, so nothing above
; stops it — but it holds its exe open, which would leave orphaned files in
; Program Files and a ghost daemon running from a half-deleted folder. The
; service case is already stopped by uninstall-service, so this only ever
; hits the login-mode process. taskkill exits nonzero when nothing matched.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM palctl-daemon.exe"; Flags: runhidden waituntilterminated; RunOnceId: "KillPalctlDaemon"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM palctl-gui.exe"; Flags: runhidden waituntilterminated; RunOnceId: "KillPalctlGui"
; Also clear the login-startup HKCU Run key (the wizard's DEFAULT background
; mode), or an autorun pointing at the just-deleted exe is left behind.
Filename: "{app}\palctl-daemon.exe"; Parameters: "uninstall-startup"; Flags: runhidden waituntilterminated; RunOnceId: "RemovePalctlStartup"
