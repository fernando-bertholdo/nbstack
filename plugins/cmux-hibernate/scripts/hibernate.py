#!/usr/bin/env python3
"""Hiberna as sessoes Claude Code do cmux: encerra as ociosas e confere que nao sobem.

O padrao e' dry-run: mostra o plano por aba e nao toca em nada. Com --apply, encerra
com SIGTERM o claude ocioso de cada aba-alvo (o que libera a memoria e, pelo hook
SessionEnd, tira a aba do restore do cmux), limpa o registro das abas armadas sem
processo e confere tudo no autosave que o cmux regrava. "Pode dar Cmd+Q" so' sai
quando todas as abas-alvo foram conferidas desarmadas.

Saida: 0 no caminho normal; 1 quando alguma aba-alvo nao foi conferida desarmada
(no dry-run, so' com --surface, quando a aba pedida nao seria); 2 em erro de leitura
do cmux ou de argumento.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import cmux_state  # noqa: E402
from lib.autosave import ler_autosave, ler_hooks  # noqa: E402
from lib.bindings import abas_que_casam  # noqa: E402
from lib.cmux_state import (  # noqa: E402
    STALE_DAYS,
    _ps_eww,
    detectar_duplicatas,
    ler_estado,
    parse_processos,
    versao_do_cmux,
)
from lib.diff import ultimo_para_inventario  # noqa: E402
from lib.encerrar import ancestrais, motivo_ocupada  # noqa: E402
from lib.inventario import completar_inventario  # noqa: E402
from lib.plano import (  # noqa: E402
    CONTROLE, DORMINDO, ENCERRAR, LIMPAR, PULAR, executar, para_snapshot, planejar)
from lib.snapshot import (  # noqa: E402
    BASE_ESTADO, aplicar_retencao, gravar, gravar_dry_run, serializar)

_VERBO = {ENCERRAR: "encerrar", LIMPAR: "limpar", DORMINDO: "dormindo", PULAR: "pular",
          CONTROLE: "controle"}


def _linha(p: dict) -> str:
    extra = p["motivo"] or ("esta aba, preservada" if p["acao"] == CONTROLE else "")
    return "    %-9s %s  %-40s %s" % (_VERBO[p["acao"]], p["sessao"][:8],
                                      (p["titulo"] or "")[:40], extra)


def _ocupada_agora(hooks):
    """Reconfere a ociosidade logo antes do SIGTERM, com o transcript relido do disco."""
    def ver(item):
        caminho = (item.get("_transcript") or {}).get("path")
        try:
            mtime = os.stat(caminho).st_mtime if caminho else None
        except OSError:
            mtime = None
        return motivo_ocupada(item["sessao"], mtime, ler_hooks() or hooks, time.time())
    return ver


def main() -> int:
    ap = argparse.ArgumentParser(description="Hiberna sessoes Claude Code no cmux.")
    ap.add_argument("--apply", action="store_true",
                    help="encerra as sessoes ociosas e confere (padrao: so' mostra o plano)")
    escopo = ap.add_mutually_exclusive_group()
    escopo.add_argument("--all", action="store_true",
                        help="inclui a aba de onde o comando foi chamado")
    escopo.add_argument("--surface", metavar="ABA",
                        help="so' esta aba (uuid ou ref surface:N), mesmo que seja a de "
                             "onde o comando foi chamado")
    ap.add_argument("--stale-days", type=int, default=STALE_DAYS)
    ap.add_argument("--estado", type=Path, default=BASE_ESTADO, metavar="DIR",
                    help="diretorio dos snapshots (padrao: %(default)s)")
    args = ap.parse_args()

    try:
        estado = ler_estado()
        _, paineis = ler_autosave()
    except RuntimeError as e:
        print("erro: %s" % e, file=sys.stderr)
        return 2
    hooks = ler_hooks()
    trazidas = completar_inventario(estado, paineis, ultimo_para_inventario(args.estado),
                                    raiz=cmux_state.RAIZ_PROJETOS)

    so_aba = None
    if args.surface is not None:
        achadas = abas_que_casam(estado, args.surface)
        if len(achadas) != 1:
            print("erro: %s abas casam com %r; --surface pede exatamente uma"
                  % ("nenhuma" if not achadas else len(achadas), args.surface), file=sys.stderr)
            return 2
        if not achadas[0].sessao:
            print("erro: a aba %s nao tem sessao Claude Code" % achadas[0].ref, file=sys.stderr)
            return 2
        so_aba = achadas[0].uuid

    controle = None if args.all else (os.environ.get("CMUX_SURFACE_ID") or "").upper() or None
    abas = [a for *_, a in estado.todas_abas() if a.sessao]
    duplicadas = detectar_duplicatas(parse_processos(_ps_eww()))
    plano = planejar(estado, controle, duplicadas, so_aba, ancestrais(), hooks)
    transcripts = {a.uuid: a.transcript for a in abas}
    for p in plano:
        p["_transcript"] = transcripts.get(p["surface"])

    print("Estado real: %d sessoes em %d workspaces, %d janelas" % (
        len(abas), sum(len(j.workspaces) for j in estado.janelas), len(estado.janelas)))
    print("  %d com processo, %d so' no autosave do cmux, %d dormindo desde o --apply anterior"
          % (sum(1 for a in abas if a.pid), trazidas["autosave"], trazidas["snapshot"]))
    estagnadas = [a for a in abas if a.estagnada]
    if estagnadas:
        print("\n  %d sem atividade ha mais de %d dias:" % (len(estagnadas), args.stale_days))
        for a in estagnadas:
            print("      %s  %s" % (a.sessao[:8], (a.titulo or "")[:58]))
    if duplicadas:
        print("\n  anomalia: %d sessoes aparecem em mais de uma aba; serao puladas"
              % len(duplicadas))

    print("\n  Plano:")
    for p in plano:
        print(_linha(p))

    escopo_nome = "surface" if so_aba else ("all" if args.all else "padrao")
    controle_snap = None if so_aba is not None and so_aba == controle else controle
    conta = {a: sum(1 for p in plano if p["acao"] == a) for a in _VERBO}

    if not args.apply:
        dados = serializar(estado, controle_snap, args.stale_days, tipo="dry-run",
                           plano=_limpo(plano), escopo=escopo_nome)
        destino = gravar_dry_run(dados, args.estado)
        print("\n  Encerraria %d, limparia %d, pularia %d; %d ja dormem (dry-run)."
              % (conta[ENCERRAR], conta[LIMPAR], conta[PULAR], conta[DORMINDO]))
        print("  Use --apply para valer. Nada foi encerrado nem escrito no cmux.")
        print("\n  Inventario do dry-run (fora da retencao): %s" % destino)
        if so_aba is not None and any(p["acao"] == PULAR for p in plano):
            print("  A aba pedida seria pulada e continuaria armada.", file=sys.stderr)
            return 1
        return 0

    executar(plano, paineis, ocupada_fn=_ocupada_agora(hooks))
    dados = serializar(estado, controle_snap, args.stale_days, tipo="apply",
                       plano=_limpo(plano), escopo=escopo_nome, versao_cmux=versao_do_cmux())
    destino = gravar(dados, args.estado)
    aplicar_retencao(args.estado)

    alvos = [p for p in plano if p["acao"] != CONTROLE]
    falhas = [p for p in alvos if not p["resultado"]["verificado"]]
    print("\n  Resultado, conferido no autosave do cmux:")
    for p in alvos:
        r = p["resultado"]
        print("    %-7s %s  %-40s %s" % ("ok" if r["verificado"] else "FALHOU", p["sessao"][:8],
                                         (p["titulo"] or "")[:40],
                                         r["erro"] or _feito(p)))
    print("\n  Desarmadas e conferidas: %d de %d abas-alvo" % (len(alvos) - len(falhas),
                                                              len(alvos)))
    if any(p["acao"] == CONTROLE for p in plano):
        print("  Esta aba continua armada: e' a unica que sobe quando o cmux reabrir.")
    print("\n  Snapshot: %s" % destino)
    if falhas:
        print("\n  Nao de Cmd+Q ainda: %d abas podem subir sozinhas ao reabrir." % len(falhas),
              file=sys.stderr)
        print("  Resolva as que FALHOU e rode de novo.", file=sys.stderr)
        return 1
    print("\n  Pode dar Cmd+Q. Nao abra as abas encerradas antes disso: abrir rearma a aba.")
    print("  Depois do Cmd+Q, espere uns 10 s antes de reabrir o cmux.")
    return 0


def _feito(p: dict) -> str:
    return {"sigterm": "encerrada", "sigterm+rpc": "encerrada; registro limpo pelo rpc",
            "rpc": "registro limpo pelo rpc"}.get(p["resultado"]["metodo"], "ja dormia")


def _limpo(plano):
    return [{k: v for k, v in p.items() if not k.startswith("_")} for p in para_snapshot(plano)]


if __name__ == "__main__":
    raise SystemExit(main())
