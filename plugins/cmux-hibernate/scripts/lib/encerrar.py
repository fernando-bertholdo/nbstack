"""Desarma de verdade: encerra o claude ocioso da aba e confere no autosave do cmux.

Por que encerrar, e nao reescrever o binding (medido no cmux 0.65.2, TECH-1307):
- `cmux surface resume set --source agent-hook` sem `auto_resume: true` e' recusado
  quando a aba ja tem um binding `agent-hook` confiavel, e o CLI imprime `OK` mesmo
  assim. Foi o que deixou 11 abas subirem sozinhas em 31/10/2026;
- quando o `claude` sai, o hook `SessionEnd` tira do autosave o `agent` e o binding
  da aba, e a memoria sai junto.

Para aba armada sem processo (o restore dela falhou, por exemplo), a reserva e'
`cmux rpc surface.resume.clear` com `agent_session_ended: true`. Esse parametro e'
contrato interno do hook do cmux, nao aparece no `--help`: por isso e' so' reserva,
e o resultado so' vale conferido no autosave.
"""
import json
import os
import signal
import subprocess
import time
from typing import Callable, Dict, Iterable, Optional, Set

from lib.autosave import ler_autosave
from lib.cmux_state import rodar_cmux

JANELA_OCUPADA = 60   # s: transcript escrito ha menos que isso = sessao em turno
TETO_SAIDA = 10.0     # s para o claude sair depois do SIGTERM
TETO_CONFERENCIA = 20.0  # s para o cmux regravar o autosave (o timer dele e' de 8 s)
_STATUS_OCUPADO = {"running", "needsInput", "error"}
_CICLO_OCUPADO = {"running", "needsInput", "backgroundWorkPending"}


def motivo_ocupada(sessao: str, transcript_mtime: Optional[float], hooks: Dict[str, dict],
                   agora: float, janela: int = JANELA_OCUPADA) -> Optional[str]:
    """Por que nao encerrar agora; None quando a sessao esta ociosa.

    Dois sinais baratos, e basta um: o transcript `.jsonl` escrito nos ultimos `janela`
    segundos, e o registro do hook do cmux dizendo que ha turno, pedido de permissao ou
    trabalho de fundo (subagente) pendente. O transcript nao e' escrito durante uma
    ferramenta longa; o registro do hook cobre esse caso quando existe."""
    if transcript_mtime is not None and agora - transcript_mtime < janela:
        return "transcript escrito ha %d s" % max(0, int(agora - transcript_mtime))
    reg = hooks.get(sessao) or {}
    if reg.get("runtimeStatus") in _STATUS_OCUPADO:
        return "hook do cmux diz %s" % reg["runtimeStatus"]
    if reg.get("agentLifecycle") in _CICLO_OCUPADO:
        return "hook do cmux diz %s" % reg["agentLifecycle"]
    return None


def _ppid(pid: str) -> Optional[str]:
    r = subprocess.run(["ps", "-o", "ppid=", "-p", pid], capture_output=True, text=True)
    saida = r.stdout.strip()
    return saida or None


def ancestrais(pid: Optional[str] = None,
               ppid: Optional[Callable[[str], Optional[str]]] = None) -> Set[str]:
    """O pid e toda a cadeia de pais. Encerrar um deles mataria quem roda o comando."""
    ppid = ppid or _ppid
    atual = pid or str(os.getpid())
    vistos: Set[str] = set()
    while atual and atual != "0" and atual not in vistos:
        vistos.add(atual)
        atual = ppid(atual)
    return vistos


def _env_de(pid: str) -> str:
    r = subprocess.run(["ps", "eww", "-o", "command=", "-p", pid],
                       capture_output=True, text=True)
    return r.stdout.replace("\n", " ")


def processo_confere(pid: str, surface: str,
                     env_de: Optional[Callable[[str], str]] = None) -> bool:
    """O pid ainda e' um claude daquela aba? Relido logo antes do SIGTERM."""
    linha = (env_de or _env_de)(pid)
    return ("/.local/bin/claude" in linha
            and ("CMUX_SURFACE_ID=%s" % surface.upper()) in linha.upper())


def _vivo(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def encerrar(pids: Iterable[str], matar=None, vivo=None, dormir=None, relogio=None,
             teto: float = TETO_SAIDA) -> Set[str]:
    """SIGTERM em cada pid e espera ate' o teto. Devolve os que nao sairam. Nunca SIGKILL."""
    matar, vivo = matar or os.kill, vivo or _vivo
    dormir, relogio = dormir or time.sleep, relogio or time.monotonic
    pendentes = set()
    for pid in pids:
        try:
            matar(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            continue
        pendentes.add(pid)
    limite = relogio() + teto
    while pendentes:
        pendentes = {p for p in pendentes if vivo(p)}
        if not pendentes or relogio() >= limite:
            break
        dormir(0.2)
    return pendentes


def limpar_registro(painel, rodar=None) -> str:
    """Pede ao cmux que esqueca o agente da aba. A resposta nao prova nada: confira."""
    payload = {"window_id": painel.janela, "workspace_id": painel.workspace,
               "surface_id": painel.surface, "checkpoint_id": painel.sessao,
               "source": "agent-hook", "agent_session_ended": True}
    return (rodar or rodar_cmux)("rpc", "surface.resume.clear", json.dumps(payload))


def conferir(surfaces: Iterable[str], depois_de: float, ler=None, dormir=None, relogio=None,
             teto: float = TETO_CONFERENCIA) -> Dict[str, Optional[str]]:
    """surface -> None (desarmada no autosave regravado depois de `depois_de`) ou o motivo
    da falha. So' vale um save posterior a acao; le de novo ate' o teto."""
    ler, dormir = ler or ler_autosave, dormir or time.sleep
    relogio = relogio or time.monotonic
    alvos = [s.upper() for s in surfaces]
    if not alvos:
        return {}
    limite = relogio() + teto
    falha = {s: "o cmux nao regravou o autosave em %d s" % teto for s in alvos}
    while True:
        try:
            mtime, paineis = ler()
        except RuntimeError as e:
            falha = {s: str(e) for s in alvos}
        else:
            if mtime > depois_de:
                falha = {}
                for s in alvos:
                    p = paineis.get(s)
                    if p is not None and p.armado:
                        falha[s] = "continua armada (%s)" % p.armado
                if not falha:
                    return {s: None for s in alvos}
        if relogio() >= limite:
            return {s: falha.get(s) for s in alvos}
        dormir(1.0)
