"""Compara o snapshot com o estado real.

A chave e' sempre o session id. Titulo e posicao mentem: uma aba rotulada com o
nome de um projeto pode hospedar sessao de outro.
"""
from pathlib import Path
from typing import Optional, Tuple
import json

from lib.carimbo import ler, mais_recentes_primeiro

MAX_SNAPSHOT_AGE = 3


def _aplicados(base: Path):
    """Os snapshots de --apply, do mais recente ao mais antigo. Dry-run e snapshot sem
    tipo (gravado ate' a 0.2.0, quando dry-run e --apply nao se distinguiam) ficam fora."""
    if not base.exists():
        return
    for d in mais_recentes_primeiro([d for d in base.iterdir() if (d / "snapshot.json").exists()]):
        try:
            dados = json.loads((d / "snapshot.json").read_text())
        except (OSError, ValueError):
            continue
        if dados.get("tipo") == "apply":
            yield dados, d


def carregar_snapshot(base: Path, escolhido: Optional[Path] = None) -> Tuple[dict, Path]:
    if escolhido:
        try:
            return json.loads((escolhido / "snapshot.json").read_text()), escolhido
        except (OSError, ValueError) as e:
            raise RuntimeError("nao li o snapshot em %s (%s)" % (escolhido, e))
    for dados, d in _aplicados(base):
        return dados, d
    raise RuntimeError("nenhum snapshot de --apply em %s — rode o hibernate --apply antes "
                       "(snapshot de dry-run ou da 0.2.0 so' com --snapshot <dir>)" % base)


def ultimo_apply(base: Path) -> Optional[dict]:
    for dados, _ in _aplicados(base):
        return dados
    return None


def idade_em_dias(dados: dict, agora: float) -> float:
    return (agora - ler(dados["gerado_em"]).timestamp()) / 86400.0


def _abas_do_snapshot(dados: dict):
    for j in dados["janelas"]:
        for w in j["workspaces"]:
            for p in w["panes"]:
                for a in p["abas"]:
                    if a.get("sessao"):
                        yield j, w, a


def comparar(dados: dict, estado) -> dict:
    vivas_agora = {a.sessao for *_, a in estado.todas_abas()
                   if a.sessao and a.fonte == "processo"}
    uuids_atuais = {a.uuid for *_, a in estado.todas_abas()}
    r = {"vivas": [], "dormindo": [], "ausentes": [], "sem_transcript": []}
    for j, w, a in _abas_do_snapshot(dados):
        item = {"sessao": a["sessao"], "titulo": a.get("titulo", ""), "cwd": a.get("cwd"),
                "workspace": w["nome"], "janela": j["uuid"], "ws_uuid": w["uuid"],
                "surface": a["uuid"]}
        if not a.get("transcript"):
            r["sem_transcript"].append(item)
        elif a["sessao"] in vivas_agora:
            r["vivas"].append(item)
        elif a["uuid"] in uuids_atuais:
            r["dormindo"].append(item)
        else:
            r["ausentes"].append(item)
    return r
