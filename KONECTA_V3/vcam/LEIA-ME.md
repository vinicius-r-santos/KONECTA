# Câmera virtual KONECTA

Uma câmera chamada **"KONECTA (Câmera Virtual do Windows)"** que o Meet, o Zoom
e o Teams enxergam como qualquer webcam. O KONECTA manda para ela a imagem da
webcam com a legenda do sinal reconhecido embaixo, para quem não sabe Libras.

```
webcam ─▶ KONECTA (reconhece, desenha a legenda) ─▶ memória compartilhada ─▶ esta DLL ─▶ serviço de câmera do Windows ─▶ Meet / Zoom
```

Usa a API nativa do Windows 11 (`MFCreateVirtualCamera`), sem OBS. A câmera
existe só enquanto o KONECTA está com ela ligada (botão **Câmera virtual**).

## Instalar numa máquina (uma vez)

1. Compilar (precisa das Build Tools do Visual Studio com C++):
   ```
   MSBuild VCamSample.sln -t:restore -p:RestorePackagesConfig=true
   MSBuild VCamSample.sln -t:build -p:Configuration=Release -p:Platform=x64
   ```
   O `nuget.config` desta pasta existe porque o NuGet desta máquina vinha sem
   nenhuma fonte de pacotes configurada.
2. Botão direito em **REGISTRAR_CAMERA.bat** → Executar como administrador.
   O Windows só aceita a DLL registrada em HKLM, porque quem a carrega é o
   serviço de câmera, não o KONECTA.

A pasta não pode ficar dentro de `C:\Users`: o serviço roda como *Local
Service* e não enxerga esse caminho.

## Depois de recompilar a DLL

O serviço de câmera segura a versão antiga na memória. Se o linker disser
LNK1104, renomeie `x64/Release/VCamSampleSource.dll` (o Windows deixa renomear
uma DLL em uso), compile de novo e rode **REINICIAR_SERVICO_CAMERA.bat** como
administrador. Pela resolução dá para saber qual versão está no ar: a atual
entrega 1280×720.

## Testado

| | |
|---|---|
| Google Meet, PC transmitindo e celular recebendo em outra conta | funciona |
| Zoom | não testado |
| Teams | não testado. O autor do código base registra que no Teams a imagem aparece na prévia, mas não chega aos outros participantes |

`LIGAR_CAMERA_TESTE.bat` liga a câmera sem o KONECTA, com o aviso de
"aguardando imagem", para testar só a chamada. Os testes automáticos estão em
`tests/test_camera_virtual.py` e abrem apenas a câmera KONECTA, nunca a webcam.

## Contrato com o KONECTA

Memória `Global\KonectaVCamFrame`: cabeçalho de 64 bytes e pixels BGRA, até
1920×1080 (`FrameGenerator.h` e `app_central/capture/camera_virtual.py`).
**Quem cria a memória é a DLL**, dentro do serviço de câmera: um processo comum
recebe erro 5 ao criar em `Global\`. O KONECTA só abre e escreve. A sequência
fica ímpar durante a escrita, para a DLL não pegar um quadro pela metade. Sem
quadro novo por 2 s, a câmera mostra "Aguardando imagem do app" em vez de
congelar.

## Origem

Baseado no [VCamSample](https://github.com/smourier/VCamSample) de Simon
Mourier, licença MIT (`LICENSE`), commit `58b6d4b0e0c2c97fdffd66d23e7c8da6b89f570d`.
Mudanças: CLSID próprio (`CLSID_KONECTA.txt`), nome KONECTA, saída 1280×720, e
o gerador de quadros (`FrameGenerator`) desenha a imagem do KONECTA em vez do
padrão de teste.
