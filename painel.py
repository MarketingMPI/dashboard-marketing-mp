#!/usr/bin/env python
"""Dashboard do Marketing (B2C Funil Alpha, Pipedrive) - Meu Patrimônio.

Roda na nuvem (GitHub Actions, .github/workflows/atualizar.yml): a cada 30 min e pelo botão "Atualizar agora".
Uso:
  python painel.py ci          # (Actions) restaura cache cifrado, coleta, cifra cache, gera _site/ para o Pages
  python painel.py collect     # busca Pipedrive (incremental), grava data/dashboard.json, imprime resumo
  python painel.py dev         # gera data/dev.html SEM criptografia, para revisão local (nunca publicar)
  python painel.py pages       # gera _site/ (index.html cifrado + assets) a partir do snapshot
  python painel.py refresh     # (local) dispara a atualização na nuvem (workflow_dispatch)

Escopo: TODOS os negócios do pipeline 45 (B2C Funil Alpha), qualquer origem.
Unidade de medida = LEAD. Contato = atividade de contato CONCLUÍDA; atividades
pendentes da cadência (5 por lead) não contam.
Somente stdlib + curl + node. Segredos via variáveis de ambiente (Actions) ou .env local; nunca impressos.
"""
import base64, hashlib, html as htmllib, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request, pathlib
from collections import Counter
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
# Funciona tanto na raiz do projeto quanto em v2/ durante o desenvolvimento.
ROOT = HERE if (HERE / "encrypt.js").exists() else HERE.parent
DATA = ROOT / "data"; OUT = ROOT / "_site"; ASSETS = ROOT / "assets"; CACHE_ENC = ROOT / "cache" / "dash_cache.enc"
WORKFLOW = "atualizar.yml"
DATA.mkdir(exist_ok=True)
CACHE = DATA / "dash_cache.json"
SNAP = DATA / "dashboard.json"
ENV_FILES = [ROOT / ".env", pathlib.Path(r"C:\Users\phenr\Downloads\mp360-landing\.env")]
BASE = "https://meupatrimoniob2c.pipedrive.com/api"
REPO = "MarketingMPI/dashboard-marketing-mp"
PAGES_URL = "https://marketingmpi.github.io/dashboard-marketing-mp/"
PIPELINE = 45
BRT = timezone(timedelta(hours=-3))
STAGES = [(345, "MQL - 1º Contato"), (346, "Agendar"), (348, "Reunião"), (349, "Negociação"), (350, "Fechamento"), (438, "Cultivo")]
STAGE_IDS = {s for s, _ in STAGES}
# Atividades que representam contato com o lead (quando concluídas).
CONTACT_TYPES = {"call", "whatsapp_chat1", "whatsapp_chat", "msg", "email", "meeting", "qualificacao"}  # no-show/tarefa não são contato
TYPE_NAMES = {"call": "Ligação", "whatsapp_chat1": "WhatsApp", "whatsapp_chat": "WhatsApp", "msg": "Mensagem",
              "email": "E-mail", "meeting": "Reunião", "qualificacao": "Reunião de qualificação"}
TEST_RX = re.compile(r"\b(teste|test lead|zzteste|dummy)\b|registro de teste", re.I)


# ---------------------------------------------------------------- HTTP
def token():
    if os.environ.get("PIPEDRIVE_API_TOKEN"): return os.environ["PIPEDRIVE_API_TOKEN"].strip()
    for f in ENV_FILES:
        if f.exists():
            for l in f.read_text(encoding="utf-8").splitlines():
                if l.startswith("PIPEDRIVE_API_TOKEN="):
                    return l.split("=", 1)[1].strip().strip('"')
    sys.exit("PIPEDRIVE_API_TOKEN não encontrado")


TOK = None


def url_for(path, **p):
    global TOK
    TOK = TOK or token()
    p["api_token"] = TOK
    return BASE + path + ("&" if "?" in path else "?") + urllib.parse.urlencode(p)


