"""Testes do provider que consome modelos do SIGNLAB.

O teste mais importante aqui e' o de contrato: se o SIGNLAB mudar o layout de
features, o reconhecimento degradaria em silencio (predicao errada, nunca
excecao). O teste compara com o feature_config gravado dentro do modelo real.
"""

import asyncio
import glob
from pathlib import Path

import numpy as np
import pytest

from app_central.providers.base import ProviderIndisponivel
from app_central.providers.signlab_sinais import (
    TAMANHO_VETOR,
    SinaisSignlab,
    montar_vetor,
    normalizar_mao,
)

MODELOS_SIGNLAB = sorted(glob.glob("C:/KONECTA/SIGNLAB/projects/*/models/*.joblib"))


# Modelos de mentira precisam viver no modulo: joblib nao serializa classe local.
class ModeloOutroLayout:
    n_features_in_ = 64


class ModeloFalso:
    n_features_in_ = TAMANHO_VETOR

    def predict(self, _x):
        return ["X"]


def _mao(deslocamento=0.0):
    """21 pontos sinteticos, com o MCP do medio a uma distancia conhecida."""
    pontos = [[deslocamento, deslocamento, 0.0] for _ in range(21)]
    pontos[9] = [deslocamento + 2.0, deslocamento, 0.0]  # medio MCP
    return pontos


# ------------------------------------------------------------- normalizacao


def test_punho_vai_para_origem():
    v = normalizar_mao(_mao(deslocamento=5.0))
    assert v[0:3] == pytest.approx([0.0, 0.0, 0.0])


def test_escala_pela_distancia_ate_o_mcp():
    """Apos normalizar, o MCP do medio fica a distancia 1 do punho."""
    v = normalizar_mao(_mao())
    assert float(np.linalg.norm(v[27:30])) == pytest.approx(1.0, abs=1e-5)


def test_mao_degenerada_nao_divide_por_zero():
    pontos = [[1.0, 1.0, 1.0] for _ in range(21)]  # todos iguais: escala 0
    v = normalizar_mao(pontos)
    assert np.all(np.isfinite(v))


def test_invariante_a_posicao_na_imagem():
    """Mesma mao em cantos diferentes da tela gera o mesmo vetor."""
    a = normalizar_mao(_mao(deslocamento=0.0))
    b = normalizar_mao(_mao(deslocamento=10.0))
    assert a == pytest.approx(b, abs=1e-5)


# ------------------------------------------------------------- vetor


def test_sem_maos_devolve_none():
    assert montar_vetor({"left_hand": None, "right_hand": None}) is None


def test_layout_das_duas_maos():
    v = montar_vetor({"left_hand": _mao(), "right_hand": _mao()})
    assert v.shape == (TAMANHO_VETOR,)
    assert v[126] == 1.0 and v[127] == 1.0


def test_so_esquerda_marca_apenas_o_flag_dela():
    v = montar_vetor({"left_hand": _mao(), "right_hand": None})
    assert v[126] == 1.0
    assert v[127] == 0.0
    assert np.all(v[63:126] == 0.0), "faixa da direita tem de ficar zerada"


def test_so_direita_ocupa_a_faixa_certa():
    v = montar_vetor({"left_hand": None, "right_hand": _mao()})
    assert v[127] == 1.0
    assert np.all(v[0:63] == 0.0)
    assert np.any(v[63:126] != 0.0)


# ------------------------------------------------------------- contrato


@pytest.mark.skipif(not MODELOS_SIGNLAB, reason="nenhum modelo do SIGNLAB disponivel")
def test_contrato_bate_com_o_modelo_real():
    """Se o SIGNLAB mudar a extracao, isto acusa antes de virar predicao errada."""
    import joblib

    dados = joblib.load(MODELOS_SIGNLAB[0])
    config = dados["feature_config"]

    assert config["length"] == TAMANHO_VETOR
    assert config["points_per_hand"] == 21
    assert config["hands"] == 2
    assert config["normalization"] == "wrist_origin__scale_wrist_to_middle_mcp"
    assert config["layout"] == [
        "left_hand_xyz[63]",
        "right_hand_xyz[63]",
        "left_present",
        "right_present",
    ]
    assert dados["model"].n_features_in_ == TAMANHO_VETOR


