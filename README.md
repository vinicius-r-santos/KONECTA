# KONECTA

Comunicação em reuniões entre pessoas surdas e ouvintes, nos dois sentidos:

- **Libras → texto:** a pessoa surda sinaliza na webcam e o KONECTA escreve o
  sinal reconhecido como legenda **dentro da própria câmera**. Os outros
  participantes do Meet, Zoom ou Teams leem, sem instalar nada.
- **Fala → Libras:** o áudio da reunião é transcrito na própria máquina
  (Whisper) e sinalizado por um avatar do VLibras ao lado.

Projeto de pesquisa (TCC) sobre reconhecimento de Libras.

---

## Como funciona

```
 webcam ──► MediaPipe (mãos) ──► modelo BiLSTM ──► "OBRIGADO"
                                                      │
                                                      ▼
                                   câmera virtual "KONECTA" ──► Meet / Zoom / Teams
                                   (a imagem da webcam + a legenda embaixo)

 áudio do PC ──► Whisper (local) ──► texto ──► avatar VLibras
```

São três peças que conversam pela rede local:

| Peça | O que é | Porta |
|---|---|---|
| `KONECTA_V3/` | O aplicativo: janela, reconhecimento, áudio e câmera virtual (PyQt6) | — |
| `TEXTO_PARA_LIBRAS/` | Servidor da página do avatar VLibras | 8300 |
| `SIGNLAB/` | Laboratório de treino dos sinais, com o app de gravação para celular | 8100 |

Quem só usa o KONECTA não precisa do SIGNLAB: o modelo treinado já vai
dentro do instalador.

---

## Instalar (Windows 11)

1. Gere o instalador (ver *Gerar o instalador* abaixo) ou use um já gerado:
   `KONECTA_Setup_<versão>.exe`.
2. Dois cliques e confirme o pedido de administrador. É ele que permite
   registrar a câmera virtual no Windows.
3. Abra pelo atalho **KONECTA**. Na reunião, escolha a câmera **KONECTA**.

O instalador leva tudo junto: Python, modelos, Whisper e runtime do C++. A
máquina de destino não precisa de nada instalado antes. Requisitos:

- **Windows 11.** A câmera virtual usa uma API que não existe no Windows 10,
  e o instalador recusa.
- **Internet para o avatar.** O VLibras é servido pelo gov.br. O resto
  (reconhecimento e transcrição) roda sem internet.

---

## Desenvolvimento

Requisitos: Windows 11 e Python 3.11.

```bat
cd KONECTA_V3
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

REM O modelo de sinais roda num processo separado, com TensorFlow: ele não
REM convive com o MediaPipe no mesmo processo.
python -m venv .venv-temporal
.venv-temporal\Scripts\pip install keras tensorflow-cpu numpy joblib
```

O SIGNLAB e o servidor do avatar usam o Python do sistema, cada um com o seu
`requirements.txt`.

- **Abrir tudo:** `KONECTA.pyw`, na raiz. Sobe o SIGNLAB e o avatar e abre o
  KONECTA com a câmera virtual ligada, sem janela de terminal.
- **Câmera virtual:** compile e registre uma vez, como administrador. O passo
  a passo está em [`KONECTA_V3/vcam/LEIA-ME.md`](KONECTA_V3/vcam/LEIA-ME.md).
- **Gerar o instalador:** `KONECTA_V3\build_instalador\BUILD.bat` (~7 min,
  precisa do [Inno Setup 6](https://jrsoftware.org/isinfo.php)). Sai em
  `build_instalador\Output\`. Ele sempre embute o modelo mais novo de
  `KONECTA_V3/models/`, o mesmo que o KONECTA de desenvolvimento está usando.

---

## Treinar sinais

1. **Gravar.** No SIGNLAB (`http://localhost:8100`) ou pelo **app de celular**,
   uma página que abre no Chrome do Android e grava de qualquer lugar por um
   link com senha. Ver [`SIGNLAB/APP_ANDROID.md`](SIGNLAB/APP_ANDROID.md).
2. **Treinar e publicar sozinho.** Três minutos depois da última gravação do
   app, o SIGNLAB treina de novo. Se a acurácia passar do mínimo, publica o
   modelo em `KONECTA_V3/models/`, e o KONECTA troca de modelo sem precisar
   reiniciar.

A classe `(nenhum)` ensina o modelo a reconhecer "não é sinal nenhum" (mãos
paradas, coçar o rosto). Ela nunca aparece como legenda.

### Vocabulário atual

Modelo `exp4`, 15 sinais mais `(nenhum)`, acurácia de 96,4% no conjunto de
teste:

> Pai · Mãe · Filho · Filha · Cachorro · Passarinho · sim · não · obrigado ·
> de nada · entendi · não entendi · não sei · saber/sei · de novo/repetir

---

## Privacidade (LGPD)

- **O app do celular não guarda vídeo.** O vídeo é apagado logo depois de
  extrair os pontos das mãos; só esses números ficam no SIGNLAB.
- **Nenhum dado de gravação vai para o git.** O banco, os projetos e os
  códigos de acesso do app (`SIGNLAB/data/acesso.json`) estão no
  `.gitignore`. Os modelos publicados não guardam o nome de quem gravou.
- **O V-LIBRASIL (UFPE) não é usado no TCC,** por LGPD. O script de
  importação continua no repositório só para pesquisa local.

---

## Limitações conhecidas

- **O modelo vê só o formato e a orientação da mão,** não onde ela está no
  corpo, nem o movimento no espaço, nem a expressão facial. Sinais que mudam
  só por esses parâmetros podem se confundir.
- **Cerca de 2 s por sinal.** O modelo olha uma janela de 30 quadros antes de
  decidir. Reduzir isso exige retreinar com uma janela menor.
- **Poucas pessoas sinalizando.** Só 5 dos 15 sinais foram gravados por duas
  pessoas; os outros, por uma só. É de esperar que o modelo acerte menos com
  quem sinaliza diferente, e é aí que mais gravações ajudam.

---

## Estrutura

```
KONECTA/
├── KONECTA.pyw            abre tudo em desenvolvimento
├── KONECTA_V3/            o aplicativo
│   ├── app_central/       janela, reconhecimento, áudio, câmera virtual
│   ├── vcam/              DLL da câmera virtual (C++)
│   ├── build_instalador/  PyInstaller + Inno Setup
│   └── models/            modelos publicados pelo SIGNLAB
├── SIGNLAB/               treino dos sinais e app de gravação
├── TEXTO_PARA_LIBRAS/     servidor do avatar VLibras
└── KONECTA_V2/ ...        versões e experimentos anteriores, fora de uso
```

Detalhes de cada parte: [`KONECTA_V3/README.md`](KONECTA_V3/README.md),
[`SIGNLAB/README.md`](SIGNLAB/README.md) e
[`TEXTO_PARA_LIBRAS/README.md`](TEXTO_PARA_LIBRAS/README.md).

---

## Créditos

- [VLibras](https://vlibras.gov.br/): avatar e
  tradução texto → Libras (governo federal).
- [VCamSample](https://github.com/smourier/VCamSample) (MIT, Simon
  Mourier): base da câmera virtual.
- [MediaPipe](https://ai.google.dev/edge/mediapipe) (Google): pontos das mãos.
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper): transcrição
  local.

---

**Pesquisador:** Vinicius · **Atualizado em:** 28/09/2026
