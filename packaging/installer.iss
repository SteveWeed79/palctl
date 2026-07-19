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
; The VC++ x64 runtime installer, Authenticode-verified at build time (see
; release.yml / build.ps1). Bundled so a fresh Windows box — which doesn't
; have the runtime and whose sparse root-cert store makes runtime HTTPS
; downloads fail — never needs to download it during setup. Extracted to
; {tmp}, run only when the runtime is actually missing, deleted afterward.
Source: "..\dist\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\palctl"; Filename: "{app}\palctl-gui.exe"
Name: "{group}\palctl setup"; Filename: "{app}\palctl-gui.exe"; Comment: "Open palctl and its setup wizard"
Name: "{group}\Uninstall palctl"; Filename: "{uninstallexe}"
Name: "{autodesktop}\palctl"; Filename: "{app}\palctl-gui.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
; There is deliberately NO "register the service now" task: it could only
; register a LocalSystem daemon with no config — a half-setup that either
; fights the wizard's registration or pairs a SYSTEM daemon with a
; user-account server (the split that blinds the watchdog). The wizard is the
; one supported setup path; unattended deployments script
; `palctl-daemon install-service` instead.
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

function VCRedistNeeded: Boolean;
var
  Installed: Cardinal;
begin
  { Same key palctl's own preflight reads. 64-bit install mode, so this sees
    the 64-bit registry view — where the x64 runtime registers. Missing key or
    Installed<>1 means the Palworld server would fail to launch: install it. }
  Result := True;
  if RegQueryDWordValue(HKEY_LOCAL_MACHINE,
    'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
    'Installed', Installed) then
    Result := (Installed <> 1);
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
  LoginStartupWasRegistered := RegValueExists(HKEY_CURRENT_USER,
    'Software\Microsoft\Windows\CurrentVersion\Run', 'palctl-daemon');
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM palctl-daemon.exe', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;

function NeedsServiceRestart: Boolean;
begin
  { Restart the daemon after the upgrade stopped it to free the exe — only
    when a service registration already existed (the wizard's doing). }
  Result := ServiceWasRegistered;
end;

function NeedsLoginDaemonRestart: Boolean;
begin
  { Same idea for the login-startup mode: bring the daemon back after the
    upgrade killed it, or the watchdog/scheduler/bot silently stay dead until
    the next sign-in. Skipped when a service is registered — one daemon only. }
  Result := LoginStartupWasRegistered and not ServiceWasRegistered;
end;

[Run]
; Install the VC++ x64 runtime when it's missing — the Palworld server won't
; launch without it, and this is exactly the fresh box that can't download it.
; 3010 (success, reboot required) is fine; /norestart defers the reboot.
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; Check: VCRedistNeeded; Flags: waituntilterminated; StatusMsg: "Installing the Visual C++ runtime (the Palworld server needs it)..."
; On an upgrade of an existing (wizard-registered) service, PrepareToInstall
; stopped it to free the exe; start it back so the watchdog/scheduler/bot don't
; stay dead until reboot.
Filename: "{sys}\net.exe"; Parameters: "start palctl-daemon"; Check: NeedsServiceRestart; Flags: runhidden waituntilterminated; StatusMsg: "Restarting the palctl background service..."
; Same for the login-startup mode (the wizard's DEFAULT): PrepareToInstall
; killed the running daemon to free the exe; relaunch it the way the Run key
; would at login — as the original, non-elevated user, so it reads that user's
; config and DPAPI secrets (the Discord token).
Filename: "{app}\palctl-daemon.exe"; Parameters: "run --headless"; Check: NeedsLoginDaemonRestart; Flags: runhidden nowait runasoriginaluser; StatusMsg: "Restarting palctl in the background..."
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
