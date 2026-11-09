"""Agente de verificação do Painel Operacional.

Roda a cada atualização (Actions, logo depois da coleta e antes de publicar) e confere, campo por campo, os números
que vão para a tela contra as fontes consultadas DE NOVO naquele momento:

  1. Pipedrive B2C — todos os negócios: status, etapa, consultor, datas, motivo de perda, pendências, testes,
     duplicidades; atividades (contatos, reuniões, agendamentos, faltas, pendências) de uma amostra que gira
     a cada hora (cobre a base inteira em ~1 dia útil) + os negócios mexidos desde a verificação anterior.
  2. Coerência interna — datas e jornada de cada lead.
  3. Cálculo da tela — o MESMO código JS do painel × o auditor independente em Python (audit.py), em todos os
     cenários, e as somas que precisam fechar entre si (leads = abertos + ganhos + perdidos etc.).
  4. Meta Ads — investimento, impressões e cliques por grupo em 4 períodos fechados.
  5. MP PRO — pedidos da Kiwify por mês/status (qtd, bruto, taxa, líquido) e campanhas Perpétuo por mês.
  6. Instagram — seguidores, seguindo, publicações, cobertura de dias, um dia ao acaso e os posts recentes.
  7. Aplicação Direta (funil 60) — cada negócio: status, etapa, datas.

Regras: só leitura nas fontes; o resultado detalhado vai CIFRADO dentro do painel (aba de verificação);
o log do Actions é público, então aqui só saem contagens. Diferença causada por algo que mudou na fonte DEPOIS
da leitura não é erro: vira "aviso" e é conferida de novo na próxima hora.
"""
import json, os, pathlib, random, re, subprocess, tempfile, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, date

import painel, audit

ROOT = pathlib.Path(__file__).resolve().parent
AMOSTRA = 20          # negócios com atividades relidas por hora (rodízio)
MEXIDOS = 15          # + negócios alterados desde a verificação anterior
MOVIDO = "Entrou no Funil Alpha vindo de"


class Bloco:
    def __init__(self, nome):
        self.nome, self.n, self.div, self.avisos, self.status = nome, 0, [], [], "ok"

    def ok(self, cond, txt, ids=()):
        self.n += 1
        if not cond:
            self.div.append({"txt": txt, "ids": list(ids)[:15]})
        return cond

    def aviso(self, txt, ids=()):
        self.avisos.append({"txt": txt, "ids": list(ids)[:15]})

    def out(self):
        st = self.status if self.status != "ok" else ("divergente" if self.div else "ok")
        return {"nome": self.nome, "checagens": self.n, "status": st, "divergencias": self.div, "avisos": self.avisos}


def _utc(s):
    return painel.parse_utc(s) if s else None


def _list(path, **q):
    out, cur = [], None
    while True:
        p = dict(q)
        if cur: p["cursor"] = cur
        r = painel.get(path, **p)
        out += r.get("data") or []
        cur = (r.get("additional_data") or {}).get("next_cursor")
        if not cur: return out


