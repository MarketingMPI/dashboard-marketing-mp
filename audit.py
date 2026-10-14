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
    so_trd = bool(f.get("so_trd"))
    for l in leads:
        if l.get("b") and not base:
            continue
        if so_trd and not re.match(r"^\s*TRD\s*\|", l.get("n") or "", re.I):
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


def compute(leads, filtros, agora_str, stages, equipe=()):
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
            "perdidos": perdidos, "lead_time": lead_time}


# ----------------------------------------------------------------------------- cenários
def filtros_base(de, ate, **kw):
    f = {"de": de, "ate": ate, "consultores": [], "origens": [], "status": [],
         "etapas": [], "etiquetas": [], "busca": "", "incluir_base": True, "so_trd": False}
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
        ("ultimos_7_so_trd", filtros_base(d7, h, so_trd=True)),
        ("tudo_so_trd", filtros_base("2000-01-01", h, so_trd=True)),
        ("ultimos_30_sem_base", filtros_base(d30, h, incluir_base=False)),
    ]


def load():
    with open(DATA, encoding="utf-8") as fh:
        return json.load(fh)


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
