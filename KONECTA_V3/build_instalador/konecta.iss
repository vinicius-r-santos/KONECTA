; Instalador do KONECTA (Inno Setup 6). Não compile direto: use BUILD.bat,
; que gera antes os executáveis em dist\KONECTA na ordem certa.
;
; O que ele faz, no clique:
;   - pede administrador (UAC): a câmera virtual só pode ser registrada em HKLM,
;     porque quem a carrega é o serviço de câmera do Windows (FrameServer);
;   - copia o KONECTA para Arquivos de Programas e registra a DLL da câmera;
;   - cria os atalhos. Nada de Python, venv ou runtime do VC++ para instalar:
;     tudo vai dentro (a DLL só depende de DLLs do próprio Windows).

#define AppVersao "1.0.0"

[Setup]
; Fixo para sempre: é por ele que o Windows reconhece uma atualização em vez
; de instalar um segundo KONECTA ao lado.
AppId={{454187EF-0415-43D8-BEF3-BBF3025052A1}
AppName=KONECTA
AppVersion={#AppVersao}
AppPublisher=KONECTA
DefaultDirName={autopf}\KONECTA
DisableProgramGroupPage=yes
PrivilegesRequired=admin
; A câmera virtual usa MFCreateVirtualCamera, que só existe no Windows 11.
MinVersion=10.0.22000
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=KONECTA_Setup_{#AppVersao}
SetupIconFile=..\..\konecta.ico
UninstallDisplayIcon={app}\KONECTA.exe
Compression=lzma2
SolidCompression=yes
LZMANumBlockThreads=8
WizardStyle=modern

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "atalho"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\KONECTA\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; regserver: chama o DllRegisterServer ao instalar e o DllUnregisterServer ao
; desinstalar. restartreplace: se mesmo assim a DLL antiga estiver presa,
; troca no próximo reinício em vez de falhar.
Source: "..\vcam\x64\Release\VCamSampleSource.dll"; DestDir: "{app}\vcam"; Flags: ignoreversion regserver restartreplace uninsrestartdelete

[Icons]
Name: "{autoprograms}\KONECTA"; Filename: "{app}\KONECTA.exe"
Name: "{autodesktop}\KONECTA"; Filename: "{app}\KONECTA.exe"; Tasks: atalho

[Run]
; runasoriginaluser: o instalador roda como administrador, o KONECTA não deve.
Filename: "{app}\KONECTA.exe"; Description: "{cm:LaunchProgram,KONECTA}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
// Numa atualização ou desinstalação, dois donos seguram arquivos da pasta:
// o serviço de câmera do Windows (a DLL, sempre que alguma câmera foi usada)
// e o avatar_server.exe, que continua no ar depois de fechar o KONECTA.
// Parar os dois antes evita o "arquivo em uso". O FrameServer volta sozinho
// quando um app abrir a câmera.
procedure LiberarArquivos;
var
  Codigo: Integer;
  Exe: String;
  Nomes: TArrayOfString;
  I: Integer;
begin
  Nomes := ['KONECTA.exe', 'avatar_server.exe', 'whisper_worker.exe', 'sinais_worker.exe'];
  for I := 0 to GetArrayLength(Nomes) - 1 do
  begin
    Exe := Nomes[I];
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ' + Exe, '', SW_HIDE, ewWaitUntilTerminated, Codigo);
  end;
  Exec(ExpandConstant('{sys}\net.exe'), 'stop FrameServer /y', '', SW_HIDE, ewWaitUntilTerminated, Codigo);
  Exec(ExpandConstant('{sys}\net.exe'), 'stop FrameServerMonitor /y', '', SW_HIDE, ewWaitUntilTerminated, Codigo);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  LiberarArquivos;
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    LiberarArquivos;
end;
