#!/usr/bin/env python
"""Auditoria independente das métricas do Dashboard do Marketing (SPEC v2, 'Função de contrato').

Implementação de referência em Python (stdlib), sem depender do front-end.

Uso:
  python v2/audit.py expected                 -> grava data/audit_expected.json (+ resumo + checagem de sanidade)
  python v2/audit.py compare <frontend.json>  -> compara resultados do front-end com o esperado (exit 1 se divergir)
  python v2/audit.py sanity                   -> só a checagem de sanidade dos dados

O arquivo do front-end tem o mesmo formato: lista de {cenario, filtros, resultado}, onde
resultado = MP_DASH.compute(D.leads, filtros, D.gerado_em).
"""
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = _HERE if os.path.exists(os.path.join(_HERE, "encrypt.js")) else os.path.dirname(_HERE)
DATA = os.path.join(BASE, "data", "dashboard.json")
OUT = os.path.join(BASE, "data", "audit_expected.json")

AGENDADO_STAGES = {348, 349, 350}
REUNIAO_TIPOS = {"meeting", "qualificacao"}
TOL = 1e-6


# ----------------------------------------------------------------------------- utilidades
def dt(s):
    """String BRT 'YYYY-MM-DDTHH:MM:SS' (ignora sufixo de fuso) -> datetime ingênuo."""
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def dias(a, b):
    """dias(a - b), fracionário."""
    return (a - b).total_seconds() / 86400.0


