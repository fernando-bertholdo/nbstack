"""O main() do hibernate.py contra um cmux falso: processos, autosave e rpc trocados."""
import contextlib
import io
import json
import pathlib
import signal
import sys
import tempfile
import unittest
from unittest import mock

RAIZ = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hibernate  # noqa: E402
from cmux_falso import CmuxFalso, sessao, uuid  # noqa: E402

CTRL = uuid(1)
PADRAO = [{"n": 1, "pid": "100"},                              # controle
          {"n": 2, "pid": "200"},                              # viva e ociosa
          {"n": 3, "pid": "300"},                              # viva e ociosa
          {"n": 4},                                            # armada sem processo
          {"n": 5, "armada": False}]                           # ja dormia


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.estado_dir = pathlib.Path(self.tmp.name) / "estado"

    def rodar(self, *argv, abas=PADRAO, protegidos=(), controle=CTRL, **cmux):
        self.cmux = CmuxFalso(self.tmp.name, abas, **cmux)
        saida, erro = io.StringIO(), io.StringIO()
        with self.cmux.ligado(hibernate, protegidos=protegidos), \
                mock.patch.dict("os.environ", {"CMUX_SURFACE_ID": controle}), \
                mock.patch.object(sys, "argv", ["hibernate.py", "--estado",
                                                str(self.estado_dir), *argv]), \
                contextlib.redirect_stdout(saida), contextlib.redirect_stderr(erro):
            try:
                rc = hibernate.main()
            except SystemExit as e:
                rc = e.code
        self.saida, self.erro = saida.getvalue(), erro.getvalue()
        return rc

    def snapshots(self):
        if not self.estado_dir.exists():
            return []
        return sorted(d.name for d in self.estado_dir.iterdir())

    def snapshot(self):
        nome = [n for n in self.snapshots() if n.endswith("Z")][-1]
        return json.loads((self.estado_dir / nome / "snapshot.json").read_text())

    def plano(self):
        return {p["surface"]: p for p in self.snapshot()["plano"]}


class TestDryRun(Base):
    def test_nao_encerra_nem_limpa_nem_entra_na_retencao(self):
        rc = self.rodar()
        self.assertEqual(rc, 0)
        self.assertEqual((self.cmux.sinais, self.cmux.rpcs), ([], []))
        self.assertEqual(self.snapshots(), ["ultimo-dry-run"])
        self.assertIn("Encerraria 2, limparia 1, pularia 0; 0 ja dormem", self.saida)
        self.assertNotIn("Cmd+Q", self.saida)

    def test_dry_run_nao_apaga_snapshot_de_apply(self):
        self.estado_dir.mkdir()
        for i in range(5):
            d = self.estado_dir / ("2026-10-0%dT10-00-00Z" % (i + 1))
            d.mkdir()
            (d / "snapshot.json").write_text('{"tipo": "apply", "janelas": []}')
        self.rodar()
        self.assertEqual(len([n for n in self.snapshots() if n.endswith("Z")]), 5)

    def test_inventario_inclui_a_armada_sem_processo(self):
        self.rodar()
        inv = (self.estado_dir / "ultimo-dry-run" / "INVENTARIO.md").read_text()
        self.assertIn(sessao(4), inv)


