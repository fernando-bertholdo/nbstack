"""Encerrar, limpar e conferir: nenhum sucesso sem o autosave regravado dizer que sim."""
import json
import pathlib
import signal
import sys
import unittest
from unittest import mock

RAIZ = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "scripts"))
from lib import encerrar  # noqa: E402
from lib.autosave import Painel  # noqa: E402

SURF = "B0000001-0000-4000-8000-000000000001"


class Relogio:
    """Tempo falso: dormir avanca o relogio, e cada chamada ve o instante atual."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def dormir(self, s):
        self.t += s


class TestOcupada(unittest.TestCase):
    def test_transcript_recente_e_ocupada(self):
        m = encerrar.motivo_ocupada("s", 990.0, {}, agora=1000.0)
        self.assertIn("transcript escrito ha 10 s", m)

    def test_transcript_antigo_e_hook_ocioso_e_ociosa(self):
        hooks = {"s": {"runtimeStatus": "idle", "agentLifecycle": "idle"}}
        self.assertIsNone(encerrar.motivo_ocupada("s", 100.0, hooks, agora=1000.0))

    def test_hook_em_turno_ou_esperando_e_ocupada(self):
        for reg in ({"runtimeStatus": "running"}, {"runtimeStatus": "needsInput"},
                    {"runtimeStatus": "idle", "agentLifecycle": "backgroundWorkPending"},
                    {"runtimeStatus": "idle", "agentLifecycle": "running"}):
            self.assertIsNotNone(encerrar.motivo_ocupada("s", 100.0, {"s": reg}, agora=1000.0),
                                 reg)

    def test_sem_transcript_e_sem_hook_e_ociosa(self):
        self.assertIsNone(encerrar.motivo_ocupada("s", None, {}, agora=1000.0))


class TestAncestrais(unittest.TestCase):
    def test_sobe_a_cadeia_ate_o_launchd(self):
        pais = {"50": "40", "40": "30", "30": "1", "1": "0"}
        self.assertEqual(encerrar.ancestrais("50", ppid=pais.get), {"50", "40", "30", "1"})

    def test_para_em_ciclo_ou_falha_de_leitura(self):
        self.assertEqual(encerrar.ancestrais("50", ppid={"50": "50"}.get), {"50"})
        self.assertEqual(encerrar.ancestrais("50", ppid=lambda p: None), {"50"})


class TestProcessoConfere(unittest.TestCase):
    def test_exige_claude_e_a_mesma_aba(self):
        linha = "/Users/x/.local/bin/claude --resume abc CMUX_SURFACE_ID=%s HOME=/x" % SURF
        self.assertTrue(encerrar.processo_confere("9", SURF, env_de=lambda p: linha))
        self.assertTrue(encerrar.processo_confere("9", SURF.lower(), env_de=lambda p: linha))
        outra = linha.replace(SURF, "B0000002-0000-4000-8000-000000000002")
        self.assertFalse(encerrar.processo_confere("9", SURF, env_de=lambda p: outra))
        self.assertFalse(encerrar.processo_confere(
            "9", SURF, env_de=lambda p: "/bin/zsh CMUX_SURFACE_ID=%s" % SURF))
        self.assertFalse(encerrar.processo_confere("9", SURF, env_de=lambda p: ""))


class TestEncerrar(unittest.TestCase):
    def test_sigterm_e_espera_sair(self):
        r = Relogio()
        vivos = {"10", "20"}
        enviados = []

        def matar(pid, sinal):
            enviados.append((pid, sinal))
            vivos.discard(str(pid))

        restantes = encerrar.encerrar(["10", "20"], matar=matar, vivo=lambda p: p in vivos,
                                      dormir=r.dormir, relogio=r)
        self.assertEqual(restantes, set())
        self.assertEqual(enviados, [(10, signal.SIGTERM), (20, signal.SIGTERM)])

    def test_quem_nao_sai_no_teto_e_reportado_sem_sigkill(self):
        r = Relogio()
        enviados = []
        restantes = encerrar.encerrar(["10"], matar=lambda p, s: enviados.append(s),
                                      vivo=lambda p: True, dormir=r.dormir, relogio=r, teto=10)
        self.assertEqual(restantes, {"10"})
        self.assertEqual(enviados, [signal.SIGTERM])
        self.assertGreaterEqual(r.t, 1010.0)

    def test_processo_que_ja_saiu_nao_e_erro(self):
        def matar(p, s):
            raise ProcessLookupError

        self.assertEqual(encerrar.encerrar(["10"], matar=matar, vivo=lambda p: False,
                                           dormir=lambda s: None, relogio=Relogio()), set())


class TestLimpar(unittest.TestCase):
    def test_rpc_com_o_contrato_do_hook(self):
        rpc = mock.Mock(return_value='{"cleared": true}')
        p = Painel(surface=SURF, janela="W", workspace="WS", sessao="sess", cwd=None,
                   armado="agent")
        encerrar.limpar_registro(p, rodar=rpc)
        metodo, payload = rpc.call_args.args[1], json.loads(rpc.call_args.args[2])
        self.assertEqual((rpc.call_args.args[0], metodo), ("rpc", "surface.resume.clear"))
        self.assertEqual(payload, {"window_id": "W", "workspace_id": "WS", "surface_id": SURF,
                                   "checkpoint_id": "sess", "source": "agent-hook",
                                   "agent_session_ended": True})


def painel(armado):
    return Painel(surface=SURF, janela="W", workspace="WS", sessao="s", cwd=None, armado=armado)


class TestConferir(unittest.TestCase):
    def test_exige_save_posterior_a_acao(self):
        """Um autosave anterior a acao nao prova nada, mesmo dizendo 'desarmada'."""
        r = Relogio(1000.0)
        ler = mock.Mock(return_value=(999.0, {SURF: painel(None)}))
        res = encerrar.conferir([SURF], depois_de=1000.0, ler=ler, dormir=r.dormir, relogio=r,
                                teto=5)
        self.assertEqual(res, {SURF: "o cmux nao regravou o autosave em 5 s"})

    def test_desarmada_no_save_novo(self):
        r = Relogio(1000.0)
        leituras = iter([(999.0, {SURF: painel("agent")}), (1002.0, {SURF: painel(None)})])
        res = encerrar.conferir([SURF], depois_de=1000.0, ler=lambda: next(leituras),
                                dormir=r.dormir, relogio=r)
        self.assertEqual(res, {SURF: None})

    def test_painel_que_sumiu_do_autosave_nao_sobe(self):
        r = Relogio(1000.0)
        res = encerrar.conferir([SURF], depois_de=1000.0, ler=lambda: (1001.0, {}),
                                dormir=r.dormir, relogio=r)
        self.assertEqual(res, {SURF: None})

    def test_continua_armada_ate_o_teto(self):
        r = Relogio(1000.0)
        res = encerrar.conferir([SURF], depois_de=1000.0,
                                ler=lambda: (r.t, {SURF: painel("binding agent-hook")}),
                                dormir=r.dormir, relogio=r, teto=5)
        self.assertEqual(res, {SURF: "continua armada (binding agent-hook)"})

    def test_erro_de_leitura_e_reportado(self):
        r = Relogio(1000.0)

        def ler():
            raise RuntimeError("sem arquivo")

        res = encerrar.conferir([SURF], depois_de=1000.0, ler=ler, dormir=r.dormir, relogio=r,
                                teto=2)
        self.assertEqual(res, {SURF: "sem arquivo"})

    def test_sem_alvos_nao_le_nada(self):
        ler = mock.Mock()
        self.assertEqual(encerrar.conferir([], depois_de=0.0, ler=ler), {})
        ler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