@pytest.mark.skipif(not MODELOS_SIGNLAB, reason="nenhum modelo do SIGNLAB disponivel")
def test_modelo_real_aceita_nosso_vetor():
    """Ponta a ponta do que importa: o vetor que produzimos alimenta o modelo."""
    p = SinaisSignlab(caminho_modelo=MODELOS_SIGNLAB[0])
    p._carregar()
    vetor = montar_vetor({"left_hand": _mao(), "right_hand": _mao()})
    nome, confianca = p._prever(vetor)
    assert isinstance(nome, str) and nome
    assert 0.0 <= confianca <= 1.0


# ------------------------------------------------------------- erros


def test_modelo_ausente_e_erro_tratavel(tmp_path):
    p = SinaisSignlab(caminho_modelo=str(tmp_path / "nao_existe.joblib"))
    assert asyncio.run(p.disponivel()) is False
    with pytest.raises(ProviderIndisponivel):
        p._carregar()


def test_formato_errado_e_recusado(tmp_path):
    """Um joblib qualquer nao pode ser aceito como modelo do SIGNLAB."""
    import joblib

    caminho = tmp_path / "errado.joblib"
    joblib.dump({"coisa": 1}, caminho)
    p = SinaisSignlab(caminho_modelo=str(caminho))
    with pytest.raises(ProviderIndisponivel, match="bundle do SIGNLAB"):
        p._carregar()


def test_tamanho_incompativel_e_recusado(tmp_path):
    """Modelo com outro layout deve falhar claro, nao prever lixo."""
    import joblib

    caminho = tmp_path / "outro.joblib"
    joblib.dump(
        {"model": ModeloOutroLayout(), "class_names": {}, "feature_config": {"length": 64}},
        caminho,
    )
    p = SinaisSignlab(caminho_modelo=str(caminho))
    with pytest.raises(ProviderIndisponivel, match="features"):
        p._carregar()


def test_sem_maos_no_frame_nao_e_erro(monkeypatch, tmp_path):
    import joblib

    caminho = tmp_path / "m.joblib"
    joblib.dump(
        {
            "model": ModeloFalso(),
            "class_names": {"X": "X"},
            "feature_config": {"length": TAMANHO_VETOR},
        },
        caminho,
    )
    p = SinaisSignlab(caminho_modelo=str(caminho))
    monkeypatch.setattr(
        p, "_extrair_maos", lambda _f: {"left_hand": None, "right_hand": None}
    )
    r = asyncio.run(p.reconhecer(np.zeros((48, 64, 3), dtype=np.uint8)))
    assert r.texto == ""
    assert r.detalhes["status"] == "sem_maos"


@pytest.mark.skipif(
    SinaisSignlab(caminho_modelo="x")._caminho_landmarker() is None,
    reason="sem hand_landmarker.task",
)
def test_quadros_no_mesmo_milissegundo_nao_quebram_o_modo_video():
    """O modo VIDEO do MediaPipe exige marca de tempo ESTRITAMENTE crescente.

    Quadros em rajada (o "Medir acurácia" processa gravações em sequência, e o
    início da câmera entrega um lote de uma vez) caem no mesmo milissegundo —
    visto no build instalado: "Input timestamp must be monotonically increasing".
    """
    motor = SinaisSignlab(caminho_modelo="x")  # só a extração: o modelo não é carregado
    quadro = np.zeros((48, 64, 3), dtype=np.uint8)
    for _ in range(20):  # bem mais rápido que 1 quadro/ms
        motor._extrair_maos(quadro)