def get(path, **p):
    # curl é mais estável que o SSL da stdlib neste Windows. A URL (com token) vai por stdin.
    config = f'url = "{url_for(path, **p)}"\n'
    last = "falha desconhecida"
    for i in range(3):
        try:
            r = subprocess.run(["curl", "--config", "-", "-fsS", "--retry", "2", "--retry-all-errors",
                                "--connect-timeout", "10", "--max-time", "40"],
                               input=config, capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return json.loads(r.stdout)
            last = (r.stderr or f"curl código {r.returncode}")[-300:]
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            last = str(e)
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"Falha de conexão com o Pipedrive em {path}: {last}") from None


def get_many(paths):
    """Busca várias URLs em paralelo com um único curl. paths: {chave: (path, params)} -> {chave: json}."""
    out, todo = {}, dict(paths)
    for attempt in range(3):
        if not todo: break
        with tempfile.TemporaryDirectory() as tmp:
            cfg, files = [], {}
            for i, (k, (path, params)) in enumerate(todo.items()):
                f = (pathlib.Path(tmp) / f"{i}.json").as_posix(); files[k] = f
                cfg += [f'url = "{url_for(path, **params)}"', f'output = "{f}"']
            try:
                subprocess.run(["curl", "--config", "-", "-fsS", "--parallel", "--parallel-max", "4",
                                "--retry", "3", "--retry-all-errors", "--connect-timeout", "10", "--max-time", "40"],
                               input="\n".join(cfg) + "\n", capture_output=True, text=True, timeout=900)
            except subprocess.TimeoutExpired:
                pass
            for k, f in files.items():
                try:
                    out[k] = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
        todo = {k: v for k, v in todo.items() if k not in out}
        if todo: time.sleep(5)
    for k, (path, params) in todo.items():  # último recurso, em série
        out[k] = get(path, **params)
    return out


# ---------------------------------------------------------------- datas
def parse_utc(s):
    """'2026-10-01T12:00:00Z' | '2026-10-01 12:00:00' (UTC) -> datetime aware UTC."""
    if not s: return None
    s = s.strip().replace("Z", "").replace("T", " ")[:19]
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try: return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError: return None


def brt(s):
    d = parse_utc(s) if isinstance(s, str) else s
    return d.astimezone(BRT).strftime("%Y-%m-%dT%H:%M:%S") if d else None


# ---------------------------------------------------------------- coleta
def norm_activity(a):
    """Normaliza atividade da v1 (/deals/{id}/activities) ou da v2 (/activities)."""
    return {"id": a["id"], "type": a.get("type") or "", "done": bool(a.get("done")),
            "done_at": (a.get("marked_as_done_time") or None) or None, "due": a.get("due_date"), "due_t": a.get("due_time") or None,
            "add": a.get("add_time"),
            "subj": (a.get("subject") or "")[:60], "deleted": bool(a.get("is_deleted")) or a.get("active_flag") is False}


def fetch_deals():
    deals = []
    for st in ("open", "lost", "won"):
        cur = None
        while True:
            p = dict(pipeline_id=PIPELINE, status=st, limit=500)
            if cur: p["cursor"] = cur
            d = get("/v2/deals", **p)
            deals += [x for x in d.get("data") or [] if x.get("pipeline_id") == PIPELINE]
            cur = (d.get("additional_data") or {}).get("next_cursor")
            if not cur: break
    return deals


def fetch_paged_deal_items(ids, kind):
    """kind='acts' -> v1 /deals/{id}/activities ; kind='log' -> v1 /deals/{id}/changelog."""
    res = {i: [] for i in ids}
    if kind == "acts":
        pending = {i: 0 for i in ids}
        while pending:
            got = get_many({i: (f"/v1/deals/{i}/activities", {"limit": 500, "start": s}) for i, s in pending.items()})
            nxt = {}
            for i, r in got.items():
                res[i] += r.get("data") or []
                pg = (r.get("additional_data") or {}).get("pagination") or {}
                if pg.get("more_items_in_collection"): nxt[i] = pg["next_start"]
            pending = nxt
    else:
        pending = {i: None for i in ids}
        while pending:
            got = get_many({i: (f"/v1/deals/{i}/changelog", ({"limit": 500, "cursor": c} if c else {"limit": 500}))
                            for i, c in pending.items()})
            nxt = {}
            for i, r in got.items():
                res[i] += [e for e in (r.get("data") or []) if e.get("field_key") in ("stage_id", "pipeline_id", "user_id")]
                c = (r.get("additional_data") or {}).get("next_cursor")
                if c: nxt[i] = c
            pending = nxt
    return res