# ------------------------------------------------------------------ 1. Pipedrive B2C
def pipedrive(S, st, B, Ba):
    lido = _utc(S["meta"].get("pipedrive_em"))
    L = {l["id"]: l for l in S["leads"]}
    canon = {i: l for l in S["leads"] for i in (l.get("dd") or [])}
    deals = []
    for s in ("open", "lost", "won"):
        deals += [d for d in _list("/v2/deals", pipeline_id=45, status=s, limit=500,
                                   include_fields="undone_activities_count") if d.get("pipeline_id") == 45]
    users = {u["id"]: u["name"] for u in painel.get("/v1/users")["data"]}
    depois = lambda d: (_utc(d.get("update_time")) or lido) > lido
    TEST = painel.TEST_RX
    vistos = set()
    for d in deals:
        did, tit = d["id"], d.get("title") or ""
        vistos.add(did)
        if TEST.search(tit) or TEST.search(d.get("lost_reason") or ""):
            B.ok(did not in L and did not in canon, "registro de teste aparece no painel", [did]); continue
        if did in canon:
            c = canon[did]
            if d["status"] == "won" and c["s"] != "won" and not depois(d):
                B.ok(False, "negócio GANHO somado dentro de outro lead que não está como ganho", [did, c["id"]])
            else:
                B.ok(True, "")
            continue
        l = L.get(did)
        if not l:
            if depois(d): B.aviso("negócio criado/alterado depois da leitura (entra na próxima)", [did])
            else: B.ok(False, "negócio do Pipedrive ausente no painel", [did])
            continue
        own = painel.NOME_EXIBICAO.get(d.get("owner_id"), users.get(d.get("owner_id"), "Sem responsável"))
        own = "Sem consultor (Marketing)" if own == "Marketing" else own
        lr = ((d.get("lost_reason") or "").strip() or "Sem motivo informado") if d["status"] == "lost" else None
        campos = {"status": (l["s"], d["status"]), "etapa": (l["sg"], d["stage_id"]), "consultor": (l["c"], own),
                  "criação no CRM": ((l.get("ec") or "")[:16], (painel.brt(d.get("add_time")) or "")[:16]),
                  "data do ganho": ((l.get("w") or "")[:16], (painel.brt(d.get("won_time")) or "")[:16]),
                  "data da perda": ((l.get("l") or "")[:16], (painel.brt(d.get("lost_time")) or "")[:16]),
                  "motivo da perda": (l.get("lr") if l["s"] == "lost" else None, lr)}
        for k, (a, b) in campos.items():
            if a == b: B.ok(True, ""); continue
            if depois(d): B.aviso(f"{k} mudou no Pipedrive depois da leitura", [did])
            else: B.ok(False, f"{k} diferente do Pipedrive", [did])
        # pendências (bloco "em atraso"/"sem próxima atividade"): só abertos sem duplicidade
        if d["status"] == "open" and not l.get("dd") and "undone_activities_count" in d:
            a, b = len(l.get("pv") or []), d.get("undone_activities_count") or 0
            if a == b: B.ok(True, "")
            elif depois(d): B.aviso("atividades pendentes mudaram depois da leitura", [did])
            else: st.setdefault("_pend_dif", []).append(did)   # confirmado na amostra (pode ser tarefa sem data)
    for i in set(L) - vistos:
        B.ok(False, "lead do painel que não existe mais no Funil Alpha (apagado/movido)", [i])
    ok = [d for d in deals if not (TEST.search(d.get("title") or "") or TEST.search(d.get("lost_reason") or ""))]
    tarde = sum(1 for d in ok if depois(d))
    for nome, a, b in (("total de negócios", len(ok), len(L) + len(canon)),
                       ("abertos", sum(d["status"] == "open" for d in ok), sum(l["s"] == "open" for l in L.values()) + sum(1 for i in canon if next((d for d in ok if d["id"] == i), {}).get("status") == "open")),
                       ("ganhos", sum(d["status"] == "won" for d in ok), sum(l["s"] == "won" for l in L.values()) + sum(1 for i in canon if next((d for d in ok if d["id"] == i), {}).get("status") == "won"))):
        if a == b: B.ok(True, "")
        elif tarde: B.aviso(f"{nome}: Pipedrive {a} × painel {b} (houve alterações depois da leitura)")
        else: B.ok(False, f"{nome}: Pipedrive {a} × painel {b}")
    st["_deals"] = {d["id"]: d for d in deals}

    # ---- atividades: amostra rotativa + mexidos desde a última verificação
    ordem = sorted(L)
    cur = st.get("cursor", 0) % max(1, len(ordem))
    amostra = ordem[cur:cur + AMOSTRA] + ordem[:max(0, cur + AMOSTRA - len(ordem))]
    st["cursor"] = (cur + AMOSTRA) % max(1, len(ordem))
    ult = _utc(st.get("ultima")) or lido - timedelta(hours=1)
    mex = [i for i, d in st["_deals"].items() if i in L and (_utc(d.get("update_time")) or ult) > ult and i not in amostra]
    amostra += (mex + [i for i in st.pop("_pend_dif", []) if i not in amostra and i not in mex])[:MEXIDOS + 10]
    ids = {i: [i] + (L[i].get("dd") or []) for i in amostra}
    acts = {}
    for i, grupo in ids.items():
        for j in grupo:
            acts[j] = _list("/v2/activities", deal_id=j, limit=500)
    for i, grupo in ids.items():
        l = L[i]
        A = [painel.norm_activity(a) for j in grupo for a in acts[j]]
        A = [a for a in A if not a["deleted"]]
        tarde = any((_utc(a.get("update_time")) or lido) > lido for j in grupo for a in acts[j]) or \
            any(depois(st["_deals"][j]) for j in grupo if j in st["_deals"])
        mov = (l.get("ef") or "").startswith(MOVIDO)
        corte = l["e"] if mov else ""
        k = sorted([painel.contact_time(a), painel.tipo_efetivo(a)] for a in A
                   if a["done"] and a["type"] in painel.CONTACT_TYPES and painel.contact_time(a))
        k = [x for x in k if x[0] >= corte]
        if not mov and k and k[0][0] < l["e"] and not l.get("dd"):
            pass   # entrada = 1º contato (regra); nada a filtrar
        ra = sorted({painel.brt(a["add"]) for a in A if a.get("add") and painel.eh_reuniao(a)})
        ra = [x for x in ra if x >= corte]
        ns = sorted(x for x in (painel.contact_time(a) for a in A if a["done"] and a["type"] == "no_show") if x and x >= corte)
        pv = sorted(a["due"] for j in grupo if (st["_deals"].get(j) or {}).get("status", l["s"]) == "open"
                    for a in map(painel.norm_activity, acts[j]) if not a["deleted"] and not a["done"] and a.get("due")) if l["s"] == "open" else []
        sem_data = sum(1 for a in A if not a["done"] and not a.get("due"))
        comp = {"contatos (data e tipo)": ([list(x) for x in l["k"]], [list(x) for x in k]),
                "reuniões marcadas (agendamentos)": (sorted(set(l.get("ra") or [])), ra),
                "faltas (no-show)": (sorted(l.get("ns") or []), ns),
                "atividades pendentes": (sorted(x[0] for x in (l.get("pv") or [])), pv)}
        dup = bool(l.get("dd"))
        for nome, (a, b) in comp.items():
            if a == b: Ba.ok(True, ""); continue
            if tarde: Ba.aviso(f"{nome}: mudou no Pipedrive depois da leitura", [i]); continue
            if dup and nome != "atividades pendentes":
                Ba.aviso(f"{nome}: lead com negócio duplicado (regra de união) — conferir", [i]); continue
            Ba.ok(False, f"{nome} diferente do Pipedrive", [i])
        if sem_data: Ba.aviso("atividade pendente sem data (não entra no atraso)", [i])
    Ba.aviso(f"amostra desta hora: {len(amostra)} leads ({len(mex)} alterados desde a verificação anterior)")


