import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import painel


class DashboardTests(unittest.TestCase):
    def test_template_placeholders_and_no_plain_data(self):
        t = (ROOT / "template.html").read_text(encoding="utf-8")
        self.assertEqual(t.count("/*__DATA__*/null"), 1)
        self.assertEqual(t.count("/*__RAW__*/null"), 1)
        self.assertNotIn('"leads"', t)
        self.assertIn("Dashboard do Marketing", t)
        self.assertIn("MP_DASH", t)

    def test_origem(self):
        self.assertEqual(painel.origem("TRD | Fulano", ["Digital", "TRIADE"]), "Tríade")
        self.assertEqual(painel.origem("TRD-INT | Fulano", []), "TRD-INT")
        self.assertEqual(painel.origem("DIAG | Fulano", []), "Digital")
        self.assertEqual(painel.origem("Fulano", ["excliente"]), "Ex-cliente")
        self.assertEqual(painel.origem("Fulano", ["ind"]), "Indicação")
        self.assertEqual(painel.origem("Fulano", ["EVFO-07/10/26"]), "Eventos")
        self.assertEqual(painel.origem("Fulano", []), "Outros")

    def test_stage_history_converts_to_brt_and_keeps_only_alpha(self):
        deal = {"add_time": "2026-10-01T12:00:00Z", "stage_id": 346, "status": "lost", "lost_time": "2026-10-05T12:00:00Z"}
        log = [{"k": "stage_id", "o": "150", "n": "345", "t": "2026-10-02 12:00:00"},
               {"k": "stage_id", "o": "345", "n": "346", "t": "2026-10-03 12:00:00"}]
        h = painel.stage_history(deal, log)
        self.assertEqual(h, [[345, "2026-10-02T09:00:00", "2026-10-03T09:00:00"], [346, "2026-10-03T09:00:00", "2026-10-05T09:00:00"]])

    def test_tests_are_excluded(self):
        self.assertTrue(painel.TEST_RX.search("REGISTRO DE TESTE (QA formulario MP360) — pode apagar"))
        self.assertFalse(painel.TEST_RX.search("TRD | Celeste"))


class VeracidadeTests(unittest.TestCase):
    def test_real_entry_formats(self):
        n = lambda c: [{"add_time": "2026-10-01 10:00:00", "content": c}]
        self.assertEqual(painel.real_entry(n("Este lead entrou de verdade em 12/09/2026 às 11:05 (horário")),
                         ["2026-09-12T11:05:00", "Lead recuperado do Meta (nota)"])
        self.assertEqual(painel.real_entry(n("Enviado em: 05/10/2026 01:43"))[0], "2026-10-05T01:43:00")
        self.assertEqual(painel.real_entry(n("Entrada (Meta/form): 09/20/2026 2:07pm"))[0], "2026-09-20T14:07:00")
        self.assertEqual(painel.real_entry(n("Entrada no Meta: 2026-09-16T23:24:21+0000"))[0], "2026-09-16T20:24:21")
        self.assertIsNone(painel.real_entry(n("Planejamento")))

    def test_contact_time_retroactive_uses_due_date(self):
        a = {"done_at": "2024-10-21 13:18:44", "add": "2024-10-21 13:18:44", "due": "2024-07-09", "due_t": None}
        self.assertEqual(painel.contact_time(a), "2024-07-09T12:00:00")
        b = {"done_at": "2026-10-14 15:00:00", "add": "2026-10-10 10:00:00", "due": "2026-10-13", "due_t": None}
        self.assertEqual(painel.contact_time(b), "2026-10-14T12:00:00")

    def test_dedupe_same_cycle_merges_new_cycle_counts_again(self):
        mk = lambda i, s, e, k: {"id": i, "s": s, "e": e, "ec": e, "ef": "Criação no CRM", "b": 0, "pk": ["x"], "k": k}
        # duplicidade no mesmo ciclo (6 dias) -> um lead só, entrada = a mais antiga
        out = painel.dedupe([mk(1, "lost", "2026-07-21T10:00:00", [["2026-07-21T11:00:00", "call"]]),
                             mk(2, "open", "2026-07-27T10:00:00", [["2026-07-27T12:00:00", "call"]])])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 2)
        self.assertEqual(out[0]["e"], "2026-07-21T10:00:00")
        self.assertEqual(len(out[0]["k"]), 2)
        self.assertEqual(out[0]["dd"], [1])
        # nova conversão 31 dias depois -> ciclo novo, conta no próprio período e é marcada reincidente
        out = painel.dedupe([mk(1, "lost", "2026-09-12T10:00:00", []), mk(2, "open", "2026-10-13T10:00:00", [])])
        self.assertEqual(sorted(l["e"][:10] for l in out), ["2026-09-12", "2026-10-13"])
        self.assertEqual([l.get("re") for l in out if l["id"] == 2], ["2026-09-12"])

    def test_each_done_contact_counts(self):
        cache = {"users": {"1": "Consultor"}, "labels": {}, "stage_pipe": {}, "deals": [
            {"id": 9, "title": "TRD | X", "status": "lost", "stage_id": 345, "owner_id": 1, "label_ids": [],
             "add_time": "2026-10-06T12:00:00Z", "lost_time": "2026-10-07T12:00:00Z", "lost_reason": "Sem contato"}],
            "per_deal": {"9": {"pk": [], "log": [], "acts": [
                {"id": i, "type": "call", "done": i < 3, "done_at": "2026-10-06 18:56:00" if i < 3 else None,
                 "add": "2026-10-06 12:00:00", "due": "2026-10-06", "due_t": None} for i in range(1, 6)]}}}
        leads, _ = painel.model(cache)
        self.assertEqual(len(leads[0]["k"]), 2)  # A01 e A02 concluídas no mesmo minuto = 2 contatos; pendentes não contam

    def test_closed_before_moving_keeps_creation_as_entry(self):
        cache = {"users": {"1": "C"}, "labels": {}, "stage_pipe": {"10": "Outro funil"}, "deals": [
            {"id": 7, "title": "X", "status": "won", "stage_id": 350, "owner_id": 1, "label_ids": [],
             "add_time": "2025-01-31T18:47:00Z", "won_time": "2025-01-31T18:47:30Z"}],
            "per_deal": {"7": {"pk": [], "acts": [], "log": [{"k": "stage_id", "o": "10", "n": "350", "t": "2025-09-04 13:30:00"}]}}}
        l = painel.model(cache)[0][0]
        self.assertEqual(l["e"][:16], "2025-01-31T15:47")
        self.assertLessEqual(l["e"], l["w"])

    def test_moved_into_alpha_from_base(self):
        deal = {"add_time": "2024-10-21T14:00:00Z"}
        log = [{"k": "stage_id", "o": "351", "n": "345", "t": "2026-10-08 19:40:00"}]
        self.assertEqual(painel.moved_into_alpha(deal, log, {"351": "B2C Base de Relacionamento"}),
                         ("2026-10-08T16:40:00", "B2C Base de Relacionamento"))
        self.assertIsNone(painel.moved_into_alpha(deal, [], {}))


if __name__ == "__main__":
    unittest.main()