class TestApply(Base):
    def test_caminho_feliz_encerra_limpa_confere_e_libera_o_cmd_q(self):
        rc = self.rodar("--apply")
        self.assertEqual(rc, 0, self.erro)
        self.assertEqual(sorted(self.cmux.sinais), [("200", signal.SIGTERM),
                                                    ("300", signal.SIGTERM)])
        self.assertEqual([r["surface_id"] for r in self.cmux.rpcs], [uuid(4)])
        self.assertIn("Pode dar Cmd+Q", self.saida)
        self.assertIn("espere uns 10 s antes de reabrir", self.saida)
        self.assertIn("Esta aba continua armada", self.saida)
        p = self.plano()
        self.assertEqual(self.snapshot()["tipo"], "apply")
        self.assertEqual(p[uuid(2)]["resultado"]["metodo"], "sigterm")
        self.assertEqual(p[uuid(4)]["resultado"]["metodo"], "rpc")
        self.assertTrue(all(p[uuid(n)]["resultado"]["verificado"] for n in (2, 3, 4)))
        self.assertNotIn(uuid(5), p)  # sem sessao em fonte nenhuma
        self.assertEqual(p[CTRL]["acao"], "controle")

    def test_nunca_sinaliza_a_aba_de_controle(self):
        self.rodar("--apply")
        self.assertNotIn("100", [pid for pid, _ in self.cmux.sinais])

    def test_ok_que_nao_pegou_reprova_e_nao_libera_o_cmd_q(self):
        """31/10/2026: o cmux respondeu OK, o autosave continuou armado e 11 abas subiram."""
        rc = self.rodar("--apply", session_end_limpa=False, rpc_limpa=False)
        self.assertEqual(rc, 1)
        self.assertNotIn("Pode dar Cmd+Q", self.saida)
        self.assertIn("Nao de Cmd+Q ainda", self.erro)
        p = self.plano()
        for n in (2, 3, 4):
            self.assertFalse(p[uuid(n)]["resultado"]["verificado"], n)
        self.assertEqual(p[uuid(2)]["resultado"]["metodo"], "sigterm+rpc")
        self.assertEqual(self.saida.count("FALHOU"), 3)

    def test_session_end_que_nao_limpa_cai_no_rpc_e_passa(self):
        rc = self.rodar("--apply", session_end_limpa=False)
        self.assertEqual(rc, 0, self.erro)
        self.assertEqual(self.plano()[uuid(2)]["resultado"]["metodo"], "sigterm+rpc")
        self.assertIn("Pode dar Cmd+Q", self.saida)

    def test_aba_ocupada_e_pulada_e_segura_o_cmd_q(self):
        abas = PADRAO[:2] + [{"n": 3, "pid": "300", "idade_transcript": 5}]
        rc = self.rodar("--apply", abas=abas)
        self.assertEqual(rc, 1)
        self.assertEqual(self.cmux.sinais, [("200", signal.SIGTERM)])
        self.assertIn("ocupada: transcript escrito ha", self.plano()[uuid(3)]["motivo"])
        self.assertNotIn("Pode dar Cmd+Q", self.saida)

    def test_processo_que_nao_sai_falha_sem_sigkill(self):
        rc = self.rodar("--apply", nao_sai={"200"})
        self.assertEqual(rc, 1)
        self.assertEqual([s for p, s in self.cmux.sinais if p == "200"], [signal.SIGTERM])
        self.assertIn("SIGKILL", self.plano()[uuid(2)]["resultado"]["erro"])

    def test_retencao_conta_so_os_apply(self):
        self.rodar()
        self.rodar("--apply")
        self.assertEqual(len(self.snapshots()), 2)
        self.assertIn("ultimo-dry-run", self.snapshots())

    def test_segundo_apply_mantem_quem_ja_dormia_no_inventario(self):
        """F7: hibernar duas vezes seguidas apagava do inventario quem ja dormia."""
        self.rodar("--apply")
        primeiro = self.snapshot()
        abas = [{"n": 1, "pid": "100"}] + [{"n": n, "armada": False} for n in (2, 3, 4, 5)]
        self.cmux = None
        with mock.patch("lib.snapshot.agora_utc", return_value="2099-01-01T00:00:00Z"):
            self.rodar("--apply", abas=abas)
        segundo = self.snapshot()
        self.assertNotEqual(primeiro["gerado_em"], segundo["gerado_em"])
        sessoes = {p["sessao"] for p in segundo["plano"]}
        self.assertTrue({sessao(n) for n in (2, 3)} <= sessoes, sessoes)