def real_entry(notes):
    """Entrada REAL do lead declarada nas notas do negócio (formatos usados pelo Marketing).
    O add_time do deal é a criação no CRM: lead recuperado/importado entra dias depois de chegar.
    Retorna [iso BRT, fonte] ou None."""
    for n in sorted(notes, key=lambda n: n.get("add_time") or ""):
        t = htmllib.unescape(re.sub(r"<[^>]+>", " ", n.get("content") or ""))
        m = re.search(r"entrou de verdade em\s*(\d{2})/(\d{2})/(\d{4})\s*(?:às|as)?\s*(\d{1,2}):(\d{2})", t, re.I)
        if m:
            d, mo, y, h, mi = map(int, m.groups()); return [datetime(y, mo, d, h, mi).isoformat(), "Lead recuperado do Meta (nota)"]
        m = re.search(r"Enviado em:?\s*(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2})", t, re.I)
        if m:
            d, mo, y, h, mi = map(int, m.groups()); return [datetime(y, mo, d, h, mi).isoformat(), "Formulário Meta (nota 'Enviado em')"]
        m = re.search(r"Entrada \(Meta/form\):?\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*([ap]m)", t, re.I)
        if m:  # exportação CSV do Meta: data no formato US (mês primeiro)
            mo, d, y, h, mi = map(int, m.groups()[:5]); h = h % 12 + (12 if m.group(6).lower() == "pm" else 0)
            return [datetime(y, mo, d, h, mi).isoformat(), "Importação CSV do Meta (nota)"]
        m = re.search(r"Entrada no (Meta|AC):?\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s*(Z|[+-]\d{2}:?\d{2})?", t, re.I)
        if m:
            z = m.group(3) or "-03:00"; z = "+00:00" if z == "Z" else (z if ":" in z else z[:3] + ":" + z[3:])
            dt = datetime.fromisoformat(m.group(2) + z).astimezone(BRT).replace(tzinfo=None)
            return [dt.isoformat(), f"Entrada no {m.group(1)} (nota)"]
    return None


def person_keys(p):
    """Identidade da pessoa: hash do e-mail e dos 8 últimos dígitos do telefone. Nada legível é guardado."""
    H = lambda x: hashlib.sha256(x.encode()).hexdigest()[:16]
    em = {H("e:" + e["value"].strip().lower()) for e in (p.get("email") or []) if (e.get("value") or "").strip()}
    ph = {H("t:" + re.sub(r"\D", "", x["value"])[-8:]) for x in (p.get("phone") or []) if len(re.sub(r"\D", "", x.get("value") or "")) >= 8}
    return sorted(em | ph)