# ------------------------------------------------------------------ 2. coerência interna
def coerencia(S, B):
    L, g = S["leads"], S["gerado_em"][:19]
    regras = [
        ("ganho antes da entrada", lambda l: l.get("w") and l["w"] < l["e"]),
        ("perda antes da entrada", lambda l: l.get("l") and l["l"] < l["e"]),
        ("contato antes da entrada", lambda l: l["k"] and l["k"][0][0] < l["e"][:19]),
        ("contato depois da atualização", lambda l: l["k"] and l["k"][-1][0] > g),
        ("aberto cuja jornada não termina na etapa atual", lambda l: l["s"] == "open" and (not l["h"] or l["h"][-1][0] != l["sg"] or l["h"][-1][2] is not None)),
        ("fechado com etapa ainda em aberto", lambda l: l["s"] != "open" and l["h"] and l["h"][-1][2] is None),
        ("etapa com saída antes da entrada", lambda l: any(s[2] and s[2] < s[1] for s in l["h"])),
        ("ganho sem data de ganho", lambda l: l["s"] == "won" and not l.get("w")),
        ("perdido sem data de perda", lambda l: l["s"] == "lost" and not l.get("l")),
        ("perdido sem motivo preenchido no campo", lambda l: l["s"] == "lost" and not l.get("lr")),
        ("fechado com atividade pendente na lista de atraso", lambda l: l["s"] != "open" and l.get("pv")),
        ("etapa fora do Funil Alpha", lambda l: l["sg"] not in painel.STAGE_IDS),
        ("lead repetido no painel", None),
    ]
    for nome, f in regras:
        if f is None:
            c = Counter(l["id"] for l in L); bad = [i for i, n in c.items() if n > 1]
        else:
            bad = [l["id"] for l in L if f(l)]
        B.ok(not bad, f"{nome}: {len(bad)}", bad)