class TestErro(Base):
    def test_erro_no_meio_do_apply_grava_o_snapshot_e_nao_libera(self):
        """O que ja foi encerrado fica registrado mesmo que o resto quebre."""
        boom = mock.Mock(side_effect=ValueError("quebrou"))
        with mock.patch.object(hibernate, "executar", boom):
            rc = self.rodar("--apply")
        self.assertEqual(rc, 1)
        self.assertNotIn("Pode dar Cmd+Q", self.saida)
        self.assertIn("quebrou", self.erro)
        p = self.plano()
        self.assertFalse(p[uuid(2)]["resultado"]["verificado"])
        self.assertIn("quebrou", p[uuid(2)]["resultado"]["erro"])


class TestEscopo(Base):
    def test_all_inclui_o_controle(self):
        rc = self.rodar("--apply", "--all")
        self.assertEqual(rc, 0, self.erro)
        self.assertIn(("100", signal.SIGTERM), self.cmux.sinais)
        self.assertNotIn("Esta aba continua armada", self.saida)

    def test_all_pula_a_sessao_que_roda_o_comando(self):
        rc = self.rodar("--apply", "--all", protegidos={"100"})
        self.assertEqual(rc, 1)
        self.assertNotIn("100", [p for p, _ in self.cmux.sinais])
        self.assertIn("/exit", self.plano()[CTRL]["motivo"])

    def test_surface_so_a_aba_pedida(self):
        rc = self.rodar("--apply", "--surface", "surface:3")
        self.assertEqual(rc, 0, self.erro)
        self.assertEqual(self.cmux.sinais, [("300", signal.SIGTERM)])
        self.assertEqual(self.cmux.rpcs, [])
        self.assertEqual(list(self.plano()), [uuid(3)])
        self.assertEqual(self.snapshot()["escopo"], "surface")

    def test_surface_na_propria_aba_de_controle(self):
        """O revezamento hiberna a aba velha pelo CMUX_SURFACE_ID dela."""
        rc = self.rodar("--apply", "--surface", CTRL.lower())
        self.assertEqual(rc, 0, self.erro)
        self.assertEqual(self.cmux.sinais, [("100", signal.SIGTERM)])
        self.assertIsNone(self.snapshot()["aba_de_controle"])

    def test_surface_numa_aba_armada_sem_processo(self):
        rc = self.rodar("--apply", "--surface", "surface:4")
        self.assertEqual(rc, 0, self.erro)
        self.assertEqual([r["surface_id"] for r in self.cmux.rpcs], [uuid(4)])

    def test_surface_vazio_sai_2_sem_snapshot(self):
        rc = self.rodar("--surface", "", "--apply")
        self.assertEqual(rc, 2)
        self.assertIn("nenhuma", self.erro)
        self.assertEqual((self.snapshots(), self.cmux.sinais), ([], []))

    def test_surface_inexistente_ou_sem_sessao_sai_2(self):
        self.assertEqual(self.rodar("--surface", "surface:99"), 2)
        self.assertEqual(self.snapshots(), [])
        self.assertEqual(self.rodar("--surface", "surface:5"), 2)
        self.assertIn("nao tem sessao", self.erro)

    def test_all_com_surface_e_recusado(self):
        self.assertEqual(self.rodar("--all", "--surface", "surface:2"), 2)

    def test_surface_ocupada_no_dry_run_sai_1(self):
        abas = PADRAO[:1] + [{"n": 2, "pid": "200", "idade_transcript": 1}]
        rc = self.rodar("--surface", "surface:2", abas=abas)
        self.assertEqual(rc, 1)
        self.assertIn("seria pulada", self.erro)

    def test_autosave_ilegivel_sai_2(self):
        self.cmux = CmuxFalso(self.tmp.name, PADRAO)
        self.cmux.autosave.write_text("{")
        saida, erro = io.StringIO(), io.StringIO()
        with self.cmux.ligado(hibernate), mock.patch.object(sys, "argv", ["hibernate.py"]), \
                contextlib.redirect_stdout(saida), contextlib.redirect_stderr(erro):
            self.assertEqual(hibernate.main(), 2)
        self.assertIn("autosave", erro.getvalue())


if __name__ == "__main__":
    unittest.main()