def media(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def mediana(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    m = n // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2.0


def pct(a, b):
    return a / b * 100.0 if b else None


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn").lower()


# ----------------------------------------------------------------------------- métricas por lead
def lead_metrics(l, agora):
    e = dt(l["e"])
    k = l["k"] or []
    h = l["h"] or []
    m = {"e": e, "contatado": len(k) > 0, "n_contatos": len(k)}
    m["fds"] = e.weekday() >= 5
    m["dias_1o_contato"] = max(0.0, dias(dt(k[0][0]), e)) if k else None

    # agendado
    segs_ag = [s for s in h if s[0] in AGENDADO_STAGES]
    m["agendado"] = bool(segs_ag) or l["s"] == "won"
    if segs_ag:
        ini = min(dt(s[1]) for s in segs_ag)
        m["dias_ate_agendamento"] = max(0.0, dias(ini, e))
    elif l["s"] == "won" and l["w"]:
        m["dias_ate_agendamento"] = max(0.0, dias(dt(l["w"]), e))
    else:
        m["dias_ate_agendamento"] = None

    # reunião (contato meeting/qualificacao) — SPEC não manda limitar a 0
    reun = [dt(x[0]) for x in k if x[1] in REUNIAO_TIPOS]
    m["reuniao"] = bool(reun)
    m["dias_ate_reuniao"] = dias(min(reun), e) if reun else None

    m["dias_ate_perda"] = dias(dt(l["l"]), e) if l["s"] == "lost" and l["l"] else None
    m["dias_ate_ganho"] = dias(dt(l["w"]), e) if l["s"] == "won" and l["w"] else None
    if l["s"] == "lost" and l["l"]:
        ll = dt(l["l"])
        m["contatos_ate_perda"] = sum(1 for x in k if dt(x[0]) <= ll)
    else:
        m["contatos_ate_perda"] = None

    if l["s"] == "open":
        ref = max(dt(x[0]) for x in k) if k else e
        m["dias_sem_contato"] = dias(agora, ref)
        m["parado_7d"] = m["dias_sem_contato"] > 7
    else:
        m["dias_sem_contato"] = None
        m["parado_7d"] = False
    return m


# ----------------------------------------------------------------------------- filtro
CAMPANHAS = {"trd": r"^\s*TRD\s*\|", "diag": r"^\s*DIAG\s*\|", "rcon": r"^\s*RCON\b"}  # prefixo do título


def filtra(leads, f):
    de, ate = f.get("de") or "0000-00-00", f.get("ate") or "9999-99-99"
    cons = set(f.get("consultores") or [])
    oris = set(f.get("origens") or [])
    sts = set(f.get("status") or [])
    etps = set(f.get("etapas") or [])
    etqs = set(f.get("etiquetas") or [])
    busca = norm(f.get("busca") or "")
    out = []
    base = f.get("incluir_base") is not False   # padrão: funil inteiro
    camp = CAMPANHAS.get(f.get("campanha") or ("trd" if f.get("so_trd") else ""))
    for l in leads:
        if l.get("b") and not base:
            continue
        if camp and not re.match(camp, l.get("n") or "", re.I):
            continue
        d = l["e"][:10]
        if not (de <= d <= ate):
            continue
        if cons and l["c"] not in cons:
            continue
        if oris and l["o"] not in oris:
            continue
        if sts and l["s"] not in sts:
            continue
        if etps and l["sg"] not in etps:
            continue
        if etqs and not (set(l["lb"] or []) & etqs):
            continue
        if busca and busca not in norm(l["n"]):
            continue
        out.append(l)
    return out


# ----------------------------------------------------------------------------- performance (mídia × Pipedrive)
MIDIA = None   # D["midia"]; definido por load()/expected_all()/compare() (ou por quem importa o módulo)
PAGAS = {"trd": r"^\s*TRD\s*\|", "diag": r"^\s*DIAG\s*\|", "isca": r"^\s*\[ISCA", "outros": None}


def performance(leads, f, agora, midia):
    """Investimento (Meta, B2C, centavos) ÷ resultados no Pipedrive dos leads que ENTRARAM no período.
    Só período e campanha valem; leads da Meta são ignorados."""
    midia = midia or {"camps": [], "dias": []}
    camp = f.get("campanha") or ("trd" if f.get("so_trd") else "")
    sel = ([camp] if camp in PAGAS else []) if camp else list(PAGAS)
    de, ate = f.get("de") or "", f.get("ate") or ""
    def z(i): return {"id": i, "investimento_centavos": 0, "impressoes": 0, "cliques": 0, "leads": 0,
                      "contatados": 0, "agendados": 0, "reunioes": 0, "ganhos": 0}
    G = {i: z(i) for i in PAGAS}
    for day, ci, cent, imp, cl in midia["dias"]:
        if (de and day < de) or (ate and day > ate): continue
        g = G.get(midia["camps"][ci]["grupo"])
        if g is None: continue
        g["investimento_centavos"] += cent; g["impressoes"] += imp; g["cliques"] += cl
    for l in leads:
        day = (l.get("e") or "")[:10]
        if not day or (de and day < de) or (ate and day > ate): continue
        gid = next((k for k in ("trd", "diag", "isca") if re.match(PAGAS[k], l.get("n") or "", re.I)), None)
        if not gid: continue
        m = lead_metrics(l, agora); g = G[gid]
        g["leads"] += 1; g["contatados"] += m["contatado"]; g["agendados"] += m["agendado"]
        g["reunioes"] += m["reuniao"]; g["ganhos"] += l["s"] == "won"
    def fin(g):
        inv = g["investimento_centavos"] / 100
        by = lambda n: inv / n if n else None
        g.update(investimento=inv, custo_lead=by(g["leads"]), custo_contatado=by(g["contatados"]),
                 custo_agendamento=by(g["agendados"]), custo_reuniao=by(g["reunioes"]), custo_ganho=by(g["ganhos"]),
                 ctr=g["cliques"] / g["impressoes"] * 100 if g["impressoes"] else None, cpc=by(g["cliques"]),
                 cpm=inv / g["impressoes"] * 1000 if g["impressoes"] else None,
                 taxa_reuniao=g["reunioes"] / g["leads"] * 100 if g["leads"] else None)
        return g
    tot = z("total")
    for i in sel:
        for k in tot:
            if k != "id": tot[k] += G[i][k]
    return {"selecionados": sel, "total": fin(tot), "grupos": [fin(G[i]) for i in PAGAS]}


# ----------------------------------------------------------------------------- MP PRO (Kiwify + Meta Perpétuo)
MPPRO = None   # D["mppro"]; definido por load() ou por quem importa o módulo
ATIVO_DIAS, INADIMPLENTE_DIAS = 40, 25


def _d(s):
    return date.fromisoformat(s[:10])


def _mais1mes(day):
    y, m, d = int(day[:4]), int(day[5:7]) + 1, int(day[8:10])
    if m > 12:
        y, m = y + 1, 1
    prox = date(y + (m == 12), m % 12 + 1, 1)
    return f"{y:04d}-{m:02d}-{min(d, (prox - timedelta(days=1)).day):02d}"


def _media(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def _div(a, b):
    return a / b if b else None


def mppro(MP, f, agora_day):
    """Recalcula a aba MP PRO de forma independente (mesmas regras do SPEC/template)."""
    if not MP or not MP.get("pedidos"):
        return None
    de, ate = f.get("de") or "0000-00-00", f.get("ate") or "9999-12-31"
    T = min(ate, agora_day)
    inP = lambda d: de <= d <= ate
    nome = lambda c: MP["clientes"][c]
    O = [dict(i=i, ts=r[0], day=r[0][:10], c=r[1], sub=r[2], st=r[3], met=r[4], bruto=r[5], taxa=r[6], liq=r[7],
              mot=r[8], rday=r[9][:10] if r[9] else None) for i, r in enumerate(MP["pedidos"])]
    apr = [o for o in O if o["st"] in ("paid", "refunded")]
    rec = [o for o in O if o["st"] == "refused"]
    tent = [o for o in O if o["st"] in ("refused", "waiting")]
    AV = [dict(ts=r[0], day=r[0][:10], c=r[1], st=r[2], met=r[3], bruto=r[4], taxa=r[5], liq=r[6], rday=r[7][:10] if r[7] else None)
          for r in MP.get("avulsos") or []]
    av_p = [o for o in AV if o["st"] == "paid" and inP(o["day"])]
    av_re = [o for o in AV if o["st"] == "refunded" and o["rday"] and inP(o["rday"])]
    # sequência por assinatura e por cliente
    por_sub, por_cli = {}, {}
    for o in apr:
        por_sub.setdefault(o["sub"], []).append(o); por_cli.setdefault(o["c"], []).append(o)
    first_ids = {v[0]["i"] for v in por_sub.values()}
    prev_sub, next_sub, prev_cli = {}, {}, {}
    for v in por_sub.values():
        for a, b in zip(v, v[1:]):
            prev_sub[b["i"]] = a; next_sub[a["i"]] = b
    for v in por_cli.values():
        for a, b in zip(v, v[1:]):
            prev_cli[b["i"]] = a
    is_first = lambda o: o["i"] in first_ids

    def recusas_antes(o):
        lo = (prev_sub.get(o["i"]) or prev_cli.get(o["i"]) or {"ts": ""})["ts"]
        return [r for r in rec if r["c"] == o["c"] and lo < r["ts"] <= o["ts"]]
    soma = lambda xs, k: sum(x[k] for x in xs)

    firsts = [v[0] for v in por_sub.values()]
    novas_subs = [o for o in firsts if inP(o["day"])]
    pagos = [o for o in apr if o["st"] == "paid" and inP(o["day"])]
    pag_novas = [o for o in pagos if is_first(o)]
    pag_ren = [o for o in pagos if not is_first(o)]
    reemb = [o for o in apr if o["st"] == "refunded" and o["rday"] and inP(o["rday"])]
    rec_p = [o for o in rec if inP(o["day"])]
    motivos = Counter((o["mot"] or "nao_informado") for o in rec_p)
    rec_novos = sum(1 for o in rec_p if not any(a["c"] == o["c"] and a["ts"] < o["ts"] for a in apr))

    prev, sem_cob = [], []
    for o in apr:
        if o["st"] != "paid":
            continue
        due = _mais1mes(o["day"])
        if not inP(due) or due > agora_day:
            continue
        n = next_sub.get(o["i"])
        if n:
            at = (_d(n["day"]) - _d(due)).days
            prev.append(dict(c=o["c"], ult=o["day"], due=due, pago=n["day"], atraso=at,
                             status="em_dia" if at <= 0 else "atrasou", recusas=len(recusas_antes(n)), motivo=None))
        else:
            ab = (_d(agora_day) - _d(due)).days
            rr = [r for r in rec if r["c"] == o["c"] and r["ts"] > o["ts"]]
            tt = [r for r in tent if r["c"] == o["c"] and r["ts"] > o["ts"]]
            if ab > INADIMPLENTE_DIAS and not tt:
                sem_cob.append({"cliente": nome(o["c"]), "ultimo": o["day"], "vencimento": due})
                continue
            mot = (rr[-1]["mot"] or "nao_informado") if rr else ("pix_aguardando" if any(r["st"] == "waiting" for r in tt) else None)
            prev.append(dict(c=o["c"], ult=o["day"], due=due, pago=None, atraso=ab,
                             status="inadimplente" if ab > INADIMPLENTE_DIAS else "em_aberto", recusas=len(rr), motivo=mot))
    atrasou = [x for x in prev if x["status"] == "atrasou"]

    # base em T
    aprT = [o for o in apr if o["day"] <= T]
    ef_ref = lambda o: o["st"] == "refunded" and o["rday"] and o["rday"] <= T
    cliT = {}
    for o in aprT:
        cliT.setdefault(o["c"], []).append(o)
    ativos = cancel = reemb_cli = subs_at = mrr = pagos_base = liq_base = 0
    compradores, status = [], {}
    for c, os_ in cliT.items():
        pg = [o for o in os_ if not ef_ref(o)]
        if not pg:
            st = "reembolsado"; reemb_cli += 1
        else:
            ult_sub = {}
            for o in pg:
                ult_sub[o["sub"]] = o
            a = 0
            for o in ult_sub.values():
                if (_d(T) - _d(o["day"])).days <= ATIVO_DIAS:
                    a += 1; mrr += o["bruto"]
            subs_at += a
            st = "ativo" if a else "cancelou"
            if a: ativos += 1
            else: cancel += 1
            pagos_base += len(pg); liq_base += soma(pg, "liq")
        status[c] = st
        compradores.append({"cliente": nome(c), "status": st, "pagamentos": len(pg), "entrada": os_[0]["day"],
                            "ultimo": pg[-1]["day"] if pg else None, "total_pago": soma(pg, "bruto"), "metodo": os_[-1]["met"],
                            "recusas": sum(1 for r in rec if r["c"] == c and r["day"] <= T),
                            "reembolso": next((o["rday"] for o in os_ if ef_ref(o)), None)})
    ordem = {"ativo": 0, "cancelou": 1, "reembolsado": 2}
    compradores.sort(key=lambda x: (ordem[x["status"]], -x["pagamentos"], x["entrada"], x["cliente"]))
    n_comp = len(cliT); nao_re = n_comp - reemb_cli
    liq_medio = _div(liq_base, pagos_base)
    ppa = _div(pagos_base, nao_re)
    ltv = ppa * liq_medio if ppa is not None and liq_medio is not None else None

    # churn por janela de meses
    meses = []
    ini0 = min([x for x in (apr[0]["day"] if apr else None, AV[0]["day"] if AV else None) if x], default=None)
    if ini0:
        ym = ini0[:7]
        while ym <= agora_day[:7]:
            meses.append(ym); ym = _mais1mes(ym + "-01")[:7]
    pagou = lambda ym: {o["c"] for o in aprT if not ef_ref(o) and o["day"][:7] == ym}
    janelas = []
    for ym in meses:
        nx = _mais1mes(ym + "-01")[:7]
        if (_d(_mais1mes(nx + "-01")) - timedelta(days=1)).isoformat() > T:
            break
        b, r = pagou(ym), pagou(nx)
        if not b:
            continue
        sairam = [c for c in b if c not in r]
        janelas.append({"de": ym, "para": nx, "base": len(b), "renovaram": len(b) - len(sairam), "sairam": len(sairam),
                        "churn": len(sairam) / len(b) * 100, "quem": sorted(nome(c) for c in sairam)})
    churn_ult = janelas[-1]["churn"] if janelas else None
    vida = 100 / churn_ult if churn_ult else None

    MD = MP.get("midia") or {"camps": [], "dias": []}
    m = dict(inv=0, imp=0, cl=0, lpv=0, ic=0, px=0); por_camp = {}
    for r in MD["dias"]:
        if not inP(r[0]):
            continue
        for k, v in zip(("inv", "imp", "cl", "lpv", "ic", "px"), r[2:8]):
            m[k] += v
        pc = por_camp.setdefault(r[1], {"campanha": MD["camps"][r[1]]["nome"], "status": MD["camps"][r[1]].get("status"),
                                        "investimento": 0, "impressoes": 0, "cliques": 0, "visitas": 0, "checkouts": 0})
        for k, v in zip(("investimento", "impressoes", "cliques", "visitas", "checkouts"), r[2:7]):
            pc[k] += v
    novas = len(novas_subs); novas_re = sum(1 for o in novas_subs if o["st"] == "refunded")
    cac = _div(m["inv"], novas); liq_p = soma(pagos, "liq") + soma(av_p, "liq")

    evol = []
    for ym in meses:
        pm = [o for o in apr if o["st"] == "paid" and o["day"][:7] == ym]
        ns = sum(1 for o in firsts if o["day"][:7] == ym)
        inv = sum(r[2] for r in MD["dias"] if r[0][:7] == ym)
        fm = [o for o in AV if o["st"] == "paid" and o["day"][:7] == ym]
        evol.append({"mes": ym, "pagamentos": len(pm), "novas": sum(1 for o in pm if is_first(o)),
                     "renovacoes": sum(1 for o in pm if not is_first(o)), "novas_assinaturas": ns,
                     "formacao": len(fm), "formacao_liquido": soma(fm, "liq"), "bruto": soma(pm, "bruto") + soma(fm, "bruto"),
                     "liquido": soma(pm, "liq") + soma(fm, "liq"), "base_paga": len({o["c"] for o in pm}), "investimento": inv, "cac": _div(inv, ns),
                     "parcial": ym == agora_day[:7]})
    coortes = []
    for ym in meses:
        cs = [c for c, v in cliT.items() if v[0]["day"][:7] == ym]
        if cs:
            coortes.append({"mes": ym, "entraram": len(cs), "ativos": sum(1 for c in cs if status[c] == "ativo")})
    pagamentos = []
    for o in apr:
        if not inP(o["day"]):
            continue
        pv = prev_sub.get(o["i"]); due = _mais1mes(pv["day"]) if pv else None; rr = recusas_antes(o)
        pagamentos.append({"tipo": ("Nova, reembolsada" if o["st"] == "refunded" else "Nova") if is_first(o) else "Renovação",
                           "cliente": nome(o["c"]), "dia": o["day"], "vencimento": due,
                           "atraso": (_d(o["day"]) - _d(due)).days if due else None, "metodo": o["met"], "recusas": len(rr),
                           "motivo": (rr[-1]["mot"] or "nao_informado") if rr else None, "_ts": o["ts"]})
    for o in AV:
        if inP(o["day"]):
            pagamentos.append({"tipo": "Formação, reembolsada" if o["st"] == "refunded" else "Formação", "cliente": nome(o["c"]), "dia": o["day"],
                               "vencimento": None, "atraso": None, "metodo": o["met"], "recusas": 0, "motivo": None, "_ts": o["ts"]})
    pagamentos.sort(key=lambda x: x["_ts"])
    for x in pagamentos:
        del x["_ts"]
    return {
        "periodo": {"de": f.get("de") or None, "ate": f.get("ate") or None, "base_em": T},
        "midia": {"investimento": m["inv"], "impressoes": m["imp"], "cliques": m["cl"], "visitas": m["lpv"], "checkouts": m["ic"],
                  "compras_pixel": m["px"], "ctr": _div(m["cl"] * 100, m["imp"]), "cpc": _div(m["inv"], m["cl"]),
                  "cpm": _div(m["inv"] * 1000, m["imp"]), "custo_visita": _div(m["inv"], m["lpv"]), "custo_checkout": _div(m["inv"], m["ic"]),
                  "campanhas": sorted([x for x in por_camp.values() if x["investimento"] > 0], key=lambda x: -x["investimento"])},
        "vendas": {"novas_assinaturas": novas, "novas_reembolsadas": novas_re, "renovacoes_pagas": len(pag_ren), "pagamentos": len(pagos) + len(av_p),
                   "bruto": soma(pagos, "bruto") + soma(av_p, "bruto"), "taxa": soma(pagos, "taxa") + soma(av_p, "taxa"), "liquido": liq_p,
                   "assinatura_liquido": soma(pagos, "liq"), "formacao_qtd": len(av_p), "formacao_bruto": soma(av_p, "bruto"),
                   "formacao_taxa": soma(av_p, "taxa"), "formacao_liquido": soma(av_p, "liq"),
                   "novas_qtd": len(pag_novas), "novas_bruto": soma(pag_novas, "bruto"), "novas_taxa": soma(pag_novas, "taxa"),
                   "novas_liquido": soma(pag_novas, "liq"), "ren_bruto": soma(pag_ren, "bruto"), "ren_taxa": soma(pag_ren, "taxa"),
                   "ren_liquido": soma(pag_ren, "liq"), "reembolsos": len(reemb) + len(av_re), "reembolsos_valor": soma(reemb, "bruto") + soma(av_re, "bruto"),
                   "recusas": len(rec_p), "recusas_clientes": len({o["c"] for o in rec_p}), "recusas_novos": rec_novos,
                   "motivos_recusa": dict(motivos), "resultado": liq_p - m["inv"]},
        "renovacoes": {"previstas": len(prev), "pagas": sum(1 for x in prev if x["pago"]),
                       "em_dia": sum(1 for x in prev if x["status"] == "em_dia"), "atrasaram_pagas": len(atrasou),
                       "em_aberto": sum(1 for x in prev if x["status"] == "em_aberto"),
                       "inadimplentes": sum(1 for x in prev if x["status"] == "inadimplente"),
                       "atraso_medio": _media(x["atraso"] for x in atrasou), "atraso_max": max((x["atraso"] for x in atrasou), default=None),
                       "cancelaram_sem_cobranca": sem_cob,
                       "abertas": [{"cliente": nome(x["c"]), "ultimo": x["ult"], "vencimento": x["due"], "dias": x["atraso"],
                                    "recusas": x["recusas"], "motivo": x["motivo"], "status": x["status"]} for x in prev if not x["pago"]]},
        "base": {"compradores": n_comp, "ativos": ativos, "assinaturas_ativas": subs_at, "cancelaram": cancel, "reembolsados": reemb_cli,
                 "mrr": mrr, "churn_acumulado": _div(cancel * 100, n_comp), "churn_acumulado_com_reembolso": _div((cancel + reemb_cli) * 100, n_comp),
                 "taxa_reembolso": _div(sum(1 for o in aprT if ef_ref(o)) * 100, len(aprT)), "pagamentos_por_assinante": ppa,
                 "liquido_medio": liq_medio, "ltv": ltv},
        "unit": {"cac": cac, "roas": _div(liq_p, m["inv"]), "cac_liquido": _div(m["inv"], novas - novas_re), "custo_por_ativa": _div(m["inv"], subs_at),
                 "payback_meses": cac / liq_medio if cac is not None and liq_medio else None,
                 "ltv_cac": ltv / cac if cac and ltv is not None else None},
        "churn": {"janelas": janelas, "medio": _media(j["churn"] for j in janelas), "ultimo": churn_ult, "vida_media_meses": vida,
                  "ltv_projetado": vida * liq_medio if vida is not None and liq_medio is not None else None},
        "evolucao": evol, "coortes": coortes, "pagamentos": pagamentos, "compradores": compradores,
    }


# ----------------------------------------------------------------------------- função de contrato
def bloco(P, M):
    """Agregados comuns a kpis e consultores."""
    ms = [M[l["id"]] for l in P]
    cont = [m for m in ms if m["contatado"]]
    d1 = [m["dias_1o_contato"] for m in cont]
    return {
        "leads": len(P),
        "contatados": len(cont),
        "pct_contatados": pct(len(cont), len(P)),
        "media_dias_1o_contato": media(d1),
        "mediana_dias_1o_contato": mediana(d1),
        "contatos_por_lead": media(m["n_contatos"] for m in cont),
        "agendados": sum(1 for m in ms if m["agendado"]),
        "reunioes": sum(1 for m in ms if m["reuniao"]),
        "ganhos": sum(1 for l in P if l["s"] == "won"),
        "perdidos": sum(1 for l in P if l["s"] == "lost"),
        "abertos": sum(1 for l in P if l["s"] == "open"),
        "abertos_sem_contato": sum(1 for l in P if l["s"] == "open" and not l["k"]),
        "parados_7d": sum(1 for m in ms if m["parado_7d"]),
    }


FLOW = [345, 346, 348, 349, 350]  # ordem do funil; Cultivo fora


def avanco(P, sid):
    """Entrou na etapa e depois chegou a uma etapa posterior do funil (ou ganho, se não houver) -> avançou."""
    if sid not in FLOW:
        return {"avancaram": 0, "sem_avancar": 0, "media_dias_ate_avancar": None}
    o = FLOW.index(sid); av, sem, ate = 0, 0, []
    for l in P:
        mine = [s for s in (l["h"] or []) if s[0] == sid]
        if not mine:
            continue
        t0 = min(dt(s[1]) for s in mine)
        nxt = [dt(s[1]) for s in l["h"] if s[0] in FLOW and FLOW.index(s[0]) > o and dt(s[1]) >= t0]
        t1 = min(nxt) if nxt else (dt(l["w"]) if l["s"] == "won" and l.get("w") and dt(l["w"]) >= t0 else None)
        if l["s"] == "open" and l["sg"] == sid:
            continue  # está na etapa agora: conta em "ainda estão"
        if t1 is not None:
            av += 1; ate.append(dias(t1, t0))
        else:
            sem += 1
    return {"avancaram": av, "sem_avancar": sem, "media_dias_ate_avancar": media(ate)}


def compute(leads, filtros, agora_str, stages, equipe=(), midia=None):
    agora = dt(agora_str)
    P = filtra(leads, filtros)
    M = {l["id"]: lead_metrics(l, agora) for l in P}
    ms = [M[l["id"]] for l in P]

    b = bloco(P, M)
    kpis = {
        "leads": b["leads"],
        "contatados": b["contatados"],
        "sem_contato": b["leads"] - b["contatados"],
        "pct_contatados": b["pct_contatados"],
        "fim_de_semana": sum(1 for m in ms if m["fds"]),
        "media_dias_1o_contato": b["media_dias_1o_contato"],
        "mediana_dias_1o_contato": b["mediana_dias_1o_contato"],
        "contatos_por_lead": b["contatos_por_lead"],
        "agendados": b["agendados"],
        "reunioes": b["reunioes"],
        "ganhos": b["ganhos"],
        "perdidos": b["perdidos"],
        "abertos": b["abertos"],
        "parados_7d": b["parados_7d"],
    }

    etapas = []
    for st in stages:
        sid = st["id"]
        atuais = [l for l in P if l["s"] == "open" and l["sg"] == sid]
        passaram = [l for l in P if any(s[0] == sid for s in (l["h"] or []))]
        dur_saiu, dur_atual = [], []
        for l in passaram:
            segs = [s for s in l["h"] if s[0] == sid]
            if segs[-1][2] is not None:  # já saiu da etapa
                dur_saiu.append(sum(dias(dt(s[2]), dt(s[1])) for s in segs if s[2] is not None))
        for l in atuais:
            abertos = [s for s in (l["h"] or []) if s[0] == sid and s[2] is None]
            if abertos:
                dur_atual.append(dias(agora, dt(abertos[-1][1])))
        etapas.append({
            "id": sid,
            "atuais": len(atuais),
            "passaram": len(passaram),
            "media_dias_na_etapa": media(dur_saiu),
            "media_dias_atual": media(dur_atual),
            **avanco(P, sid),
        })

    grupos = {}
    for l in P:
        grupos.setdefault(l["c"], []).append(l)
    consultores = []
    for c, ls in grupos.items():
        bc = bloco(ls, M)
        consultores.append({"consultor": c, **bc})
    fc = set(filtros.get("consultores") or [])
    for c in equipe:  # equipe atual sempre aparece, zerada
        if c not in grupos and (not fc or c in fc):
            consultores.append({"consultor": c, **bloco([], {})})
    consultores.sort(key=lambda x: (-x["leads"], x["consultor"]))

    lost = [l for l in P if l["s"] == "lost"]
    lm = [M[l["id"]] for l in lost]
    perdidos = {
        "total": len(lost),
        "media_dias_ate_perda": media(m["dias_ate_perda"] for m in lm if m["dias_ate_perda"] is not None),
        "media_contatos_ate_perda": media(m["contatos_ate_perda"] for m in lm if m["contatos_ate_perda"] is not None),
        "sem_contato": sum(1 for m in lm if m["contatos_ate_perda"] == 0),
        "motivos": dict(Counter(str(l["lr"]) for l in lost)),
    }

    lead_time = {
        "media_dias_1o_contato": b["media_dias_1o_contato"],
        "media_dias_ate_agendamento": media(m["dias_ate_agendamento"] for m in ms
                                            if m["agendado"] and m["dias_ate_agendamento"] is not None),
        "media_dias_ate_reuniao": media(m["dias_ate_reuniao"] for m in ms if m["reuniao"]),
        "media_dias_ate_perda": perdidos["media_dias_ate_perda"],
        "media_dias_ate_ganho": media(m["dias_ate_ganho"] for m in ms if m["dias_ate_ganho"] is not None),
    }
    return {"kpis": kpis, "etapas": etapas, "consultores": consultores,
            "perdidos": perdidos, "lead_time": lead_time,
            "performance": performance(leads, filtros, agora, midia if midia is not None else MIDIA),
            "mppro": mppro(MPPRO, filtros, agora_str[:10])}


# ----------------------------------------------------------------------------- cenários
def filtros_base(de, ate, **kw):
    f = {"de": de, "ate": ate, "consultores": [], "origens": [], "status": [],
         "etapas": [], "etiquetas": [], "busca": "", "incluir_base": True, "campanha": None}
    f.update(kw)
    return f


def cenarios(D):
    hoje = dt(D["gerado_em"]).date()
    iso = date.isoformat
    d7, d30 = iso(hoje - timedelta(days=6)), iso(hoje - timedelta(days=29))
    seg = iso(hoje - timedelta(days=hoje.weekday()))
    h = iso(hoje)
    top = sorted(Counter(l["c"] for l in D["leads"]).items(), key=lambda x: (-x[1], x[0]))[0][0]
    return [
        ("ultimos_7_dias", filtros_base(d7, h)),
        ("esta_semana", filtros_base(seg, h)),
        ("ultimos_30_dias", filtros_base(d30, h)),
        ("tudo", filtros_base("2000-01-01", h)),
        ("ultimos_30_so_triade", filtros_base(d30, h, origens=["Tríade"])),
        ("ultimos_30_consultor_top", filtros_base(d30, h, consultores=[top])),
        ("tudo_so_perdidos", filtros_base("2000-01-01", h, status=["lost"])),
        ("ultimos_30_etapa_mql", filtros_base(d30, h, etapas=[345])),
        ("tudo_etiqueta_digital", filtros_base("2000-01-01", h, etiquetas=["Digital"])),
        ("tudo_busca_trd", filtros_base("2000-01-01", h, busca="trd")),
        ("ultimos_7_so_trd", filtros_base(d7, h, campanha="trd")),
        ("tudo_so_trd", filtros_base("2000-01-01", h, campanha="trd")),
        ("tudo_so_diag", filtros_base("2000-01-01", h, campanha="diag")),
        ("ultimos_30_so_diag", filtros_base(d30, h, campanha="diag")),
        ("tudo_so_rcon", filtros_base("2000-01-01", h, campanha="rcon")),
        ("ultimos_7_so_rcon", filtros_base(d7, h, campanha="rcon")),
        ("ultimos_30_sem_base", filtros_base(d30, h, incluir_base=False)),
        ("mppro_setembro", filtros_base("2026-09-01", "2026-09-30")),
        ("mppro_julho_ate_hoje", filtros_base("2026-07-01", h)),
        ("ultimos_30_consultor_top_perf", filtros_base(d30, h, consultores=[top], campanha="trd")),
        ("mes_passado_diag", filtros_base(iso(hoje.replace(day=1) - timedelta(days=hoje.replace(day=1).day + 27)), iso(hoje.replace(day=1) - timedelta(days=1)), campanha="diag")),
    ]


def load():
    global MIDIA, MPPRO
    with open(DATA, encoding="utf-8") as fh:
        D = json.load(fh)
    MIDIA = D.get("midia")
    MPPRO = D.get("mppro")
    return D


def expected_all(D):
    return [{"cenario": nome, "filtros": f,
             "resultado": compute(D["leads"], f, D["gerado_em"], D["stages"], D.get("equipe") or ())}
            for nome, f in cenarios(D)]


def fmt(v, nd=2):
    return "—" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


# ----------------------------------------------------------------------------- sanidade
def sanity(D):
    L = D["leads"]
    agora = dt(D["gerado_em"])
    tipos = set(D.get("tipos") or {})
    stage_ids = [s["id"] for s in D["stages"]]
    achados = []

    def add(desc, n, extra=""):
        achados.append(f"{desc}: {n}" + (f" ({extra})" if extra else ""))

    c_antes = [l for l in L if l["k"] and dt(l["k"][0][0]) < dt(l["e"])]
    neg = sorted(dias(dt(l["k"][0][0]), dt(l["e"])) for l in c_antes)
    add("Leads com 1º contato ANTES da entrada (dias_1o_contato truncado em 0)", len(c_antes),
        f"pior {fmt(neg[0])} d; ids {[l['id'] for l in c_antes][:10]}" if neg else "")
    k_antes_tot = sum(1 for l in L for x in l["k"] if dt(x[0]) < dt(l["e"]))
    add("Contatos (itens de k) com data anterior à entrada", k_antes_tot)
    add("Contatos com data no futuro (> gerado_em)", sum(1 for l in L for x in l["k"] if dt(x[0]) > agora))
    add("k fora de ordem cronológica", sum(1 for l in L if [x[0] for x in l["k"]] != sorted(x[0] for x in l["k"])))
    dup = sum(len(l["k"]) - len({tuple(x) for x in l["k"]}) for l in L)
    dupl = sum(1 for l in L if len(l["k"]) != len({tuple(x) for x in l["k"]}))
    add("Contatos duplicados exatos (mesmo timestamp+tipo)", dup, f"em {dupl} leads")
    burst = [l["id"] for l in L if len(l["k"]) >= 5 and
             dias(dt(l["k"][4][0]), dt(l["k"][0][0])) * 86400 < 60]
    add("Leads com ≥5 contatos registrados no 1º minuto (lançamento em lote, infla contatos_por_lead)",
        len(burst), f"ids {burst[:10]}")
    seg_inv = [(l["id"], s) for l in L for s in l["h"] if s[2] is not None and dt(s[2]) < dt(s[1])]
    add("Segmentos de etapa com fim < início", len(seg_inv), f"ids {sorted({i for i, _ in seg_inv})[:10]}")
    add("Segmentos de etapa começando antes da entrada `e`",
        sum(1 for l in L for s in l["h"] if dt(s[1]) < dt(l["e"])),
        f"em {sum(1 for l in L if any(dt(s[1]) < dt(l['e']) for s in l['h']))} leads")
    add("Segmentos com etapa fora de D.stages", sum(1 for l in L for s in l["h"] if s[0] not in stage_ids))
    add("Leads com sg fora de D.stages", sum(1 for l in L if l["sg"] not in stage_ids))
    add("Leads lost sem `l`", sum(1 for l in L if l["s"] == "lost" and not l["l"]))
    add("Leads won sem `w`", sum(1 for l in L if l["s"] == "won" and not l["w"]))
    add("Leads lost sem motivo `lr`", sum(1 for l in L if l["s"] == "lost" and not l["lr"]))
    add("Leads open com `l` ou `w` preenchido", sum(1 for l in L if l["s"] == "open" and (l["l"] or l["w"])))
    add("Leads com `h` vazio", sum(1 for l in L if not l["h"]),
        "status " + str(dict(Counter(l["s"] for l in L if not l["h"]))))
    add("Leads open sem segmento aberto na etapa atual sg",
        sum(1 for l in L if l["s"] == "open" and not any(s[0] == l["sg"] and s[2] is None for s in l["h"])))
    add("Leads com 1º segmento de h iniciando >1 dia após a entrada",
        sum(1 for l in L if l["h"] and dias(dt(l["h"][0][1]), dt(l["e"])) > 1))
    fora = Counter(x[1] for l in L for x in l["k"] if x[1] not in tipos)
    add("Contatos com tipo fora de D.tipos", sum(fora.values()), str(dict(fora)) if fora else "")
    lp = [l for l in L if l["s"] == "lost" and l["l"] and dt(l["l"]) < dt(l["e"])]
    add("Lead-time até perda negativo (l < e)", len(lp), f"ids {[l['id'] for l in lp][:10]}")
    wg = [l for l in L if l["s"] == "won" and l["w"] and dt(l["w"]) < dt(l["e"])]
    add("Lead-time até ganho negativo (w < e)", len(wg), f"ids {[l['id'] for l in wg][:10]}")
    rn = [l for l in L if any(x[1] in REUNIAO_TIPOS and dt(x[0]) < dt(l["e"]) for x in l["k"])]
    add("dias_ate_reuniao negativo (reunião antes da entrada; SPEC não trunca em 0)", len(rn))
    won_noag = [l for l in L if l["s"] == "won" and not any(s[0] in AGENDADO_STAGES for s in l["h"])]
    add("Won sem segmento em Reunião/Negociação/Fechamento (agendamento via w)", len(won_noag))
    k_pos_fecho = sum(1 for l in L if l["s"] == "lost" and l["l"] and any(dt(x[0]) > dt(l["l"]) for x in l["k"]))
    add("Leads lost com contatos após a data da perda", k_pos_fecho)
    lotes = Counter(l["e"][:13] for l in L)
    lotes = {h: n for h, n in lotes.items() if n >= 10}
    det = []
    for hr, n in sorted(lotes.items()):
        g = [l for l in L if l["e"][:13] == hr]
        o = Counter(l["o"] for l in g).most_common(1)[0][0]
        det.append(f"{hr}h: {n} leads, origem dominante {o}, {sum(1 for l in g if l['k'])} contatados")
    add("Entradas em lote (≥10 leads na mesma hora — importação, não lead novo)", len(lotes), "; ".join(det))
    add("Leads abertos sem contato e sem atividade pendente", sum(1 for l in L if l["s"] == "open" and not l["k"] and not l["pa"]))
    add("Leads fechados (won/lost) com atividades pendentes", sum(1 for l in L if l["s"] != "open" and l["pa"]))
    add("Entradas por ano", "", str(dict(sorted(Counter(l["e"][:4] for l in L).items()))))

    # outliers (sobre a base inteira)
    ms = [lead_metrics(l, agora) for l in L]
    for campo in ("dias_1o_contato", "dias_ate_agendamento", "dias_ate_reuniao", "dias_ate_perda",
                  "dias_ate_ganho", "dias_sem_contato"):
        xs = sorted(m[campo] for m in ms if m[campo] is not None)
        if not xs:
            continue
        big = [x for x in xs if x > 180]
        add(f"{campo}: n={len(xs)} média={fmt(media(xs))} mediana={fmt(mediana(xs))} máx={fmt(xs[-1])}",
            f"{len(big)} acima de 180 d",
            f"média sem eles={fmt(media([x for x in xs if x <= 180]))}")
    old = [l for l in L if l["e"][:4] == "2024"]
    add("Leads de 2024 ainda abertos", sum(1 for l in old if l["s"] == "open"),
        f"de {len(old)} leads de 2024")
    return achados


# ----------------------------------------------------------------------------- comparação
def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def diff(esp, obt, path, out):
    if is_num(esp) and is_num(obt):
        if abs(esp - obt) > TOL:
            out.append((path, esp, obt))
        return
    if isinstance(esp, dict) and isinstance(obt, dict):
        for k in esp:
            if k not in obt:
                out.append((f"{path}.{k}", esp[k], "<ausente>"))
            else:
                diff(esp[k], obt[k], f"{path}.{k}", out)
        for k in obt:
            if k not in esp:
                out.append((f"{path}.{k}", "<ausente>", obt[k]))
        return
    if isinstance(esp, list) and isinstance(obt, list):
        if len(esp) != len(obt):
            out.append((f"{path}.length", len(esp), len(obt)))
        for i, (a, b) in enumerate(zip(esp, obt)):
            lab = a.get("consultor", a.get("id", i)) if isinstance(a, dict) else i
            diff(a, b, f"{path}[{lab}]", out)
        return
    if esp != obt:
        out.append((path, esp, obt))


def compare(arquivo, D):
    with open(arquivo, encoding="utf-8") as fh:
        front = json.load(fh)
    esp = {c["cenario"]: c for c in expected_all(D)}
    fr = {c["cenario"]: c for c in front}
    total = 0
    status = {}
    for nome, ce in esp.items():
        if nome not in fr:
            print(f"[{nome}] AUSENTE no arquivo do front-end")
            status[nome] = "AUSENTE"
            total += 1
            continue
        cf = fr[nome]
        out = []
        # filtros: front pode omitir chaves vazias? Exigimos igualdade nas chaves do esperado.
        ff = cf.get("filtros") or {}
        for k, v in ce["filtros"].items():
            if ff.get(k, [] if isinstance(v, list) else "") != v:
                out.append((f"filtros.{k}", v, ff.get(k, "<ausente>")))
        # recalcula o esperado com os filtros que o front usou, para não mascarar diferença de cálculo
        res_esp = compute(D["leads"], {**ce["filtros"], **ff}, D["gerado_em"], D["stages"], D.get("equipe") or ()) if ff else ce["resultado"]
        diff(res_esp, cf.get("resultado"), "resultado", out)
        status[nome] = "OK" if not out else f"FALHOU ({len(out)})"
        total += len(out)
        for p, a, b in out:
            print(f"[{nome}] {p}: esperado={json.dumps(a, ensure_ascii=False)} obtido={json.dumps(b, ensure_ascii=False)}")
    extras = set(fr) - set(esp)
    if extras:
        print("Cenários extras no front-end (ignorados):", sorted(extras))
    print("\nResumo:")
    for nome, s in status.items():
        print(f"  {nome:28s} {s}")
    print(f"\n{'OK' if total == 0 else 'FALHOU'} — {total} divergência(s)")
    return 0 if total == 0 else 1


# ----------------------------------------------------------------------------- main
def main(argv):
    if len(argv) < 2 or argv[1] not in ("expected", "compare", "sanity"):
        print(__doc__)
        return 2
    D = load()
    if argv[1] == "expected":
        res = expected_all(D)
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        print(f"Gravado {OUT} ({len(res)} cenários) — agora={D['gerado_em'][:19]}")
        r = res[0]
        k = r["resultado"]["kpis"]
        print(f"\nultimos_7_dias ({r['filtros']['de']} a {r['filtros']['ate']}):")
        for key, v in k.items():
            print(f"  {key:26s} {fmt(v, 3)}")
        print("\nTodos os cenários (leads / contatados / média 1º contato):")
        for c in res:
            kk = c["resultado"]["kpis"]
            print(f"  {c['cenario']:28s} {kk['leads']:4d} / {kk['contatados']:4d} / {fmt(kk['media_dias_1o_contato'])}")
        print("\nChecagem de sanidade dos dados:")
        for a in sanity(D):
            print("  -", a)
        return 0
    if argv[1] == "sanity":
        for a in sanity(D):
            print("-", a)
        return 0
    if len(argv) < 3:
        print("uso: python v2/audit.py compare <arquivo_frontend.json>")
        return 2
    return compare(argv[2], D)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv))
