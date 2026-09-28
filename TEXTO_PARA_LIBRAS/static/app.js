const alvo = document.getElementById("texto");
const status = document.getElementById("status");

// Fala chega mais rápido do que o avatar sinaliza. Sem fila, cada frase nova
// cortava a anterior no meio; com fila sem limite, o avatar iria ficando cada
// vez mais atrasado em relação ao áudio. Guardamos poucas e descartamos as
// mais antigas — numa legenda ao vivo o que importa é o que está sendo dito
// agora.
const MAX_FILA = 5;

const fila = [];
let plugin = null;
let consumindo = false;

function definirStatus(texto, classe) {
  status.textContent = texto;
  status.className = classe;
}

// O widget só instancia o player (window.plugin) depois de aberto.
// VLibras 7 (set/2026, servido pelo jsDelivr): abre por VLibrasWidget.open() e
// vive num shadow DOM. O [vw-access-button] do index.html continua na página
// mas perdeu o clique — clicar nele deixava o avatar em "carregando" para sempre.
// A primeira carga do avatar leva ~40s; as seguintes vêm do cache.
function aguardarPlugin() {
  return new Promise((resolve) => {
    let aberto = false;
    const checar = () => {
      if (window.plugin?.translate) return resolve(window.plugin);
      // Uma vez só: antes de carregar, cada open() injeta o script de novo.
      // open() ainda não existe nos primeiros ms (o carregador cria no DOMContentLoaded).
      if (!aberto && window.VLibrasWidget?.open) {
        window.VLibrasWidget.open();
        aberto = true;
      }
      setTimeout(checar, 1000);
    };
    checar();
  });
}

// O player não avisa quando termina de sinalizar: rastreando todos os eventos,
// todos disparam junto no instante do translate() (o gloss:end de 0s é o fim da
// frase ANTERIOR, que o novo translate interrompeu) e nada mais chega depois.
// player.status também não serve — trava em "playing" e nunca volta a "idle".
// Então estimamos a duração pelo tamanho da frase. Se o avatar cortar frases
// longas ou ficar parado à toa entre elas, ajuste MS_POR_PALAVRA.
const MS_ATE_COMECAR = 1200; // round-trip da API de tradução antes de sinalizar
const MS_POR_PALAVRA = 1700; // medido a 1x
const MS_MIN = 3000;
const MS_MAX = 20000;

// 1 / 1.5 / 2. Em áudio contínuo a fala sempre corre mais rápido que a Libras,
// então acelerar o avatar reduz o atraso e o descarte da fila — ao custo de
// legibilidade. 1.5 é o meio-termo; quem decide de verdade é quem lê os sinais.
const VELOCIDADE = 1.5;

function duracaoEstimada(texto) {
  const palavras = texto.trim().split(/\s+/).length;
  // só a parte animada escala com a velocidade; o preparo dos sinais não.
  const estimada = MS_ATE_COMECAR + (palavras * MS_POR_PALAVRA) / VELOCIDADE;
  return Math.min(Math.max(estimada, MS_MIN), MS_MAX);
}

function aguardarFimDoSinal(texto) {
  return new Promise((resolve) => setTimeout(resolve, duracaoEstimada(texto)));
}

async function consumirFila() {
  if (consumindo) return;
  consumindo = true;
  while (fila.length) {
    const texto = fila.shift();
    alvo.textContent = texto;
    aplicarVelocidade(0); // vira no-op assim que o rótulo bate
    plugin.translate(texto);
    await aguardarFimDoSinal(texto);
  }
  consumindo = false;
}

function traduzir(texto) {
  fila.push(texto);
  while (fila.length > MAX_FILA) fila.shift();
  if (plugin) consumirFila();
  else alvo.textContent = texto;
}

function conectar() {
  const sock = new WebSocket(`ws://${location.host}/ws`);

  sock.onopen = () => definirStatus(plugin ? "pronto" : "carregando avatar…", "on");
  sock.onmessage = (evento) => traduzir(evento.data);
  sock.onclose = () => {
    definirStatus("desconectado — tentando novamente…", "off");
    setTimeout(conectar, 2000);
  };
}

// Na VLibras 7 o player não expõe setSpeed: a velocidade é uma lista de opções
// (0.5x a 2.5x, com ponto) no shadow DOM, que o botão "Alterar velocidade" só
// mostra — clicar nele não troca nada. Clicar na opção mantém rótulo e avatar em
// sincronia. Insiste até o rótulo bater: a lista só existe depois que o avatar
// carrega, por isso conferimos a cada tentativa em vez de clicar e torcer.
function aplicarVelocidade(tentativas = 90) {
  const raiz = document.getElementById("vlibras-app-root")?.shadowRoot;
  const semEspaco = (el) => el?.textContent.replace(/\s/g, "");
  const alvo = `${VELOCIDADE}x`;
  if (semEspaco(raiz?.querySelector('button[aria-label="Alterar velocidade"]')) === alvo) return;
  [...(raiz?.querySelectorAll("li > button") || [])].find((b) => semEspaco(b) === alvo)?.click();
  if (tentativas > 0) setTimeout(() => aplicarVelocidade(tentativas - 1), 500);
}

aguardarPlugin().then((p) => {
  plugin = p;
  aplicarVelocidade();
  definirStatus("pronto", "on");
  consumirFila();
});
conectar();

window.traduzir = traduzir;
