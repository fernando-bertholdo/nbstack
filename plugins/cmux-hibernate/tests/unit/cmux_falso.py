"""Um cmux falso: arvore, processos, autosave em arquivo e os dois jeitos de desarmar.

Reproduz o que foi medido no cmux 0.65.2 (TECH-1307): o SIGTERM no claude dispara o
SessionEnd, que tira agent e binding do autosave, e o app regrava o arquivo. Com
`session_end_limpa=False` e `rpc_limpa=False`, ele responde OK e nada muda: e' o caso
de 31/10/2026, em que o "OK" do CLI nao valia nada.
"""
import contextlib
import json
import os
import pathlib
import signal
import time
from unittest import mock

from lib.cmux_state import Aba, Estado, Janela, Pane, Workspace, anexar_transcript

JANELA, WS = "A1000000-0000-4000-8000-000000000001", "A2000000-0000-4000-8000-000000000001"


def uuid(n):
    return "C%07d-0000-4000-8000-%012d" % (n, n)


def sessao(n):
    return "%08d-1111-4111-8111-%012d" % (n, n)


class CmuxFalso:
    """abas: lista de dicts {n, pid?, armada?, idade_transcript?}. n=1 e' a de controle."""

    def __init__(self, tmp, abas, session_end_limpa=True, rpc_limpa=True, nao_sai=()):
        self.tmp = pathlib.Path(tmp)
        self.abas = abas
        self.session_end_limpa, self.rpc_limpa = session_end_limpa, rpc_limpa
        self.nao_sai = set(nao_sai)
        self.vivos = {a["pid"]: a["n"] for a in abas if a.get("pid")}
        self.sinais, self.rpcs = [], []
        self.autosave = self.tmp / "autosave.json"
        self.hooks = self.tmp / "hooks.json"
        self.hooks.write_text(json.dumps({"sessions": {}}))
        self.raiz = self.tmp / "projetos"
        (self.raiz / "proj").mkdir(parents=True, exist_ok=True)
        agora = time.time()
        for a in abas:
            t = self.raiz / "proj" / (sessao(a["n"]) + ".jsonl")
            t.write_text('{"cwd": "/tmp/proj-%d"}\n' % a["n"])
            idade = a.get("idade_transcript", 3600)
            os.utime(t, (agora - idade, agora - idade))
        self.paineis = {uuid(a["n"]): self._terminal(a) for a in abas}
        self._gravar(agora - 120)

    def _terminal(self, a):
        if not a.get("armada", True):
            return {}
        s = sessao(a["n"])
        return {"agent": {"kind": "claude", "sessionId": s, "workingDirectory": "/tmp/p"},
                "resumeBinding": {"source": "agent-hook", "autoResume": True,
                                  "checkpointId": s, "kind": "claude"},
                "wasAgentRunning": bool(a.get("pid"))}

    def _gravar(self, mtime=None):
        panels = [{"id": u, "type": "terminal", "title": "aba", "terminal": t}
                  for u, t in self.paineis.items()]
        dados = {"windows": [{"windowId": JANELA, "tabManager": {"workspaces": [
            {"workspaceId": WS, "panels": panels}]}}]}
        self.autosave.write_text(json.dumps(dados))
        t = time.time() + 1 if mtime is None else mtime
        os.utime(self.autosave, (t, t))

    def _desarmar(self, surface):
        self.paineis[surface] = {}
        self._gravar()

    def estado(self):
        abas = []
        for a in self.abas:
            vivo = a.get("pid") in self.vivos
            abas.append(Aba(uuid=uuid(a["n"]), ref="surface:%d" % a["n"], tipo="terminal",
                            titulo="aba %d" % a["n"], sessao=sessao(a["n"]) if vivo else None,
                            cwd="/tmp/proj-%d" % a["n"] if vivo else None,
                            fonte="processo" if vivo else None,
                            pid=a["pid"] if vivo else None))
        for a in abas:
            if a.sessao:
                anexar_transcript(a, self.raiz)
        return Estado(janelas=[Janela(uuid=JANELA, ref="window:1", workspaces=[
            Workspace(uuid=WS, ref="workspace:1", nome="projeto",
                      panes=[Pane(uuid="P", ref="pane:1", abas=abas)])])])

    def kill(self, pid, sinal):
        pid = str(pid)
        if pid not in self.vivos:
            raise ProcessLookupError
        if sinal == 0:
            return
        self.sinais.append((pid, sinal))
        if sinal == signal.SIGTERM and pid not in self.nao_sai:
            n = self.vivos.pop(pid)
            if self.session_end_limpa:
                self._desarmar(uuid(n))

    def env_de(self, pid):
        n = self.vivos.get(str(pid))
        if n is None:
            return ""
        return "/Users/x/.local/bin/claude --resume %s CMUX_SURFACE_ID=%s" % (sessao(n), uuid(n))

    def rodar(self, *args):
        if args[:2] == ("rpc", "surface.resume.clear"):
            payload = json.loads(args[2])
            self.rpcs.append(payload)
            if self.rpc_limpa:
                self._desarmar(payload["surface_id"])
            return "OK"
        raise AssertionError("chamada inesperada ao cmux: %r" % (args,))

    @contextlib.contextmanager
    def ligado(self, modulo, protegidos=()):
        """Troca no modulo do script as fontes do sistema por este cmux falso."""
        from lib import autosave, cmux_state, encerrar
        relogio = [1000.0]

        class Tempo:
            @staticmethod
            def monotonic():
                return relogio[0]

            @staticmethod
            def sleep(s):
                relogio[0] += s

            time = staticmethod(time.time)

        with contextlib.ExitStack() as pilha:
            for troca in (mock.patch.object(modulo, "ler_estado", self.estado),
                          mock.patch.object(autosave, "CAMINHO_AUTOSAVE", self.autosave),
                          mock.patch.object(autosave, "CAMINHO_HOOKS", self.hooks),
                          mock.patch.object(cmux_state, "RAIZ_PROJETOS", self.raiz),
                          mock.patch.object(encerrar, "rodar_cmux", self.rodar),
                          mock.patch.object(encerrar, "_env_de", self.env_de),
                          mock.patch.object(encerrar, "time", Tempo),
                          mock.patch("os.kill", self.kill)):
                pilha.enter_context(troca)
            if hasattr(modulo, "ancestrais"):
                pilha.enter_context(mock.patch.object(modulo, "ancestrais",
                                                      lambda: set(protegidos)))
            if hasattr(modulo, "_ps_eww"):
                pilha.enter_context(mock.patch.object(modulo, "_ps_eww", lambda: ""))
            if hasattr(modulo, "versao_do_cmux"):
                pilha.enter_context(mock.patch.object(modulo, "versao_do_cmux",
                                                      lambda: "cmux 0.65.2 (falso)"))
            yield self