# ------------------------------------------------------------------ 3. cálculo da tela (JS) × auditor (Python)
def calculo(S, B):
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "p.json").write_text(json.dumps(S, ensure_ascii=False), encoding="utf-8")
    audit.DATA = str(tmp / "p.json"); D = audit.load()
    cen = [{"cenario": n, "filtros": f} for n, f in audit.cenarios(D)]
    (tmp / "c.json").write_text(json.dumps(cen, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([painel.node_bin(), str(ROOT / "verificar_calc.js"), str(tmp / "p.json"), str(tmp / "c.json")],
                       capture_output=True, text=True, encoding="utf-8", timeout=180)
    if r.returncode: raise RuntimeError("cálculo JS falhou")
    F = json.loads(r.stdout)
    esp = {c["cenario"]: c for c in audit.expected_all(D)}
    for c in F:
        out = []
        audit.diff(esp[c["cenario"]]["resultado"], c["resultado"], "resultado", out)
        B.ok(not out, f"cenário {c['cenario']}: {len(out)} número(s) da tela diferente(s) do auditor independente")
    B.ok(len(F) == len(esp), "cenários faltando no cálculo da tela")

    def ck(cen, nome, a, b):
        ok = abs(a - b) < 1e-6 if isinstance(a, (int, float)) and isinstance(b, (int, float)) else a == b
        B.ok(ok, f"{cen}: {nome} não fecha ({a} × {b})")
    for c in F:
        n, r = c["cenario"], c["resultado"]
        if n.startswith("mppro") or "kpis" not in r: continue
        k = r["kpis"]
        ck(n, "leads = contatados + sem contato", k["leads"], k["contatados"] + k["sem_contato"])
        ck(n, "leads = abertos + ganhos + perdidos", k["leads"], k["abertos"] + k["ganhos"] + k["perdidos"])
        ck(n, "abertos = atraso + vence hoje + em dia + sem próxima", k["abertos"], k["leads_em_atraso"] + k["leads_vence_hoje"] + k["leads_em_dia"] + k["leads_sem_proxima"])
        ck(n, "% contatados", round(k["pct_contatados"] or 0, 6), round(k["contatados"] * 100 / k["leads"], 6) if k["leads"] else 0)
        ck(n, "conversão = ganhos ÷ leads", round(k["taxa_conversao"] or 0, 6), round(k["ganhos"] * 100 / k["leads"], 6) if k["leads"] else 0)
        ck(n, "atividades atrasadas ≥ leads em atraso", k["atividades_atrasadas"] >= k["leads_em_atraso"], True)
        C = r["consultores"]
        for f in ("leads", "contatados", "agendados", "reunioes", "ganhos", "perdidos", "abertos", "leads_em_atraso", "atividades_atrasadas", "leads_sem_proxima"):
            ck(n, f"soma dos consultores = total ({f})", sum(x[f] for x in C), k[f])
        ck(n, "soma das etapas atuais = abertos", sum(e["atuais"] for e in r["etapas"]), k["abertos"])
        for x in C:   # mapa da carteira por consultor
            ck(n, f"{x['consultor']}: leads por etapa somam os abertos", sum((x.get("abertos_por_etapa") or {}).values()), x["abertos"])
            ck(n, f"{x['consultor']}: atraso + vence hoje + em dia + sem próxima = abertos",
               x["leads_em_atraso"] + x["leads_vence_hoje"] + x["leads_em_dia"] + x["leads_sem_proxima"], x["abertos"])
        P = r["perdidos"]
        ck(n, "perdidos (aba Perdidos) = perdidos (Macro)", P["total"], k["perdidos"])
        ck(n, "soma dos motivos = perdidos", sum(P["motivos"].values()), P["total"])
        for g in (r.get("performance") or {}).get("grupos") or []:
            if g.get("cliques") and g.get("impressoes"): ck(n, f"CTR {g['id']}", round(g["ctr"], 4), round(g["cliques"] * 100 / g["impressoes"], 4))
            if g.get("cliques"): ck(n, f"CPC {g['id']}", round(g["cpc"], 4), round(g["investimento"] / g["cliques"], 4))
            if g.get("leads") and g.get("investimento"): ck(n, f"CPL {g['id']}", round(g["custo_lead"], 4), round(g["investimento"] / g["leads"], 4))
            ck(n, f"contatados ≤ leads ({g['id']})", g["contatados"] <= g["leads"], True)


# ------------------------------------------------------------------ 4. Meta Ads
def meta_ads(S, B):
    M = S["midia"]; hoje = date.fromisoformat(S["gerado_em"][:10]); ontem = hoje - timedelta(1)
    ini_mes = hoje.replace(day=1); ant_fim = ini_mes - timedelta(1)
    per = [("ontem", ontem, ontem), ("últimos 7 dias fechados", hoje - timedelta(7), ontem),
           ("mês anterior", ant_fim.replace(day=1), ant_fim)]
    if ontem >= ini_mes: per.append(("mês atual até ontem", ini_mes, ontem))
    tok = painel.meta_token()
    for nome, a, b in per:
        rows = painel.meta_get(f"{painel.META_ACCOUNT}/insights", tok, level="campaign", limit=500,
                               time_range=json.dumps({"since": a.isoformat(), "until": b.isoformat()}),
                               fields="campaign_name,spend,impressions,inline_link_clicks")
        meta, dash = defaultdict(lambda: [0, 0, 0]), defaultdict(lambda: [0, 0, 0])
        for r in rows:
            g = painel.grupo_midia(r["campaign_name"])
            if g:
                x = meta[g]; x[0] += round(float(r["spend"]) * 100); x[1] += int(r.get("impressions") or 0); x[2] += int(r.get("inline_link_clicks") or 0)
        for day, ci, c, i, cl in M["dias"]:
            if a.isoformat() <= day <= b.isoformat():
                x = dash[M["camps"][ci]["grupo"]]; x[0] += c; x[1] += i; x[2] += cl
        for g in sorted(set(meta) | set(dash)):
            for j, campo in enumerate(("investimento", "impressões", "cliques")):
                B.ok(meta[g][j] == dash[g][j], f"{nome} · {g} · {campo}: Meta {meta[g][j]} × painel {dash[g][j]}")


# ------------------------------------------------------------------ 5. MP PRO
def mppro(S, B):
    M = S["mppro"]; corte = S["gerado_em"][:19]
    if M.get("erro_kiwify"): B.aviso("Kiwify indisponível nesta atualização"); return
    raw = [x for x in painel.kiwify_sales() if x["status"] in ("paid", "refunded", "refused")]
    api, dash = defaultdict(lambda: [0, 0, 0, 0]), defaultdict(lambda: [0, 0, 0, 0])
    for x in raw:
        ts = painel._brt(x["approved_date"] if x["status"] != "refused" else x["created_at"])
        if ts > corte: continue
        pg = x.get("payment") or {}
        a = api[f"{ts[:7]} {x['status']}"]; a[0] += 1; a[1] += painel._bruto(x); a[2] += pg.get("fee") or 0; a[3] += x.get("net_amount") or 0
    for p in M["pedidos"]:
        if p[3] not in ("paid", "refunded", "refused"): continue
        a = dash[f"{p[0][:7]} {p[3]}"]; a[0] += 1; a[1] += p[5]; a[2] += p[6]; a[3] += p[7]
    for k in sorted(set(api) | set(dash)):
        for j, campo in enumerate(("pedidos", "bruto", "taxa", "líquido")):
            B.ok(api[k][j] == dash[k][j], f"Kiwify {k} · {campo}: API {api[k][j]} × painel {dash[k][j]}")
    em = {(x["customer"].get("email") or "").lower() for x in raw if x["status"] in ("paid", "refunded") and painel._brt(x["approved_date"]) <= corte}
    cp = {p[1] for p in M["pedidos"] if p[3] in ("paid", "refunded")}
    B.ok(len(em) == len(cp), f"compradores distintos: Kiwify {len(em)} × painel {len(cp)}")
    tok = painel.meta_token()
    rows = painel.meta_get(f"{painel.META_ACCOUNT}/insights", tok, level="campaign", time_increment="monthly", limit=500,
                           time_range=json.dumps({"since": painel.META_DESDE, "until": corte[:10]}),
                           fields="campaign_name,spend,impressions,inline_link_clicks,actions")
    meta, dm = defaultdict(lambda: [0] * 5), defaultdict(lambda: [0] * 5)
    for r in rows:
        if not painel.eh_perpetuo(r["campaign_name"]): continue
        act = {a["action_type"]: int(float(a["value"])) for a in r.get("actions") or []}
        m = meta[r["date_start"][:7]]
        for i, v in enumerate((round(float(r["spend"]) * 100), int(r.get("impressions") or 0), int(r.get("inline_link_clicks") or 0),
                               act.get("landing_page_view", 0), act.get("initiate_checkout", 0))): m[i] += v
    for r in M["midia"]["dias"]:
        for i in range(5): dm[r[0][:7]][i] += r[2 + i]
    for k in sorted(set(meta) | set(dm)):
        if k == corte[:7]: continue    # mês corrente: a Meta segue somando depois da leitura
        for j, campo in enumerate(("investimento", "impressões", "cliques", "visitas", "checkouts")):
            B.ok(meta[k][j] == dm[k][j], f"Perpétuo {k} · {campo}: Meta {meta[k][j]} × painel {dm[k][j]}")


# ------------------------------------------------------------------ 6. Instagram
def instagram(S, B):
    IG = S["ig"]
    if IG.get("erro"): B.aviso("Instagram indisponível nesta atualização"); return
    tok = painel.meta_token(); uso = [0]; G = lambda p, **q: painel.ig_get(p, tok, uso, **q)
    lido = (IG.get("atualizado_em") or S["gerado_em"])[:19]
    hoje = date.fromisoformat(S["gerado_em"][:10]); rnd = random.Random(S["gerado_em"])
    for c in IG["contas"]:
        cid, u = c["id"], "@" + c["user"]
        pf = G(cid, fields="followers_count,follows_count,media_count")
        B.ok(abs(c["seguidores"] - pf["followers_count"]) <= 3, f"{u} seguidores: painel {c['seguidores']} × API {pf['followers_count']}")
        B.ok(abs(c["seguindo"] - pf["follows_count"]) <= 3, f"{u} seguindo: painel {c['seguindo']} × API {pf['follows_count']}")
        rec = G(f"{cid}/media", fields="id,timestamp,permalink", limit=25).get("data") or []
        novos = sum(1 for m in rec if painel._brt(m["timestamp"]) > lido)
        if c["midias"] + novos == pf["media_count"]: B.ok(True, "")
        else: B.ok(False, f"{u} publicações: painel {c['midias']} × API {pf['media_count']}")
        perm = {p[2] for p in c["posts"]}
        falt = [m["timestamp"][:10] for m in rec if painel._brt(m["timestamp"]) <= lido and m["permalink"] not in perm]
        B.ok(not falt, f"{u}: {len(falt)} post(s) recente(s) faltando no painel")
        ds = {x[0] for x in c["dias"]}
        ini = date.fromisoformat(painel.IG_DESDE)
        esper = {(ini + timedelta(i)).isoformat() for i in range((hoje - ini).days - 1)}
        falta = sorted(esper - ds)
        B.ok(not falta, f"{u}: {len(falta)} dia(s) sem dados no histórico")
        if c["seg"]: B.ok(c["seg"][-1][1] == c["seguidores"], f"{u}: série de seguidores não termina no total atual")
        byd = {x[0]: x for x in c["dias"]}
        cand = [d for d in byd if d < (hoje - timedelta(3)).isoformat()]
        for k in rnd.sample(cand, min(1, len(cand))) + [(hoje - timedelta(3)).isoformat()]:
            if k not in byd: continue
            fim = painel._meia_noite((date.fromisoformat(k) + timedelta(1)).isoformat())
            r = G(f"{cid}/insights", metric=",".join(painel.IG_DIA), period="day", metric_type="total_value", since=painel._meia_noite(k), until=fim)
            api = {x["name"]: (x.get("total_value") or {}).get("value") for x in r.get("data") or []}
            pan = dict(zip(painel.IG_DIA, byd[k][1:12]))
            for m in painel.IG_DIA:
                a, b = pan.get(m) or 0, api.get(m) or 0
                B.ok(abs(a - b) <= max(2, 0.02 * b), f"{u} dia {k} · {m}: painel {a} × API {b}")


# ------------------------------------------------------------------ 7. Aplicação Direta
def aplicacao(S, B):
    A = S.get("aplicacao") or {}
    if A.get("erro"): B.aviso("Aplicação Direta sem leitura nesta atualização"); return
    lido = _utc(A.get("atualizado_em") + "-03:00") if A.get("atualizado_em") else None
    P = {l["id"]: l for l in A["leads"]}
    deals = []
    for s in ("open", "lost", "won"):
        deals += [d for d in _list("/v2/deals", pipeline_id=painel.APL_PIPELINE, status=s, limit=500) if d.get("pipeline_id") == painel.APL_PIPELINE]
    vistos = set()
    for d in deals:
        if painel.TEST_RX.search(d.get("title") or "") or painel.TEST_RX.search(d.get("lost_reason") or ""): continue
        vistos.add(d["id"]); l = P.get(d["id"])
        tarde = lido and (_utc(d.get("update_time")) or lido) > lido
        if not l:
            (B.aviso if tarde else lambda t, i: B.ok(False, t, i))("candidatura do Pipedrive ausente no painel", [d["id"]]); continue
        comp = {"status": (l["s"], d["status"]), "etapa": (l["sg"], d["stage_id"]), "entrada": (l["e"], painel.brt(d.get("add_time"))),
                "data do ganho": (l.get("wt"), painel.brt(d.get("won_time")) if d["status"] == "won" else None),
                "data da perda": (l.get("lt"), painel.brt(d.get("lost_time")) if d["status"] == "lost" else None)}
        for k, (a, b) in comp.items():
            if a == b: B.ok(True, "")
            elif tarde: B.aviso(f"{k} mudou depois da leitura", [d["id"]])
            else: B.ok(False, f"{k} diferente do Pipedrive", [d["id"]])
    for i in set(P) - vistos: B.ok(False, "candidatura do painel que não existe mais no funil", [i])


# ------------------------------------------------------------------ orquestração
def verificar(S, estado=None, pipedrive_lido=True, prazo_s=300):
    """S = payload que vai para o painel. estado = memória entre execuções (cursor do rodízio, divergências
    anteriores). Devolve (resultado, novo_estado). Nunca levanta exceção: falha de fonte vira bloco 'não verificado'."""
    t0 = time.time(); st = dict(estado or {})
    blocos = []

    def roda(nomes, fn, *extra):
        Bs = [Bloco(n) for n in nomes]
        if time.time() - t0 > prazo_s:
            for b in Bs: b.status = "não verificado"; b.aviso("tempo esgotado nesta hora; volta na próxima")
        else:
            try: fn(S, *extra, *Bs)
            except Exception as e:
                for b in Bs: b.status = "não verificado"; b.aviso(f"fonte indisponível agora ({type(e).__name__})")
        blocos.extend(b.out() for b in Bs)

    if pipedrive_lido:
        roda(["Pipedrive B2C · negócios (todos)", "Pipedrive B2C · atividades (amostra da hora)"], pipedrive, st)
    else:
        for n in ("Pipedrive B2C · negócios (todos)", "Pipedrive B2C · atividades (amostra da hora)"):
            blocos.append({"nome": n, "checagens": 0, "status": "pulado", "divergencias": [],
                           "avisos": [{"txt": "Pipedrive não foi relido nesta atualização (fora da janela ou leitura recente)", "ids": []}]})
    roda(["Coerência das datas e da jornada"], coerencia)
    roda(["Cálculo da tela × auditor independente"], calculo)
    roda(["Meta Ads (Performance)"], meta_ads)
    roda(["MP PRO (Kiwify + Meta)"], mppro)
    roda(["Instagram"], instagram)
    if pipedrive_lido: roda(["Aplicação Direta (funil 60)"], aplicacao)
    st.pop("_deals", None); st.pop("_pend_dif", None)
    chaves = sorted({f"{b['nome']}|{d['txt']}|{d['ids']}" for b in blocos for d in b["divergencias"]})
    antes = set(st.get("chaves") or [])
    novas = [c for c in chaves if c not in antes]
    st["chaves"] = chaves; st["ultima"] = S["meta"].get("pipedrive_em") if pipedrive_lido else st.get("ultima")
    res = {"em": datetime.now(painel.BRT).strftime("%Y-%m-%dT%H:%M:%S"), "duracao_s": round(time.time() - t0),
           "checagens": sum(b["checagens"] for b in blocos), "divergencias": sum(len(b["divergencias"]) for b in blocos),
           "novas": len(novas), "blocos": blocos}
    return res, st


def resumo_publico(r):
    """Uma linha para o log PÚBLICO do Actions: só contagens."""
    st = Counter(b["status"] for b in r["blocos"])
    return (f"verificação: {r['checagens']} checagens | {r['divergencias']} divergência(s), {r['novas']} nova(s) | "
            f"blocos: " + ", ".join(f"{v} {k}" for k, v in st.items()) + f" | {r['duracao_s']}s")


if __name__ == "__main__":      # uso local: python verificar.py data/_live_payload.json [--sem-pipedrive]
    import sys
    S = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    r, _ = verificar(S, {}, pipedrive_lido="--sem-pipedrive" not in sys.argv)
    print(resumo_publico(r))
    if "--detalhe" in sys.argv:
        for b in r["blocos"]:
            print(f"\n[{b['status']}] {b['nome']} — {b['checagens']} checagens")
            for d in b["divergencias"][:12]: print("   DIF", d["txt"], d["ids"])
            for a in b["avisos"][:8]: print("   aviso", a["txt"], a["ids"])
