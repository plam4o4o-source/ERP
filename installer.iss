; Inno Setup скрипт за ПачоЛогистик — прави стандартен Windows инсталатор
; (Старт меню, десктоп икона, регистрация в "Добавяне/премахване на програми",
; деинсталатор) вместо гол .exe файл. Компилира се от GitHub Actions с:
;   iscc installer.iss /DMyAppVersion=1.2.3
; MyAppVersion по подразбиране е 0.0.0, ако не е подадена отвън.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppName "ПачоЛогистик"
#define MyAppPublisher "ПачоЛогистик"
#define MyAppExeName "PachoLogistic.exe"

[Setup]
AppId={{6C6E1F0E-6E52-4B90-9B7B-9E7F2B6E6A21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Приложението пази базата данни до .exe файла, затова инсталираме в папка
; на текущия потребител (без нужда от администраторски права за инсталация
; или за писане на базата данни при всеки старт) — стандартно за модерни
; Windows приложения (напр. VS Code, Slack).
DefaultDirName={localappdata}\Programs\PachoLogistic
PrivilegesRequired=lowest
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist_installer
; Без версия в името на файла, за да остане линкът към "latest/download"
; в GitHub Releases постоянен между версиите.
OutputBaseFilename=PachoLogistic-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=assets\icon.ico

[Languages]
Name: "bulgarian"; MessagesFile: "compiler:Languages\Bulgarian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\PachoLogistic.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
