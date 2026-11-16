; Routine Panel for Windows: the program PyInstaller builds (build.ps1), as one Setup.
; ISCC /DVersion=0.2.0 /DSource=<dist\RoutinePanel> routine-panel.iss
#define AppName "Routine Panel"
#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Source
  #define Source "..\..\dist\RoutinePanel"
#endif

[Setup]
AppId={{8C1E4A52-6B7D-4F0C-9C33-2F5B7A1D9E41}
AppName={#AppName}
AppVersion={#Version}
AppVerName={#AppName} {#Version}
AppPublisher=the mechanic
AppPublisherURL=https://themechanic.store/routine-panel/
AppSupportURL=https://themechanic.store/help/
DefaultDirName={autopf}\Routine Panel
DefaultGroupName=Routine Panel
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputBaseFilename=routine-panel-setup-{#Version}
SetupIconFile=routine-panel.ico
UninstallDisplayIcon={app}\RoutinePanel.exe
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no

[Messages]
BeveledLabel=HOME LAB · by the mechanic

[Tasks]
Name: "phone"; Description: "Let my phone see the panel: a small service starts when I sign in, and phones paired in the panel reach it over Tailscale or the home network. It only reads."; Flags: unchecked
Name: "desktopicon"; Description: "A shortcut on the desktop"; Flags: unchecked

[Files]
Source: "{#Source}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; the same AppUserModelID the program gives its window, so the taskbar shows it under this icon, not Chrome's
Name: "{group}\Routine Panel"; Filename: "{app}\RoutinePanel.exe"; AppUserModelID: "themechanic.RoutinePanel"
Name: "{autodesktop}\Routine Panel"; Filename: "{app}\RoutinePanel.exe"; AppUserModelID: "themechanic.RoutinePanel"; Tasks: desktopicon

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Routine Panel (phones)"""; Flags: runhidden; Tasks: phone
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Routine Panel (phones)"" dir=in action=allow protocol=TCP localport=47613 remoteip=LocalSubnet,100.64.0.0/10 profile=any program=""{app}\RoutinePanel.exe"""; Flags: runhidden; Tasks: phone
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /F /TN ""Routine Panel\Service"" /TR ""\""{app}\RoutinePanel.exe\"" --service"" /SC ONLOGON /RL LIMITED"; Flags: runhidden; Tasks: phone
Filename: "{sys}\schtasks.exe"; Parameters: "/Run /TN ""Routine Panel\Service"""; Flags: runhidden; Tasks: phone
Filename: "{app}\RoutinePanel.exe"; Description: "Open Routine Panel"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM RoutinePanel.exe"; Flags: runhidden; RunOnceId: "StopPanel"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""Routine Panel\Service"""; Flags: runhidden; RunOnceId: "RemoveService"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Routine Panel (phones)"""; Flags: runhidden; RunOnceId: "RemoveFirewallRule"
