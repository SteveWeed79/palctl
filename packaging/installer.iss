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
#define AppVersion "0.1.0"
#define AppPublisher "palctl"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputBaseFilename=palctl-setup
OutputDir=Output
Compression=lzma2
SolidCompression=yes
; Registering a Windows service needs elevation.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
WizardStyle=modern

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
Name: "daemonservice"; Description: "Register and start the palctl background service now"

[Run]
; Self-register the daemon service (downloads NSSM on first use).
Filename: "{app}\palctl-daemon.exe"; Parameters: "install-service"; Tasks: daemonservice; Flags: runhidden waituntilterminated; StatusMsg: "Registering the palctl service..."
; Offer to launch the GUI (which runs the first-run wizard) at the end.
Filename: "{app}\palctl-gui.exe"; Description: "Launch palctl"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the service before the files go, so nothing is left pointing at a
; deleted exe. runhidden so an already-absent service fails quietly.
Filename: "{app}\palctl-daemon.exe"; Parameters: "uninstall-service"; Flags: runhidden waituntilterminated; RunOnceId: "RemovePalctlService"
