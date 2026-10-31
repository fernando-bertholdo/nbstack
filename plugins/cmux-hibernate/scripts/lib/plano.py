"""O plano do hibernate, aba a aba, e a execucao do --apply.

Acoes:
    encerrar  processo vivo e ocioso: SIGTERM no claude da aba
    limpar    armada no autosave sem processo: rpc surface.resume.clear
    dormindo  sem processo e nao armada: nada a fazer
    pular     ocupada, sessao em mais de uma aba, ou a sessao que roda este comando
    controle  a aba de onde o comando roda, preservada (nao e' alvo)

Nenhum resultado e' "verificado" sem o autosave regravado dizer que a aba nao sobe.
"""
import time
from typing import Callable, Dict, Iterable, List, Optional

from lib.autosave import Painel
from lib.carimbo import agora_utc
from lib.encerrar import conferir, encerrar, limpar_registro, motivo_ocupada, processo_confere

ENCERRAR, LIMPAR, DORMINDO, PULAR, CONTROLE = "encerrar", "limpar", "dormindo", "pular", "controle"
PROPRIA = "e' a sessao que roda este comando: saia dela com /exit antes do Cmd+Q"


def planejar(estado, controle: Optional[str], duplicadas: Optional[Iterable[str]] = None,
             so_aba: Optional[str] = None, protegidos: Iterable[str] = (),
             hooks: Optional[Dict[str, dict]] = None, agora: Optional[float] = None) -> List[dict]:
    """Uma entrada por aba com sessao. Com so_aba (uuid), so' aquela aba, mesmo que seja a
    de controle: e' o que deixa uma sessao hibernar a propria aba, como no revezamento."""
    proibidas, protegidos = set(duplicadas or []), set(protegidos)
    hooks, agora = hooks or {}, time.time() if agora is None else agora
    plano = []
    for janela, ws, _pane, aba in estado.todas_abas():
        if not aba.sessao or (so_aba is not None and aba.uuid != so_aba):
            continue
        item = {"surface": aba.uuid, "janela": janela.uuid, "workspace": ws.uuid,
                "ws_nome": ws.nome, "sessao": aba.sessao, "cwd": aba.cwd, "titulo": aba.titulo,
                "fonte": aba.fonte, "pid": aba.pid, "armada": aba.armada, "acao": None,
                "motivo": None, "resultado": None}
        ocupada = motivo_ocupada(aba.sessao, (aba.transcript or {}).get("mtime"), hooks, agora)
        if aba.sessao in proibidas:
            item["acao"], item["motivo"] = PULAR, "sessao em mais de uma aba (anomalia)"
        elif so_aba is None and aba.uuid == controle:
            item["acao"] = CONTROLE
        elif aba.pid and aba.pid in protegidos:
            item["acao"], item["motivo"] = PULAR, PROPRIA
        elif aba.pid and ocupada:
            item["acao"], item["motivo"] = PULAR, "ocupada: " + ocupada
        elif aba.pid:
            item["acao"] = ENCERRAR
        elif aba.armada:
            item["acao"], item["motivo"] = LIMPAR, "armada (%s) sem processo" % aba.armada
        else:
            item["acao"] = DORMINDO
        plano.append(item)
    return plano


def para_snapshot(plano: List[dict]) -> List[dict]:
    """O pid nao vai ao snapshot: e' estado do momento e envelheceria em silencio."""
    return [{k: v for k, v in p.items() if k != "pid"} for p in plano]


def _resultado(item: dict, metodo: Optional[str], erro: Optional[str]) -> None:
    item["resultado"] = {"metodo": metodo, "verificado": erro is None, "quando": agora_utc(),
                         "erro": erro}


def _painel(item: dict, paineis: Dict[str, Painel]) -> Painel:
    p = paineis.get(item["surface"].upper())
    return Painel(surface=item["surface"], janela=p.janela if p else item["janela"],
                  workspace=p.workspace if p else item["workspace"],
                  sessao=(p.sessao if p and p.sessao else item["sessao"]), cwd=item["cwd"],
                  armado=item["armada"])


def executar(plano: List[dict], paineis: Dict[str, Painel], confere=processo_confere,
             encerrar_fn=encerrar, limpar_fn=limpar_registro, conferir_fn=conferir,
             ocupada_fn: Optional[Callable[[dict], Optional[str]]] = None,
             agora: Callable[[], float] = time.time) -> None:
    """Executa o plano e grava `resultado` em cada alvo. A aba de controle fica com None."""
    inicio = agora()
    sinalizar = []
    for item in plano:
        if item["acao"] != ENCERRAR:
            continue
        motivo = ocupada_fn(item) if ocupada_fn else None
        if motivo:
            item["acao"], item["motivo"] = PULAR, "ocupada: " + motivo
        elif not confere(item["pid"], item["surface"]):
            _resultado(item, None, "o processo %s mudou desde a leitura; nenhum sinal enviado"
                       % item["pid"])
        else:
            sinalizar.append(item)
    restantes = encerrar_fn([i["pid"] for i in sinalizar])
    sairam = []
    for item in sinalizar:
        if item["pid"] in restantes:
            _resultado(item, "sigterm", "o claude nao saiu em 10 s do SIGTERM; SIGKILL nao e' "
                                        "usado, encerre com /exit")
        else:
            sairam.append(item)
    limpar = [i for i in plano if i["acao"] == LIMPAR]
    for item in limpar:
        limpar_fn(_painel(item, paineis))
    res = conferir_fn([i["surface"] for i in sairam + limpar], depois_de=inicio)
    for item in limpar:
        _resultado(item, "rpc", res.get(item["surface"].upper()))
    reserva = [i for i in sairam if res.get(i["surface"].upper())]
    for item in sairam:
        if item not in reserva:
            _resultado(item, "sigterm", None)
    if reserva:
        marco = agora()
        for item in reserva:
            limpar_fn(_painel(item, paineis))
        res2 = conferir_fn([i["surface"] for i in reserva], depois_de=marco)
        for item in reserva:
            _resultado(item, "sigterm+rpc", res2.get(item["surface"].upper()))
    for item in plano:
        if item["acao"] == DORMINDO:
            _resultado(item, None, None)
        elif item["acao"] == PULAR:
            _resultado(item, None, item["motivo"])
