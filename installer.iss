; Inno Setup скрипт за PH Logistics — прави стандартен Windows инсталатор
; (Старт меню, десктоп икона, регистрация в "Добавяне/премахване на програми",
; деинсталатор) вместо гол .exe файл. Компилира се от GitHub Actions с:
;   iscc installer.iss /DMyAppVersion=1.2.3
; MyAppVersion по подразбиране е 0.0.0, ако не е подадена отвън.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppName "PH Logistics"
#define MyAppPublisher "PH Logistics"
#define MyAppExeName "PHLogistics.exe"
; Одит (06.10.2026): старите технически имена (до v3.78) — само за прехода.
#define LegacyDir "{localappdata}\Programs\PachoLogistic"
#define LegacyExeName "PachoLogistic.exe"
; Годината се взима автоматично при КОМПИЛИРАНЕ на инсталатора (ISPP функция,
; изчислява се на момента на билда от GitHub Actions) — не се редактира ръчно,
; вижте и version_info.txt/release.yml за същия механизъм при самото .exe.
#define MyAppCopyright "© " + GetDateTimeString('yyyy', '', '') + " " + MyAppPublisher + ". Всички права запазени."

[Setup]
AppId={{6C6E1F0E-6E52-4B90-9B7B-9E7F2B6E6A21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppCopyright={#MyAppCopyright}
; Изрично зададени, за да не разчитаме на подразбиращото се извеждане на
; Inno Setup от AppName/AppVersion/AppPublisher — самият Setup.exe също
; трябва да носи коректни версия/издател/авторски права в Windows Properties.
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} — инсталатор
VersionInfoCopyright={#MyAppCopyright}
; Приложението пази базата данни до .exe файла, затова инсталираме в папка
; на текущия потребител (без нужда от администраторски права за инсталация
; или за писане на базата данни при всеки старт) — стандартно за модерни
; Windows приложения (напр. VS Code, Slack).
; Одит (06.10.2026): новата папка е PHLogistics. UsePreviousAppDir=no —
; преинсталиране върху стара инсталация в ...\Programs\PachoLogistic отива в
; новата папка (същият AppId — записът в „Приложения“ се подменя), а новото
; .exe премества данните при първия си старт (legacy_migration.py). Стара
; инсталация в ДРУГА, ръчно избрана папка остава там (GetDefaultDirName) —
; програмата чете и старите имена на файловете.
DefaultDirName={code:GetDefaultDirName}
UsePreviousAppDir=no
PrivilegesRequired=lowest
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist_installer
; Без версия в името на файла, за да остане линкът към "latest/download"
; в GitHub Releases постоянен между версиите.
OutputBaseFilename=PHLogistics-Setup
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
Source: "dist\PHLogistics.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[InstallDelete]
; Одит (05.10.2026): програмата е преименувана на „PH Logistics“ — старите
; преки пътища „ПачоЛогистик“ се махат, за да не останат два при преинсталиране.
Type: files; Name: "{group}\ПачоЛогистик.lnk"
Type: files; Name: "{autodesktop}\ПачоЛогистик.lnk"
; Одит (06.10.2026): САМО програмните файлове на старата локална инсталация
; (данните никога — тях ги мести новото .exe, а при съмнение ги оставя и сочи
; към тях). Не и ако старата папка е споделена в мрежата: други компютри може
; да пускат .exe-то оттам (LegacyProgramFilesRemovable).
Type: files; Name: "{#LegacyDir}\{#LegacyExeName}"; Check: LegacyProgramFilesRemovable
Type: files; Name: "{#LegacyDir}\{#LegacyExeName}.old"; Check: LegacyProgramFilesRemovable
Type: files; Name: "{#LegacyDir}\{#LegacyExeName}.new"; Check: LegacyProgramFilesRemovable
Type: files; Name: "{#LegacyDir}\unins000.exe"; Check: LegacyProgramFilesRemovable
Type: files; Name: "{#LegacyDir}\unins000.dat"; Check: LegacyProgramFilesRemovable
Type: files; Name: "{#LegacyDir}\unins000.msg"; Check: LegacyProgramFilesRemovable

[UninstallDelete]
; Одит (01.10.2026, O8): предишната версия, пазена от скрипта за обновяване за връщане назад.
Type: files; Name: "{app}\{#MyAppExeName}.old"
; Одит (06.10.2026): служебни файлове на новите имена (данните — никога).
Type: files; Name: "{app}\ph_update_*.bat"
Type: files; Name: "{app}\ph_update.log"

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6C6E1F0E-6E52-4B90-9B7B-9E7F2B6E6A21}_is1';
  SharesKey = 'SYSTEM\CurrentControlSet\Services\LanmanServer\Shares';

function NewDefaultDir(): String;
begin
  Result := ExpandConstant('{localappdata}\Programs\PHLogistics');
end;

function LegacyDefaultDir(): String;
begin
  Result := ExpandConstant('{#LegacyDir}');
end;

function SamePath(A, B: String): Boolean;
begin
  Result := CompareText(RemoveBackslashUnlessRoot(A), RemoveBackslashUnlessRoot(B)) = 0;
end;

{ Одит (06.10.2026): стара инсталация в ръчно избрана папка остава там. }
function GetDefaultDirName(Param: String): String;
var
  Prev: String;
begin
  Result := NewDefaultDir();
  if RegQueryStringValue(HKCU, UninstallKey, 'Inno Setup: App Path', Prev) then
    if (Prev <> '') and DirExists(Prev) and not SamePath(Prev, LegacyDefaultDir()) then
      Result := RemoveBackslashUnlessRoot(Prev);
end;

function PathIsInside(Path, Parent: String): Boolean;
begin
  Path := AddBackslash(AnsiLowercase(Path));
  Parent := AddBackslash(AnsiLowercase(Trim(Parent)));
  Result := (Length(Parent) > 1) and (Copy(Path, 1, Length(Parent)) = Parent);
end;

{ Същото правило като legacy_migration.read_share_paths: липсващ ключ = няма
  дялове; неразчетим регистър = приемаме „споделена“ (не пипаме). }
function LegacyDirIsShared(): Boolean;
var
  Names: TArrayOfString;
  I, P: Integer;
  Value, Rest, Line: String;
begin
  Result := True;
  if not RegKeyExists(HKLM, SharesKey) then
  begin
    Result := False;
    exit;
  end;
  if not RegGetValueNames(HKLM, SharesKey, Names) then
    exit;
  for I := 0 to GetArrayLength(Names) - 1 do
  begin
    if not RegQueryMultiStringValue(HKLM, SharesKey, Names[I], Value) then
      exit;
    Rest := Value + #0;
    while Rest <> '' do
    begin
      P := Pos(#0, Rest);
      if P = 0 then
        P := Length(Rest) + 1;
      Line := Copy(Rest, 1, P - 1);
      Delete(Rest, 1, P);
      if CompareText(Copy(Line, 1, 5), 'Path=') = 0 then
        if PathIsInside(LegacyDefaultDir(), Copy(Line, 6, Length(Line))) then
          exit;
    end;
  end;
  Result := False;
end;

{ Старите програмни файлове се трият само при инсталиране в новата папка по
  подразбиране и само ако старата папка не е споделена. }
function LegacyProgramFilesRemovable(): Boolean;
begin
  Result := SamePath(ExpandConstant('{app}'), NewDefaultDir()) and not LegacyDirIsShared();
end;
