#!/usr/bin/env python
"""Dashboard do Marketing (B2C Funil Alpha, Pipedrive) - Meu Patrimônio.

Roda na nuvem (GitHub Actions, .github/workflows/atualizar.yml): a cada 1 hora e pelo botão "Atualizar agora".
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
import base64, hashlib, html as htmllib, json, os, unicodedata, re, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request, pathlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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
# Consultores da equipe atual: aparecem sempre no dashboard, mesmo sem leads no recorte.
EQUIPE = ["Danilo Ribeiro", "Pedro Francisco", "Victor Carvalho"]
PII_RX = [(re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "(e-mail oculto)"),
          (re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)"), "(CPF oculto)"),
          (re.compile(r"(?<!\d)(\+?55\s?)?\(?\d{2}\)?\s?9?\d{4}[-\s]?\d{4}(?!\d)"), "(telefone oculto)")]


def sem_pii(txt):
    """Dashboard mostra só o nome: e-mail, CPF ou telefone digitados no título do negócio são mascarados."""
    for rx, rep in PII_RX:
        txt = rx.sub(rep, txt or "")
    return txt


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
    for i in range(5):
        try:
            r = subprocess.run(["curl", "--config", "-", "-fsS", "--retry", "4", "--retry-all-errors", "--retry-delay", "3",
                                "--connect-timeout", "10", "--max-time", "40"],
                               input=config, capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return json.loads(r.stdout)
            last = (r.stderr or f"curl código {r.returncode}")[-300:]
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            last = str(e)
        time.sleep((15 if "429" in last else 3) * (i + 1))  # 429 = limite de requisições do Pipedrive
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
                subprocess.run(["curl", "--config", "-", "-fsS", "--parallel", "--parallel-max", "3", "--rate", "6/s",
                                "--retry", "5", "--retry-all-errors", "--connect-timeout", "10", "--max-time", "40"],
                               input="\n".join(cfg) + "\n", capture_output=True, text=True, timeout=900)
            except subprocess.TimeoutExpired:
                pass
            for k, f in files.items():
                try:
                    out[k] = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
        todo = {k: v for k, v in todo.items() if k not in out}
        if todo: time.sleep(20)
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
        # Cada atividade de contato CONCLUÍDA = 1 contato (pedido do usuário: A01 e A02 marcadas = 2 contatos,
        # mesmo concluídas no mesmo minuto). Pendentes não contam.
        contacts = sorted([contact_time(a), a["type"]]
                          for a in info["acts"] if a["done"] and a["type"] in CONTACT_TYPES and contact_time(a))
        crm = brt(d.get("add_time")); ent = info.get("ent")
        hist = stage_history(d, info["log"])
        reat = False
        moved = moved_into_alpha(d, info["log"], cache.get("stage_pipe") or {})
        fechado = brt(d.get("won_time") or d.get("lost_time"))
        if moved and fechado and fechado < moved[0]:
            # ganho/perdido em OUTRO funil e só depois arquivado no Alpha: o ciclo aconteceu antes da mudança,
            # então a entrada continua sendo a criação (senão o tempo até ganho/perda fica negativo)
            moved = None
        if moved:
            # Negócio que já existia em outro funil e foi (re)colocado no Alpha: a entrada é a chegada ao Alpha,
            # e os contatos do ciclo anterior não contam para este ciclo.
            entry, fonte = moved[0], f"Entrou no Funil Alpha vindo de {moved[1]} (criado no CRM em {crm[8:10]}/{crm[5:7]}/{crm[:4]})"
            contacts = [x for x in contacts if x[0] >= entry]
            hist = [x for x in hist if x[1] >= entry[:19]]  # jornada do ciclo atual (etapas de antes da volta ficam fora)
            reat = "base de relacionamento" in moved[1].lower()
        else:
            entry, fonte = (ent[0], ent[1]) if ent else (crm, "Criação no CRM")
            if contacts and contacts[0][0] < entry:  # houve contato antes: o lead já existia
                entry, fonte = contacts[0][0], "1º contato registrado (anterior à criação no CRM)"
        pend = sorted(a["due"] for a in info["acts"] if not a["done"] and a.get("due"))
        leads.append({
            "id": d["id"], "n": sem_pii(title), "s": d["status"], "sg": d["stage_id"], "c": owner, "o": origem(title, lb), "lb": lb,
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
                c["k"] = sorted(x for l in cl for x in l["k"])
                c["b"] = min(l["b"] for l in cl)
                c["dd"] = [l["id"] for l in cl_sorted[1:]]
            if i > 0:
                prev = clusters[i - 1][0]
                c["re"] = prev["e"][:10]  # reincidente: já tinha entrado antes
                c["ef"] = c["ef"] + f" · reincidente (entrada anterior em {prev['e'][8:10]}/{prev['e'][5:7]}/{prev['e'][:4]})"
            c.pop("pk", None)
            out.append(c)
    return out


# ---------------------------------------------------------------- Meta Ads (investimento em mídia, SOMENTE LEITURA)
META_ACCOUNT = "act_451721936206114"   # Meu Patrimônio CA (única conta autorizada)
META_API = "https://graph.facebook.com/v26.0/"
META_DESDE = "2025-01-01"


def meta_token():
    """Token da Meta: segredo do Actions/.env (META_ACCESS_TOKEN) ou o login OAuth do Hermes (local). Nunca impresso."""
    t = env("META_ACCESS_TOKEN")
    if t: return t
    f = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "mcp-tokens" / "meta-ads.json"
    if f.exists():
        try: return json.loads(f.read_text(encoding="utf-8"))["access_token"]
        except Exception: return None
    return None


def meta_get(path, tok, **q):
    q["access_token"] = tok
    url, rows = META_API + path + "?" + urllib.parse.urlencode(q), []
    while url:
        for tent in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "mp-dashboard/1.0"})
                d = json.loads(urllib.request.urlopen(req, timeout=120).read())
                break
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403) or tent == 3: raise
                time.sleep(5 * (tent + 1))
            except Exception:
                if tent == 3: raise
                time.sleep(5 * (tent + 1))
        rows += d.get("data", [])
        url = (d.get("paging") or {}).get("next")
    return rows


def grupo_midia(nome):
    """Campanhas B2C da Meta, agrupadas pelo nome. B2B (MP PRO, lives, consultores) fica de fora (None)."""
    n = nome or ""
    b2c = re.search(r"\[B2C\]", n, re.I) or re.search(r"tr[ií]ade|diagn[oó]stico", n, re.I)
    if not b2c or re.search(r"\[B2B\]", n, re.I): return None
    if re.search(r"tr[ií]ade", n, re.I): return "trd"
    if re.search(r"diagn[oó]stico", n, re.I): return "diag"
    if re.search(r"\[ISCA\]", n, re.I): return "isca"
    return "outros"


def sync_midia(old):
    """Investimento diário por campanha (incremental: reprocessa os últimos 35 dias, que a Meta ainda ajusta)."""
    old = old or {}
    tok = meta_token()
    if not tok:
        return {**old, "erro": "sem token da Meta"}
    hoje = datetime.now(BRT).date()
    dias = old.get("dias") or {} if old.get("v") == 2 else {}
    since = META_DESDE if not dias else (hoje - timedelta(days=35)).isoformat()
    try:
        rows = meta_get(f"{META_ACCOUNT}/insights", tok, level="campaign", time_increment=1, limit=500,
                        time_range=json.dumps({"since": since, "until": hoje.isoformat()}),
                        fields="campaign_id,campaign_name,spend,impressions,inline_link_clicks,actions")
        camps = {c["id"]: c for c in meta_get(f"{META_ACCOUNT}/campaigns", tok, limit=500,
                                               fields="id,name,effective_status")}
    except Exception as e:
        code = getattr(e, "code", None)
        return {**old, "erro": f"falha ao consultar a Meta ({code or type(e).__name__})"}
    dias = {k: v for k, v in dias.items() if k[:10] < since}
    nomes = dict(old.get("nomes") or {})
    for r in rows:
        cid = r["campaign_id"]; nomes[cid] = r["campaign_name"]
        act = {a["action_type"]: int(float(a["value"])) for a in r.get("actions") or []}
        dias[f"{r['date_start']}|{cid}"] = [int(round(float(r.get("spend") or 0) * 100)), int(r.get("impressions") or 0),
                                            int(r.get("inline_link_clicks") or 0), act.get("landing_page_view", 0),
                                            act.get("initiate_checkout", 0), act.get("purchase", 0)]
    for cid, c in camps.items():
        if cid in nomes: nomes[cid] = c["name"]
    status = {cid: c.get("effective_status") for cid, c in camps.items()}
    return {"v": 2, "dias": dias, "nomes": nomes, "status": status, "erro": None,
            "atualizado_em": datetime.now(BRT).strftime("%Y-%m-%dT%H:%M:%S-03:00")}


def midia_payload(m):
    """Só campanhas B2C. Investimento em CENTAVOS (inteiro) para a soma ser exata no dashboard e na auditoria."""
    if not m or not m.get("dias"):
        return {"camps": [], "dias": [], "atualizado_em": None, "erro": (m or {}).get("erro") or "sem dados"}
    camps, idx, dias = [], {}, []
    for key in sorted(m["dias"]):
        day, cid = key.split("|")
        g = grupo_midia(m["nomes"].get(cid, ""))
        v = m["dias"][key]
        if g is None or not any(v): continue
        if cid not in idx:
            idx[cid] = len(camps)
            camps.append({"id": cid, "nome": m["nomes"].get(cid, cid), "grupo": g, "status": (m.get("status") or {}).get(cid)})
        dias.append([day, idx[cid]] + v[:3])
    return {"camps": camps, "dias": dias, "atualizado_em": m.get("atualizado_em"), "erro": m.get("erro")}


# ---------------------------------------------------------------- MP PRO (Kiwify + Meta Perpétuo, SOMENTE LEITURA)
KIWIFY_API = "https://public-api.kiwify.com/v1"
MPPRO_PRODUTO = "5243d100-6347-11f1-9029-d1b5a9444144"   # MP PRO (club, R$ 49/mês, Janela Fundadora)
FORMACAO_PRODUTO = "a35d7750-16fb-11f1-834f-21c1163effb3"  # MP PRO - Formação de Consultores de Investimentos (venda avulsa)
FORMACAO_DESDE = "2026-03-01"
MPPRO_DESDE = "2026-07-01"
KIWIFY_ENV = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "profiles" / "apine" / ".env"


def kiwify_cfg(name):
    v = env(name)
    if v: return v
    if KIWIFY_ENV.exists():
        for l in KIWIFY_ENV.read_text(encoding="utf-8", errors="replace").splitlines():
            if l.startswith(name + "="): return l.split("=", 1)[1].strip().strip('"').strip("'")


def kiwify_get(path, tok, **q):
    url = KIWIFY_API + path + "?" + urllib.parse.urlencode(q)
    for tent in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mp-dashboard/1.0", "Authorization": "Bearer " + tok,
                                                       "x-kiwify-account-id": kiwify_cfg("KIWIFY_ACCOUNT_ID")})
            return json.loads(urllib.request.urlopen(req, timeout=90).read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404) or tent == 3: raise
            time.sleep(8 * (tent + 1))
        except Exception:
            if tent == 3: raise
            time.sleep(8 * (tent + 1))


def kiwify_sales(produto=MPPRO_PRODUTO, desde=MPPRO_DESDE):
    """Todos os pedidos de um produto (padrão: MP PRO). Janelas de 90 dias; end_date é EXCLUSIVO (a próxima janela começa nele)."""
    cid, sec = kiwify_cfg("KIWIFY_CLIENT_ID"), kiwify_cfg("KIWIFY_CLIENT_SECRET")
    if not (cid and sec and kiwify_cfg("KIWIFY_ACCOUNT_ID")): raise RuntimeError("sem credenciais da Kiwify")
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": sec}).encode()
    tok = json.loads(urllib.request.urlopen(urllib.request.Request(KIWIFY_API + "/oauth/token", data=body, method="POST",
        headers={"User-Agent": "mp-dashboard/1.0", "Content-Type": "application/x-www-form-urlencoded"}), timeout=60).read())["access_token"]
    ini, fim, out = datetime.fromisoformat(desde).date(), datetime.now(BRT).date() + timedelta(days=2), {}
    while ini < fim:
        e = min(ini + timedelta(days=90), fim); page = 1
        while True:
            d = kiwify_get("/sales", tok, start_date=ini.isoformat(), end_date=e.isoformat(), product_id=produto,
                           page_size=100, page_number=page, view_full_sale_details="true").get("data") or []
            for x in d: out[x["id"]] = x
            if len(d) < 100: break
            page += 1; time.sleep(0.7)
        ini = e
    return list(out.values())


def _brt(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(BRT).strftime("%Y-%m-%dT%H:%M:%S") if ts else None


def _mes_seguinte(day):
    y, m, d = int(day[:4]), int(day[5:7]) + 1, int(day[8:10])
    if m > 12: y, m = y + 1, 1
    ult = (datetime(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return f"{y:04d}-{m:02d}-{min(d, ult):02d}"


def _bruto(x):
    """Preço do produto (sem os juros do parcelamento, que o comprador paga à Kiwify): bruto − taxa = líquido."""
    pg = x.get("payment") or {}
    return int(pg.get("product_base_price") or pg.get("charge_amount") or 0)


def mppro_pedidos(sales, avulsas=()):
    """Pedidos -> payload. Assinatura reconstruída por cliente (a API não expõe assinaturas): um pagamento novo
    entra na assinatura cujo último pagamento foi há 20+ dias (a de vencimento mais antigo); senão abre outra
    assinatura (cliente com duas assinaturas paralelas). E-mail só serve de chave e NÃO vai para o payload."""
    cli, nomes, pedidos, subs, nsub = {}, [], [], {}, 0
    rows = []
    for x in sales:
        st = x.get("status")
        if st not in ("paid", "refunded", "refused", "waiting_payment"): continue
        ts = _brt(x.get("approved_date") if st != "refused" else x.get("created_at")) or _brt(x.get("created_at"))
        rows.append((ts, x))
    rows.sort(key=lambda r: (r[0], r[1]["id"]))
    for ts, x in rows:
        c = x["customer"]
        key = (c.get("email") or c.get("id") or "").strip().lower()
        if key not in cli: cli[key] = len(nomes); nomes.append(None)
        ci = cli[key]; nomes[ci] = sem_pii((c.get("name") or "").strip()) or "(sem nome)"
        st, sub = {"waiting_payment": "waiting"}.get(x["status"], x["status"]), -1   # waiting = cobrança em andamento (Pix)
        if st in ("paid", "refunded"):
            day = ts[:10]
            cand = [s for s in subs.setdefault(ci, []) if (datetime.fromisoformat(day) - datetime.fromisoformat(s[1])).days >= 20]
            if cand:
                s = min(cand, key=lambda s: (_mes_seguinte(s[1]), s[0])); s[1] = day; sub = s[0]
            else:
                sub = nsub; subs[ci].append([nsub, day]); nsub += 1
        pg = x.get("payment") or {}
        pedidos.append([ts, ci, sub, st, "pix" if "pix" in (x.get("payment_method") or "") else "cartao",
                        _bruto(x), int(pg.get("fee") or 0), int(x.get("net_amount") or pg.get("net_amount") or 0),
                        x.get("card_rejection_reason") or None, _brt(x.get("refunded_at"))])
    # Formação de Consultores: venda avulsa (não é assinatura) -> só soma na receita
    av = []
    for x in avulsas:
        if x.get("status") not in ("paid", "refunded"): continue
        c = x["customer"]; key = (c.get("email") or c.get("id") or "").strip().lower()
        if key not in cli: cli[key] = len(nomes); nomes.append((c.get("name") or "").strip() or "(sem nome)")
        pg = x.get("payment") or {}
        av.append([_brt(x.get("approved_date") or x.get("created_at")), cli[key], x["status"],
                   "pix" if "pix" in (x.get("payment_method") or "") else "cartao", _bruto(x), int(pg.get("fee") or 0),
                   int(x.get("net_amount") or pg.get("net_amount") or 0), _brt(x.get("refunded_at")), int(x.get("installments") or 1)])
    av.sort(key=lambda r: r[0])
    return {"clientes": nomes, "pedidos": pedidos, "avulsos": av}


def eh_perpetuo(nome):
    return bool(re.search(r"mp\s*pro", nome or "", re.I) and re.search(r"perp[eé]tuo", nome or "", re.I))


def mppro_payload(midia, old):
    """MP PRO = Kiwify (vendas/assinantes) + Meta só das campanhas 'MP PRO ... Perpétuo'. Falha numa fonte mantém o último dado."""
    out = dict(old or {})
    try:
        out.update(mppro_pedidos(kiwify_sales(), kiwify_sales(FORMACAO_PRODUTO, FORMACAO_DESDE))); out["erro_kiwify"] = None
        out["kiwify_em"] = datetime.now(BRT).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    except Exception as e:
        out["erro_kiwify"] = f"falha ao consultar a Kiwify ({getattr(e, 'code', None) or type(e).__name__})"
    camps, idx, dias = [], {}, []
    for key in sorted((midia or {}).get("dias") or {}):
        day, cid = key.split("|")
        nome = midia["nomes"].get(cid, "")
        v = midia["dias"][key]
        if not eh_perpetuo(nome) or not any(v): continue
        if cid not in idx:
            idx[cid] = len(camps); camps.append({"id": cid, "nome": nome, "status": (midia.get("status") or {}).get(cid)})
        dias.append([day, idx[cid]] + (list(v) + [0, 0, 0])[:6])
    out["midia"] = {"camps": camps, "dias": dias, "atualizado_em": (midia or {}).get("atualizado_em"), "erro": (midia or {}).get("erro")}
    out.setdefault("clientes", []); out.setdefault("pedidos", []); out.setdefault("avulsos", [])
    return out


# ---------------------------------------------------------------- Instagram (Meta Graph API, SOMENTE LEITURA)
IG_CONTAS = [("17841444843479536", "meupatrimonio.nacional", "Meu Patrimônio"),
             ("17841435917222082", "rede.meupatrimonio", "Rede Meu Patrimônio"),
             ("17841425597508477", "gabriel.meupatrimonio", "Gabriel · Meu Patrimônio"),
             ("17841407560835140", "renanlimacfp", "Renan Lima")]
IG_SEM_PERMISSAO = []                          # perfis visíveis no Business sem insights liberados
IG_DESDE = "2026-01-01"
IG_DIA = ["reach", "views", "accounts_engaged", "total_interactions", "likes", "comments", "shares", "saves", "reposts",
          "profile_links_taps", "website_clicks"]
IG_ORG = ["views", "reach", "total_interactions", "likes", "comments", "shares", "saves"]   # aceitam breakdown=media_product_type
IG_BACKFILL_DIAS = int(os.environ.get("IG_BACKFILL", "300"))
IG_POSTS_POR_EXEC = int(os.environ.get("IG_POSTS", "150"))                         # dias novos por conta por execução (o resto entra nas próximas)


class _Limite(Exception):
    pass


def ig_get(path, tok, uso, **q):
    q["access_token"] = tok
    url = META_API + path + "?" + urllib.parse.urlencode(q)
    for tent in range(3):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "mp-dashboard/1.0"}), timeout=60)
            h = r.headers.get("x-business-use-case-usage")
            if h:
                try:
                    for v in json.loads(h).values():
                        for x in v: uso[0] = max(uso[0], x.get("call_count", 0), x.get("total_time", 0), x.get("total_cputime", 0))
                except Exception: pass
            return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if '"code":4' in body or '"code":17' in body or '"code":32' in body or '"code":613' in body: raise _Limite()
            if e.code in (400, 401, 403) or tent == 2: raise
            time.sleep(4 * (tent + 1))
        except Exception:
            if tent == 2: raise
            time.sleep(4 * (tent + 1))


def _meia_noite(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=BRT).timestamp())


def sync_instagram(old):
    """Totais diários (dia de Brasília), seguidores e posts com insights. Incremental: refaz os 3 últimos dias e
    completa o histórico aos poucos; posts recentes têm os números atualizados com mais frequência."""
    old = old or {}
    tok = meta_token()
    if not tok: return {**old, "erro": "sem token da Meta"}
    agora = datetime.now(BRT); hoje = agora.date(); uso = [0]
    contas = old.get("contas") or {}
    erro = None
    try:
        for cid, user, nome in IG_CONTAS:
            c = contas.setdefault(cid, {"dias": {}, "seg": {}, "novos": {}, "posts": {}})
            c.setdefault("org", {})
            c["user"], c["nome"] = user, nome
            pf = ig_get(cid, tok, uso, fields="followers_count,follows_count,media_count")
            c["seguidores"], c["seguindo"], c["midias"] = pf.get("followers_count"), pf.get("follows_count"), pf.get("media_count")
            c["seg"][hoje.isoformat()] = c["seguidores"]
            # novos seguidores por dia (a Meta só guarda os últimos 30 dias)
            fc = ig_get(f"{cid}/insights", tok, uso, metric="follower_count", period="day",
                        since=_meia_noite((hoje - timedelta(days=29)).isoformat()), until=_meia_noite(hoje.isoformat()))
            for v in ((fc.get("data") or [{}])[0].get("values") or []):
                d = (datetime.fromisoformat(v["end_time"].replace("+0000", "+00:00")) - timedelta(days=1)).date().isoformat()
                c["novos"][d] = v.get("value")
            # totais por dia: refaz os 3 últimos e completa o histórico do mais recente para o mais antigo
            alvo, d = [], hoje
            while d.isoformat() >= IG_DESDE:
                k = d.isoformat()
                if (hoje - d).days <= 2 or k not in c["dias"] or k not in c["org"]: alvo.append(k)
                d -= timedelta(days=1)
            recentes = [k for k in alvo if (hoje - datetime.fromisoformat(k).date()).days <= 2]
            alvo = recentes + [k for k in alvo if k not in recentes][:IG_BACKFILL_DIAS]

            def um_dia(k):
                fim = min(_meia_noite((datetime.fromisoformat(k) + timedelta(days=1)).date().isoformat()), int(agora.timestamp()))
                if k in c["dias"] and (hoje - datetime.fromisoformat(k).date()).days > 2:
                    vals = dict(zip(IG_DIA, c["dias"][k]))          # totais já lidos; falta só a separação orgânico × anúncio
                else:
                    r = ig_get(f"{cid}/insights", tok, uso, metric=",".join(IG_DIA), period="day", metric_type="total_value",
                               since=_meia_noite(k), until=fim)
                    vals = {x["name"]: (x.get("total_value") or {}).get("value") for x in r.get("data") or []}
                # mesma janela quebrada por tipo de conteúdo: o balde "AD" é o que veio de anúncio pago
                o = ig_get(f"{cid}/insights", tok, uso, metric=",".join(IG_ORG), period="day", metric_type="total_value",
                           breakdown="media_product_type", since=_meia_noite(k), until=fim)
                org = {}
                for x in o.get("data") or []:
                    tv = x.get("total_value") or {}
                    org[x["name"]] = {"_total": tv.get("value"), **{y["dimension_values"][0]: y["value"]
                                      for b in tv.get("breakdowns") or [] for y in b.get("results") or []}}
                return k, [vals.get(m) for m in IG_DIA], org
            for i0 in range(0, len(alvo), 24):          # lotes de 24 dias, 6 chamadas simultâneas; para se a Meta apertar
                if uso[0] > 70 and i0: break
                with ThreadPoolExecutor(6) as ex:
                    for k, v, org in ex.map(um_dia, alvo[i0:i0 + 24]): c["dias"][k] = v; c["org"][k] = org
            # posts (feed e reels) desde IG_DESDE
            after, fim_lista = None, False
            while not fim_lista:
                q = dict(fields="id,timestamp,media_type,media_product_type,permalink,caption,like_count,comments_count", limit=50)
                if after: q["after"] = after
                r = ig_get(f"{cid}/media", tok, uso, **q)
                for m in r.get("data") or []:
                    ts = datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00")).astimezone(BRT)
                    if ts.date().isoformat() < IG_DESDE: fim_lista = True; break
                    po = c["posts"].setdefault(m["id"], {})
                    po.update(ts=ts.strftime("%Y-%m-%dT%H:%M:%S"), tipo=m.get("media_product_type") if m.get("media_product_type") == "REELS" else m.get("media_type"),
                              link=m.get("permalink"), leg=re.sub(r"\s+", " ", (m.get("caption") or "")).strip()[:140],
                              likes=m.get("like_count"), com=m.get("comments_count"))
                after = ((r.get("paging") or {}).get("cursors") or {}).get("after") if (r.get("paging") or {}).get("next") else None
                if not after: fim_lista = True
            fila = []
            for mid, po in c["posts"].items():
                idade = (agora - datetime.fromisoformat(po["ts"]).replace(tzinfo=BRT)).total_seconds() / 3600
                ult = po.get("upd"); desde_ult = (agora - datetime.fromisoformat(ult).replace(tzinfo=BRT)).total_seconds() / 3600 if ult else 1e9
                if (idade <= 14 * 24 and desde_ult >= 2) or (idade <= 90 * 24 and desde_ult >= 24) or desde_ult >= 24 * 7:
                    fila.append((desde_ult < 1e9, -idade, mid))
            fila = [m for *_, m in sorted(fila)][:IG_POSTS_POR_EXEC]   # nunca lidos primeiro, depois os mais recentes

            def um_post(mid):
                po = c["posts"][mid]
                met = "reach,views,likes,comments,shares,saved,total_interactions" + (",ig_reels_avg_watch_time" if po["tipo"] == "REELS" else "")
                try:
                    r = ig_get(f"{mid}/insights", tok, uso, metric=met)
                except urllib.error.HTTPError:
                    try: r = ig_get(f"{mid}/insights", tok, uso, metric="reach,likes,comments,shares,saved,total_interactions")
                    except urllib.error.HTTPError: return mid, None
                return mid, {x["name"]: (x.get("values") or [{}])[0].get("value") for x in r.get("data") or []}
            if uso[0] <= 80:
                with ThreadPoolExecutor(6) as ex:
                    for mid, ins in ex.map(um_post, fila):
                        if ins is not None: c["posts"][mid]["ins"] = ins; c["posts"][mid]["upd"] = agora.strftime("%Y-%m-%dT%H:%M:%S")
    except _Limite:
        erro = "limite de chamadas da Meta atingido; completa na próxima atualização"
    except Exception as e:
        erro = f"falha ao consultar o Instagram ({getattr(e, 'code', None) or type(e).__name__})"
    return {"contas": contas, "erro": erro, "atualizado_em": agora.strftime("%Y-%m-%dT%H:%M:%S-03:00")}


def ig_payload(ig):
    out = {"contas": [], "erro": (ig or {}).get("erro"), "atualizado_em": (ig or {}).get("atualizado_em"), "sem_permissao": IG_SEM_PERMISSAO}
    for cid, user, nome in IG_CONTAS:
        c = ((ig or {}).get("contas") or {}).get(cid)
        if not c: continue
        dias = []
        for k, v in sorted(c["dias"].items()):
            org = (c.get("org") or {}).get(k)
            linha = [k] + list(v) + [c["novos"].get(k)]
            if org is None:
                linha += [None] * 9
            else:
                def organico(m):
                    b = org.get(m) or {}
                    if m == "reach":      # alcance não soma: alcance orgânico ≈ soma dos tipos não pagos, limitada ao total
                        n = sum(val for t, val in b.items() if t not in ("AD", "_total"))
                        return min(n, b.get("_total") or n)
                    return (b.get("_total") or 0) - (b.get("AD") or 0)
                linha += [organico(m) for m in IG_ORG] + [(org.get("views") or {}).get("STORY") or 0, (org.get("views") or {}).get("AD") or 0]
            dias.append(linha)
        posts = []
        for mid, po in sorted(c["posts"].items(), key=lambda x: x[1]["ts"]):
            i = po.get("ins") or {}
            posts.append([po["ts"], po.get("tipo"), po.get("link"), po.get("leg"), i.get("reach"), i.get("views"),
                          i.get("likes", po.get("likes")), i.get("comments", po.get("com")), i.get("shares"), i.get("saved"),
                          i.get("total_interactions"), i.get("ig_reels_avg_watch_time")])
        out["contas"].append({"id": cid, "user": user, "nome": nome, "seguidores": c.get("seguidores"), "seguindo": c.get("seguindo"),
                              "midias": c.get("midias"), "seg": sorted([k, v] for k, v in c["seg"].items() if v is not None),
                              "dias": dias, "posts": posts})
    return out


PIPEDRIVE_INTERVALO_MIN = 60      # leads do Pipedrive: no máximo 1 leitura por hora (cota diária da API)


def pipedrive_pular(full=False):
    """Separa as atualizações: o agendamento de hora em hora lê o Pipedrive; publicações pedidas à mão
    (botão "Atualizar agora", mudanças de layout, rodadas locais) só releem Meta, Kiwify e Instagram,
    a não ser que a última leitura do Pipedrive tenha mais de 1 hora. PIPEDRIVE_FORCAR=1 força a leitura."""
    if full or os.environ.get("PIPEDRIVE_FORCAR") == "1" or not CACHE.exists(): return False, ""
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
        idade = (datetime.now(timezone.utc) - parse_utc(cache["last_run"])).total_seconds() / 60
    except Exception:
        return False, ""
    if not cache.get("per_deal"): return False, ""
    agendado = os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    limite = 20 if agendado else PIPEDRIVE_INTERVALO_MIN - 5      # agendado: só evita leitura dupla
    if idade < limite:
        return True, f"última leitura há {idade:.0f} min (Pipedrive só de hora em hora)"
    return False, ""


def pipedrive_bloqueado():
    """1 chamada leve: se o Pipedrive já responde 429 (limite diário), não vale gastar 15 min em novas tentativas."""
    for tent in range(2):
        try:
            urllib.request.urlopen(urllib.request.Request("https://api.pipedrive.com/v1/users/me?api_token=" + token(),
                                                          headers={"User-Agent": "mp-dashboard/1.0"}), timeout=30)
            return False
        except urllib.error.HTTPError as e:
            if e.code != 429: return False
            if tent == 0: time.sleep(20)
        except Exception:
            return False
    return True


# ---------------------------------------------------------------- MP HUB (usuários) — fotografia importada à mão
# A API do MP HUB exige login com código por e-mail, então a coleta é feita no navegador logado e importada com
# `python painel.py mphub-import <arquivo.json>`. O arquivo vai CIFRADO (PAINEL_SENHA) para fontes/mphub.enc.json;
# só entram campos sem dado de contato (nada de e-mail, telefone ou IP).
MPHUB_ENC = ROOT / "fontes" / "mphub.enc.json"
MPHUB_CAMPOS = ["id", "nome", "role", "company_nome", "office_nome", "created_at", "last_login_at", "last_login_device",
                "last_login_browser", "cidade", "uf", "perfil_profissional", "instituicao", "pl_aproximado", "tempo_mercado_anos"]


def mphub_import(arq):
    d = json.loads(pathlib.Path(arq).read_text(encoding="utf-8"))
    us = [{k: u.get(k) for k in MPHUB_CAMPOS} for u in d["usuarios"]]
    out = {"extraido_em": d["extraido_em"], "usuarios": us, "acessos_24h": d.get("acessos_24h") or []}
    MPHUB_ENC.parent.mkdir(exist_ok=True)
    MPHUB_ENC.write_bytes(crypt("enc", json.dumps(out, ensure_ascii=False).encode("utf-8")))
    return len(us)


def mphub_e_mp(u):
    """É da Meu Patrimônio: vinculado à empresa MP no HUB OU instituição informada = Meu Patrimônio / MP."""
    if (u.get("company_nome") or "").strip(): return True
    n = re.sub(r"[^a-z ]", "", unicodedata.normalize("NFKD", u.get("instituicao") or "").encode("ascii", "ignore").decode().lower()).strip()
    return n == "mp" or "meu patrimonio" in n


def _brt_min(ts):          # MP HUB: data/hora BRT sem segundos (NÃO confundir com _brt da Kiwify)
    if not ts: return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(BRT).strftime("%Y-%m-%dT%H:%M")


def mphub_payload():
    if not MPHUB_ENC.exists(): return None
    try:
        d = json.loads(crypt("dec", MPHUB_ENC.read_bytes()))
    except Exception:
        return {"erro": "não foi possível ler a fotografia do MP HUB", "usuarios": []}
    us = []
    for u in d["usuarios"]:
        pl = u.get("pl_aproximado")
        us.append([u["id"], sem_pii(u.get("nome") or ""), u.get("role"), u.get("company_nome") or u.get("office_nome"),
                   _brt_min(u.get("created_at")), _brt_min(u.get("last_login_at")), u.get("last_login_device"), u.get("cidade"),
                   (u.get("uf") or "").upper() or None, u.get("perfil_profissional"), float(pl) if pl not in (None, "") else None,
                   u.get("tempo_mercado_anos"), u.get("office_nome"), mphub_e_mp(u)])
    return {"extraido_em": _brt_min(d["extraido_em"]), "usuarios": us, "acessos_24h": d.get("acessos_24h") or []}


def collect(full=False):
    now = datetime.now(timezone.utc)
    erro_pd = None
    pular, motivo = pipedrive_pular(full)
    try:
        if pular:
            cache = json.loads(CACHE.read_text(encoding="utf-8")); n_changed, was_full = 0, False
            print("Pipedrive:", motivo, "— mantendo a leitura de", cache.get("last_run"))
        else:
            if CACHE.exists() and pipedrive_bloqueado(): raise RuntimeError("429 (sondagem)")
            cache, n_changed, was_full = sync(full)
    except Exception as e:                     # Pipedrive fora/limite diário: segue com a última leitura boa
        if not CACHE.exists(): raise
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
        if not cache.get("per_deal"): raise
        n_changed, was_full = 0, False
        erro_pd = "limite diário de consultas do Pipedrive atingido" if "429" in str(e) else "falha de conexão com o Pipedrive"
        print("AVISO Pipedrive:", erro_pd, "— usando a leitura anterior")
    leads, excl = model(cache)
    cache["midia"] = sync_midia(cache.get("midia"))
    cache["mppro"] = mppro_payload(cache["midia"], cache.get("mppro"))
    cache["ig"] = sync_instagram(cache.get("ig"))
    tmp = CACHE.with_suffix(".tmp"); tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8"); tmp.replace(CACHE)
    snap = {"gerado_em": now.astimezone(BRT).strftime("%Y-%m-%dT%H:%M:%S-03:00"),
            "stages": [{"id": s, "nome": n} for s, n in STAGES],
            "tipos": TYPE_NAMES, "equipe": EQUIPE, "leads": leads, "midia": midia_payload(cache["midia"]), "mppro": cache["mppro"], "ig": ig_payload(cache["ig"]), "mphub": mphub_payload(),
            "meta": {"pipeline": "B2C Funil Alpha (45)", "excluidos_teste": excl.get("teste", 0),
                     "coleta": "completa" if was_full else f"incremental ({n_changed} negócios atualizados)",
                     "erro_pipedrive": erro_pd, "pipedrive_em": cache.get("last_run"),
                     "pipedrive_intervalo_min": PIPEDRIVE_INTERVALO_MIN}}
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
    html = re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]*>\n', "", html)
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
    print("ok", s["gerado_em"], s["meta"]["coleta"], "| midia:", "ok" if not s["midia"]["erro"] else s["midia"]["erro"],
          "| kiwify:", "ok" if not s["mppro"].get("erro_kiwify") else s["mppro"]["erro_kiwify"],
          "| instagram:", "ok" if not s["ig"].get("erro") else s["ig"]["erro"])


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
    elif cmd == "mphub-import": print("ok", mphub_import(sys.argv[2]), "usuários")
    elif cmd == "dev": print("ok", build_dev(sys.argv[2] if len(sys.argv) > 2 else None))
    elif cmd in ("refresh", "publish"): refresh()
    else: sys.exit(__doc__)