def sync(full=False):
    """Atualiza o cache. Deals: sempre todos. Atividades/changelog: só negócios novos/alterados,
    + varredura de atividades alteradas desde a última execução. Recarga completa 1x por dia."""
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    started = datetime.now(timezone.utc)
    today = started.astimezone(BRT).date().isoformat()
    full = full or cache.get("full_day") != today
    deals = fetch_deals()
    users = {u["id"]: u["name"] for u in get("/v1/users")["data"]}
    fields = get("/v1/dealFields", limit=500)["data"]
    labels = {o["id"]: o["label"] for f in fields if f["key"] == "label" for o in (f.get("options") or [])}
    pipes = {p["id"]: p["name"] for p in get("/v1/pipelines")["data"]}
    stage_pipe = {s["id"]: pipes.get(s["pipeline_id"], str(s["pipeline_id"])) for s in get("/v1/stages")["data"]}
    per = {} if full else {int(k): v for k, v in (cache.get("per_deal") or {}).items()}
    changed = [d["id"] for d in deals if full or d["id"] not in per or per[d["id"]].get("u") != d.get("update_time")]
    if changed:
        acts = fetch_paged_deal_items(changed, "acts")
        logs = fetch_paged_deal_items(changed, "log")
        notes = get_many({i: ("/v1/notes", {"deal_id": i, "limit": 500}) for i in changed})
        pids = sorted({d["person_id"] for d in deals if d["id"] in acts and d.get("person_id")})
        persons = get_many({pid: (f"/v1/persons/{pid}", {}) for pid in pids})
        for d in deals:
            if d["id"] in acts:
                pr = (persons.get(d.get("person_id")) or {}).get("data") or {}
                per[d["id"]] = {"u": d.get("update_time"),
                                "ent": real_entry((notes.get(d["id"]) or {}).get("data") or []),
                                "pk": person_keys(pr),
                                "acts": [norm_activity(a) for a in acts[d["id"]]],
                                "log": [{"k": e["field_key"], "o": e.get("old_value"), "n": e.get("new_value"), "t": e.get("time")}
                                        for e in logs[d["id"]]]}
    # Atividades concluídas/alteradas sem mexer no negócio (garantia extra entre as recargas diárias)
    if not full and cache.get("last_run"):
        since = (parse_utc(cache["last_run"]) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ids = {d["id"] for d in deals}; cur = None
        while True:
            p = {"updated_since": since, "limit": 500}
            if cur: p["cursor"] = cur
            r = get("/v2/activities", **p)
            for a in r.get("data") or []:
                did = a.get("deal_id")
                if did in ids and did in per:
                    lst = [x for x in per[did]["acts"] if x["id"] != a["id"]]
                    n = norm_activity(a)
                    if not n["deleted"]: lst.append(n)
                    per[did]["acts"] = lst
            cur = (r.get("additional_data") or {}).get("next_cursor")
            if not cur: break
    ids = {d["id"] for d in deals}
    per = {k: v for k, v in per.items() if k in ids}
    cache = {"full_day": today if full else cache.get("full_day"), "last_run": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "deals": deals, "users": {str(k): v for k, v in users.items()}, "labels": {str(k): v for k, v in labels.items()},
             "stage_pipe": {str(k): v for k, v in stage_pipe.items()},
             "per_deal": {str(k): v for k, v in per.items()}}
    tmp = CACHE.with_suffix(".tmp"); tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8"); tmp.replace(CACHE)
    return cache, len(changed), full


# ---------------------------------------------------------------- modelagem
def origem(title, label_names):
    t = (title or "").strip().upper(); L = {x.upper() for x in label_names}
    if "TRIADE" in L or re.match(r"^TRD\s*\|", t): return "Tríade"
    if t.startswith("TRD-INT"): return "TRD-INT"
    if "DIGITAL" in L or t.startswith("DIAG"): return "Digital"
    if "EXCLIENTE" in L or t.startswith("EXCL"): return "Ex-cliente"
    if "IND" in L or re.match(r"^IND\b", t) or any(x.startswith("IND ") for x in L): return "Indicação"
    if any(x.startswith(("EV-", "EVFO", "EV3FO", "EV ")) for x in L) or re.match(r"^EV\w*FO\b", t): return "Eventos"
    if "MFO" in L or t.startswith("MFO"): return "MFO"
    if "RCON" in L or t.startswith("RCON"): return "RCON"
    if t.startswith("CURITIBA"): return "Curitiba"
    if "OUTBOUND" in L or any(x.startswith("PROSP") for x in L): return "Prospecção"
    if "INBOUND" in L: return "Inbound"
    if t.startswith(("WHATSAPP", "CHAT")): return "WhatsApp/Chat"
    return "Outros"


def stage_history(deal, log):
    """Segmentos [stage_id, início BRT, fim BRT|None] só para etapas do Funil Alpha."""
    ch = sorted(((parse_utc(e["t"]), e) for e in log if e["k"] == "stage_id" and e.get("t")), key=lambda x: x[0])
    add = parse_utc(deal.get("add_time"))
    cur = int(ch[0][1]["o"]) if ch and str(ch[0][1].get("o") or "").isdigit() else deal["stage_id"]
    start = add; segs = []
    for t, e in ch:
        segs.append([cur, start, t])
        cur = int(e["n"]) if str(e.get("n") or "").isdigit() else cur; start = t
    end = parse_utc(deal.get("won_time") or deal.get("lost_time")) if deal["status"] != "open" else None
    segs.append([cur, start, end])
    return [[s, brt(a), brt(max(a, b) if b else None)] for s, a, b in segs if s in STAGE_IDS and a]


def contact_time(a):
    """Quando o contato aconteceu. Atividade CRIADA já concluída com vencimento anterior é registro
    retroativo (ex.: 'Whatsapp chat (2024-07-08)' lançado meses depois): vale o vencimento, não o clique."""
    done, add = parse_utc(a.get("done_at")), parse_utc(a.get("add"))
    if done and a.get("due"):
        retro = add is not None and abs((done - add).total_seconds()) < 120 and a["due"] < brt(done)[:10]
        # vencimento > 3 anos antes do registro = data digitada errada (ex.: 2015): fica a data do registro
        if retro and a["due"] >= (done - timedelta(days=3 * 365)).date().isoformat():
            if a.get("due_t"):
                return brt(parse_utc(a["due"] + " " + a["due_t"][:5] + ":00"))
            return a["due"] + "T12:00:00"
    if done: return brt(done)
    return a["due"] + "T12:00:00" if a.get("due") else None


def collapse_sweeps(contacts, minutes=10):
    """Conclusões do MESMO tipo em menos de 10 min no mesmo lead = um único toque. Na prática é o consultor
    marcando a cadência inteira (A01..A15) como feita de uma vez; contar 5 contatos inflaria 'contatos por lead'."""
    out = []
    for t, typ in contacts:
        prev = next((x for x in reversed(out) if x[1] == typ), None)
        if prev and (parse_utc(t.replace("T", " ")) - parse_utc(prev[0].replace("T", " "))).total_seconds() < minutes * 60:
            continue
        out.append([t, typ])
    return out


def moved_into_alpha(deal, log, stage_pipe):
    """(entrada BRT, funil de origem) da ÚLTIMA vez que o negócio veio de outro funil para o Alpha, se isso aconteceu
    mais de 1 dia depois da criação. None quando o negócio nasceu no Alpha."""
    last = None
    for e in sorted((e for e in log if e["k"] == "stage_id" and e.get("t")), key=lambda e: e["t"]):
        o = int(e["o"]) if str(e.get("o") or "").isdigit() else None
        n = int(e["n"]) if str(e.get("n") or "").isdigit() else None
        if n in STAGE_IDS and o is not None and o not in STAGE_IDS:
            last = (parse_utc(e["t"]), stage_pipe.get(str(o), "outro funil"))
    add = parse_utc(deal.get("add_time"))
    if last and add and (last[0] - add).total_seconds() > 86400:
        return brt(last[0]), last[1]
    return None


def base_import_ids(deals, per):
    """Lotes de importação de BASE (lista de contatos subida de uma vez, ex.: ex-clientes):
    origem Import, >= 10 negócios do mesmo criador na mesma hora e sem data real de entrada em nota.
    Não são leads gerados no período; o painel os separa."""
    grp = {}
    for d in deals:
        if d.get("origin") == "Import":
            grp.setdefault((d.get("creator_user_id"), (d.get("add_time") or "")[:13]), []).append(d)
    out = set()
    for v in grp.values():
        if len(v) >= 10:
            out |= {d["id"] for d in v if not (per.get(str(d["id"])) or {}).get("ent")}
    return out


def model(cache):
    users = {int(k): v for k, v in cache["users"].items()}
    labels = {int(k): v for k, v in cache["labels"].items()}
    per = cache["per_deal"]
    base = base_import_ids(cache["deals"], per)
    leads, excl = [], Counter()
    for d in cache["deals"]:
        title = (d.get("title") or "").strip()
        if TEST_RX.search(title) or TEST_RX.search(d.get("lost_reason") or ""):
            excl["teste"] += 1; continue
        info = per.get(str(d["id"])) or {"acts": [], "log": []}
        lb = [labels.get(i, str(i)) for i in (d.get("label_ids") or [])]
        owner = users.get(d.get("owner_id"), "Sem responsável")
        if owner == "Marketing": owner = "Sem consultor (Marketing)"
        # set(): o mesmo contato lançado em duplicidade (mesmo instante e tipo) conta uma vez só
        contacts = [list(x) for x in sorted({(contact_time(a), a["type"])
                    for a in info["acts"] if a["done"] and a["type"] in CONTACT_TYPES and contact_time(a)})]
        contacts = collapse_sweeps(contacts)
        crm = brt(d.get("add_time")); ent = info.get("ent")
        hist = stage_history(d, info["log"])
        reat = False
        moved = moved_into_alpha(d, info["log"], cache.get("stage_pipe") or {})
        if moved:
            # Negócio que já existia em outro funil e foi (re)colocado no Alpha: a entrada é a chegada ao Alpha,
            # e os contatos do ciclo anterior não contam para este ciclo.
            entry, fonte = moved[0], f"Entrou no Funil Alpha vindo de {moved[1]} (criado no CRM em {crm[8:10]}/{crm[5:7]}/{crm[:4]})"
            contacts = [x for x in contacts if x[0] >= entry]
            reat = "base de relacionamento" in moved[1].lower()
        else:
            entry, fonte = (ent[0], ent[1]) if ent else (crm, "Criação no CRM")
            if contacts and contacts[0][0] < entry:  # houve contato antes: o lead já existia
                entry, fonte = contacts[0][0], "1º contato registrado (anterior à criação no CRM)"
        pend = sorted(a["due"] for a in info["acts"] if not a["done"] and a.get("due"))
        leads.append({
            "id": d["id"], "n": title, "s": d["status"], "sg": d["stage_id"], "c": owner, "o": origem(title, lb), "lb": lb,
            "e": entry, "ec": crm, "ef": fonte, "b": 1 if (d["id"] in base or reat) else 0, "pk": info.get("pk") or [], "w": brt(d.get("won_time")), "l": brt(d.get("lost_time")),
            "lr": (d.get("lost_reason") or "").strip() or ("Sem motivo informado" if d["status"] == "lost" else None),
            "k": contacts,
            "pa": len(pend), "nx": pend[0] if pend else None,
            "h": hist,
        })
    return dedupe(leads), excl


def dedupe(leads, window_days=14):
    """Uma pessoa = um lead POR CICLO. Negócios da mesma pessoa (e-mail/telefone) que entraram com até 14 dias de
    diferença são duplicidade do mesmo ciclo e viram um lead só (canônico: aberto/ganho antes de perdido; empate ->
    o mais recente; entrada = a mais antiga; contatos = união). Uma nova conversão depois disso é um ciclo novo e
    conta no período em que aconteceu (marcada como reincidente)."""
    par = {}
    def f(x):
        while par.setdefault(x, x) != x: x = par[x]
        return x
    for l in leads:
        for k in l["pk"]:
            a, b = f(("d", l["id"])), f(("k", k))
            if a != b: par[a] = b
    groups = {}
    for l in leads: groups.setdefault(f(("d", l["id"])), []).append(l)
    out = []
    for g in groups.values():
        g.sort(key=lambda l: l["e"])
        clusters = []
        for l in g:
            if clusters and (datetime.fromisoformat(l["e"]) - datetime.fromisoformat(clusters[-1][0]["e"])).total_seconds() <= window_days * 86400:
                clusters[-1].append(l)
            else:
                clusters.append([l])
        for i, cl in enumerate(clusters):
            cl_sorted = sorted(cl, key=lambda l: (l["s"] != "lost", l["ec"] or ""), reverse=True)
            c = dict(cl_sorted[0])
            if len(cl) > 1:
                first = cl[0]
                c["e"] = first["e"]
                c["ef"] = first["ef"] if first is cl_sorted[0] else f"{first['ef']} — negócio anterior #{first['id']}"
                c["k"] = [list(x) for x in sorted({tuple(x) for l in cl for x in l["k"]})]
                c["b"] = min(l["b"] for l in cl)
                c["dd"] = [l["id"] for l in cl_sorted[1:]]
            if i > 0:
                prev = clusters[i - 1][0]
                c["re"] = prev["e"][:10]  # reincidente: já tinha entrado antes
                c["ef"] = c["ef"] + f" · reincidente (entrada anterior em {prev['e'][8:10]}/{prev['e'][5:7]}/{prev['e'][:4]})"
            c.pop("pk", None)
            out.append(c)
    return out


def collect(full=False):
    now = datetime.now(timezone.utc)
    cache, n_changed, was_full = sync(full)
    leads, excl = model(cache)
    snap = {"gerado_em": now.astimezone(BRT).strftime("%Y-%m-%dT%H:%M:%S-03:00"),
            "stages": [{"id": s, "nome": n} for s, n in STAGES],
            "tipos": TYPE_NAMES, "leads": leads,
            "meta": {"pipeline": "B2C Funil Alpha (45)", "excluidos_teste": excl.get("teste", 0),
                     "coleta": "completa" if was_full else f"incremental ({n_changed} negócios atualizados)"}}
    tmp = SNAP.with_suffix(".tmp"); tmp.write_text(json.dumps(snap, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"); tmp.replace(SNAP)
    return snap


def stable_summary(s):
    L = s["leads"]; c = Counter(x["s"] for x in L)
    out = [f"Leads Funil Alpha: {len(L)} | abertos {c['open']} | perdidos {c['lost']} | ganhos {c['won']}"]
    st = Counter(x["sg"] for x in L if x["s"] == "open")
    for sid, n in STAGES: out.append(f"Etapa {n}: {st.get(sid, 0)}")
    cont = sum(1 for x in L if x["k"]); out.append(f"Com contato realizado: {cont} | sem contato: {len(L) - cont}")
    out.append(f"Contatos realizados: {sum(len(x['k']) for x in L)}")
    for k, v in sorted(Counter(x["c"] for x in L).items()): out.append(f"Consultor {k}: {v}")
    for k, v in sorted(Counter(x["lr"] for x in L if x["s"] == "lost").items()): out.append(f"Motivo {k}: {v}")
    return "\n".join(out)


# ---------------------------------------------------------------- build
def env(name):
    if os.environ.get(name): return os.environ[name].strip()
    f = ROOT / ".env"
    if f.exists():
        for l in f.read_text(encoding="utf-8").splitlines():
            if l.startswith(name + "="): return l.split("=", 1)[1].strip()
    return None


def node_bin():
    n = shutil.which("node")
    if n: return n
    for c in (pathlib.Path.home() / "AppData" / "Local" / "hermes" / "tools").glob("node-*/node.exe"):
        return str(c)
    sys.exit("node não encontrado")


def crypt(mode, data):
    """AES-GCM com chave PBKDF2 da PAINEL_SENHA (crypt.js). mode: enc (bytes->json str) | dec (json str->bytes)."""
    pw = env("PAINEL_SENHA")
    if not pw: sys.exit("PAINEL_SENHA ausente")
    r = subprocess.run([node_bin(), str(ROOT / "crypt.js"), mode], input=data, capture_output=True,
                       env={**os.environ, "PAINEL_SENHA": pw})
    if r.returncode: raise RuntimeError(f"falha em crypt {mode}: " + r.stderr.decode(errors="ignore")[-200:])
    return r.stdout


def encrypt(s):
    return crypt("enc", json.dumps(s, ensure_ascii=False).encode("utf-8")).decode()


def template():
    return (HERE / "template.html").read_text(encoding="utf-8")


def build_pages(out=OUT):
    """_site/ = index.html cifrado + assets. O token do botão "Atualizar agora" (DISPATCH_TOKEN, só Actions deste
    repositório) vai DENTRO do conteúdo cifrado: só aparece depois da senha."""
    s = json.loads(SNAP.read_text(encoding="utf-8"))
    tok = env("DISPATCH_TOKEN")
    payload = dict(s, refresh={"repo": REPO, "workflow": WORKFLOW, "token": tok} if tok else None)
    html = template().replace("/*__DATA__*/null", encrypt(payload))
    assert '"leads"' not in html and "gerado_em" not in html.split("/*__RAW__*/")[0], "dado em texto puro no HTML"
    out = pathlib.Path(out)
    shutil.rmtree(out, ignore_errors=True); out.mkdir(parents=True)
    (out / "index.html").write_text(html, encoding="utf-8")
    shutil.copytree(ASSETS, out / "assets")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return s


def build_dev(out=None):
    """HTML sem senha e sem criptografia, só para revisão local. NUNCA publicar."""
    s = json.loads(SNAP.read_text(encoding="utf-8"))
    out = pathlib.Path(out) if out else DATA / "dev.html"
    html = template().replace("/*__RAW__*/null", json.dumps(s, ensure_ascii=False))
    html = html.replace('href="assets/', f'href="{ASSETS.as_uri()}/').replace('src="assets/', f'src="{ASSETS.as_uri()}/')
    html = html.replace("url(assets/", f"url({ASSETS.as_uri()}/")
    out.write_text(html, encoding="utf-8")
    return out


# ---------------------------------------------------------------- nuvem (GitHub Actions)
def ci():
    """Execução no Actions. O cache incremental (tem nomes de leads) só existe CIFRADO fora do runner."""
    if CACHE_ENC.exists():
        try:
            CACHE.write_bytes(crypt("dec", CACHE_ENC.read_bytes()))
        except Exception:
            print("cache ilegível; coleta completa")
    s = collect()
    CACHE_ENC.parent.mkdir(exist_ok=True)
    CACHE_ENC.write_bytes(crypt("enc", CACHE.read_bytes()))
    build_pages()
    # Logs do Actions são PÚBLICOS: nada de nomes, consultores ou números do negócio aqui.
    print("ok", s["gerado_em"], s["meta"]["coleta"])


def github_token():
    """Credencial do Git Credential Manager, sem prompt (helper 'manager' direto: o seletor gráfico trava)."""
    e = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        r = subprocess.run(["git", "-c", "credential.helper=", "-c", "credential.helper=manager", "credential", "fill"],
                           input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True, timeout=30, env=e)
    except (OSError, subprocess.TimeoutExpired):
        return None
    v = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    return v.get("password")


def dispatch():
    """Pede uma atualização à nuvem (o mesmo que o botão do dashboard)."""
    tok = github_token()
    if not tok: sys.exit("sem credencial do GitHub")
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
                                 data=b'{"ref":"main"}', method="POST",
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
                                          "User-Agent": "Dashboard-Marketing"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("ATUALIZACAO_SOLICITADA" if r.status == 204 else f"HTTP {r.status}")


def pages_mode():
    """'workflow' quando o Pages já é publicado pelo Actions; 'legacy' enquanto serve o index.html do branch."""
    tok = github_token()
    if not tok: return "legacy"
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/pages", headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json", "User-Agent": "Dashboard-Marketing"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read()).get("build_type") or "legacy"
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return "legacy"


def publish_legacy():
    """Transição: enquanto o Pages ainda serve o branch, coleta aqui e faz push do index.html cifrado."""
    s = collect(); build_pages()
    shutil.copyfile(OUT / "index.html", ROOT / "index.html")
    e = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    g = lambda *a, t=120: subprocess.run(["git", "-C", str(ROOT), "-c", "credential.helper=", "-c", "credential.helper=manager",
                                          "-c", "user.name=MarketingMPI", "-c", "user.email=MarketingMPI@users.noreply.github.com", *a],
                                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=t, env=e)
    g("pull", "--rebase", "--autostash", "origin", "main")
    g("add", "index.html"); g("commit", "-m", f"Atualiza Dashboard do Marketing {s['gerado_em'][:16]}")
    p = g("push", "origin", "HEAD:main", t=240)
    if p.returncode: print("ERRO_PUSH:", (p.stderr or p.stdout)[-300:]); sys.exit(1)
    print("PUBLICADO", s["gerado_em"])


def refresh():
    """Tarefa local: na nuvem só pede a execução; na transição publica daqui."""
    dispatch() if pages_mode() == "workflow" else publish_legacy()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    if cmd == "ci": ci()
    elif cmd == "collect": print(stable_summary(collect(full="--full" in sys.argv)))
    elif cmd == "pages": build_pages(); print("ok", OUT)
    elif cmd == "dev": print("ok", build_dev(sys.argv[2] if len(sys.argv) > 2 else None))
    elif cmd in ("refresh", "publish"): refresh()
    else: sys.exit(__doc__)
