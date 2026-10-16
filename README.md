# Dashboard do Marketing — Meu Patrimônio

Painel de BI do marketing da Meu Patrimônio: leads do **B2C Funil Alpha** (Pipedrive), investimento em mídia (Meta Ads), vendas do **MP PRO** (Kiwify), usuários do **MP HUB** e redes sociais (Instagram e YouTube) — tudo numa página só, atualizada sozinha de hora em hora.

**Publicado em:** https://marketingmpi.github.io/dashboard-marketing-mp/
A página é protegida por senha: os dados vão para o GitHub Pages **criptografados (AES‑256‑GCM)** e só são decifrados no navegador depois que a senha é digitada. Quem não tem a senha vê apenas a tela de bloqueio.

---

## Como funciona (em uma frase)

A cada hora o GitHub Actions roda `painel.py`, busca os dados nas APIs, gera um único `index.html` com os dados **cifrados** dentro e publica no GitHub Pages.

```
Pipedrive ─┐
Meta Ads  ─┤
Kiwify    ─┼─► painel.py ─► data/dashboard.json ─► cifra (AES-GCM) ─► _site/index.html ─► GitHub Pages
Instagram ─┤                                                              ▲
MP HUB    ─┤                                                        template.html
YouTube   ─┘                                                     (HTML+CSS+JS, sem bibliotecas)
```

- **Unidade de medida = LEAD** (não atividade). Contato = atividade de contato **concluída**; atividades pendentes da cadência não contam.
- Todas as datas são tratadas em **horário de Brasília**, e o "agora" do painel é sempre `gerado_em` (nunca o relógio do navegador) — assim os números batem com a auditoria.
- O dashboard **não guarda dados pessoais**: e‑mail, CPF e telefone que apareçam no título do negócio são mascarados na coleta.

---

## O que o painel mostra

**B2C — Análises** (respeitam os filtros de período, consultor, origem, status, etapa, etiqueta e busca)

| Aba | Conteúdo |
|---|---|
| Visão Macro | KPIs com variação vs. período anterior, leads por dia, origem, consultor, mini‑funil, taxa de conversão |
| Performance | Investimento em mídia × leads × resultados |
| Lead a Lead | Tabela de todos os leads do recorte, exportável em CSV, com drawer de detalhe por lead |
| Lead‑time | Tempo médio até 1º contato, agendamento, reunião, perda e ganho; envelhecimento dos abertos |
| Funil / Mapa do CRM | Etapa por etapa: atuais, quem passou, tempo médio na etapa; matriz Consultor × Etapa |
| Perdidos | Motivos, perdas por etapa e por consultor, tempo e contatos até a perda |
| Consultores | Funil de eficiência por consultor e ranking comparativo |

**B2B — Análises:** MP PRO (vendas Kiwify), Usuários MP HUB, Aplicação Direta.
**Redes Sociais:** Instagram (4 contas) e YouTube (canal Gabriel Liberato · Meu Patrimônio).

As definições exatas de cada métrica estão em [`SPEC.md`](SPEC.md) — é o **contrato**: o front‑end (`window.MP_DASH.compute`) e a auditoria em Python (`audit.py`) implementam as mesmas fórmulas.

---

## Fontes de dados

| Fonte | Como entra | Frequência |
|---|---|---|
| **Pipedrive** — pipeline 45 (B2C Funil Alpha) | API, coleta incremental | 1×/hora (cota diária da API) |
| **Meta Ads** — conta Meu Patrimônio CA, campanhas `[B2C]` | Graph API (reprocessa os últimos 35 dias) | toda execução |
| **Kiwify** — MP PRO (assinatura) e Formação de Consultores | Public API | toda execução |
| **Instagram** — 4 contas MP | Graph API (somente leitura) | toda execução, com backfill gradual |
| **MP HUB** — usuários | **importação manual** (`fontes/mphub.enc.json`) | sob demanda |
| **YouTube** — exportação do YouTube Studio | **importação manual** (`fontes/youtube.enc.json`) | sob demanda |

Se o Pipedrive falhar ou estourar o limite diário, a coleta segue com a última leitura boa e o painel avisa no cabeçalho.

---

## Comandos

```bash
python painel.py collect     # coleta e grava data/dashboard.json (imprime resumo sem dados pessoais)
python painel.py collect --full   # força coleta completa (ignora o cache incremental)
python painel.py dev         # gera data/dev.html SEM criptografia, para revisão local — NUNCA publicar
python painel.py pages       # gera _site/ (index.html cifrado + assets) a partir do snapshot
python painel.py ci          # o que o GitHub Actions roda: cache cifrado -> coleta -> cifra -> _site/
python painel.py refresh     # pede uma atualização à nuvem (mesmo efeito do botão "Atualizar agora")

python painel.py mphub-import  <arquivo.json>   # fotografia dos usuários do MP HUB -> fontes/mphub.enc.json
python painel.py youtube-import <export.zip>    # exportação do YouTube Studio -> fontes/youtube.enc.json
```

Auditoria e testes:

```bash
python audit.py expected                 # recalcula as métricas em Python e grava data/audit_expected.json
python audit.py compare <frontend.json>  # compara o front-end com o esperado (exit 1 se divergir)
python audit.py sanity                   # só a checagem de sanidade dos dados
python -m unittest discover tests        # testes (placeholders do template, origem, histórico de etapas, ...)
```

**Dependências:** só a biblioteca padrão do Python 3 + `node` (usado apenas para cifrar/decifrar, `crypt.js`). Nenhum pacote para instalar, nenhuma biblioteca de JavaScript no front‑end.

---

## Segredos e variáveis de ambiente

Nunca versionados. Na nuvem vêm dos **Secrets do repositório**; localmente, de um `.env` na raiz (ignorado pelo Git).

| Variável | Para quê |
|---|---|
| `PIPEDRIVE_API_TOKEN` | leitura do CRM |
| `PAINEL_SENHA` | senha do dashboard (chave da criptografia) |
| `DISPATCH_TOKEN` | botão "Atualizar agora" (vai dentro do conteúdo cifrado) |
| `META_ACCESS_TOKEN` | Meta Ads e Instagram |
| `KIWIFY_CLIENT_ID` / `KIWIFY_CLIENT_SECRET` / `KIWIFY_ACCOUNT_ID` | vendas MP PRO |

Opcionais: `PIPEDRIVE_FORCAR=1` (força a leitura do Pipedrive), `IG_BACKFILL`, `IG_POSTS` (ritmo do backfill do Instagram).

> Trocar a `PAINEL_SENHA` invalida o cache cifrado guardado no Actions: a execução seguinte simplesmente refaz a coleta completa.

---

## Publicação automática

[`.github/workflows/atualizar.yml`](.github/workflows/atualizar.yml) roda de hora em hora (`cron: 7 * * * *`) e sob demanda (`workflow_dispatch`, acionado pelo botão do dashboard). O workflow:

1. restaura o cache incremental (guardado **cifrado**, porque contém nomes de leads);
2. roda `python3 painel.py ci`;
3. publica `_site/` no GitHub Pages;
4. faz um commit vazio de manutenção se o repositório passar 45 dias parado — o GitHub desliga o agendamento de repositórios públicos inativos após 60 dias.

---

## Segurança e privacidade

Este repositório é **público** e os logs do Actions também são. Por isso:

- nenhum dado de lead é impresso nos logs (só contagens e status);
- `data/`, `cache/`, `_site/` e `.env` estão no `.gitignore`;
- o que vai ao ar é apenas o `index.html` com o JSON **cifrado**; o build falha se encontrar dado em texto puro no HTML;
- o painel mostra somente o nome do negócio — e‑mails, CPFs e telefones são mascarados na coleta;
- `data/dev.html` (versão sem senha, para revisão local) **nunca** pode ser publicado.

---

## Estrutura

```
painel.py                      coleta, modelagem, criptografia e build (CLI)
template.html                  o dashboard inteiro: HTML + CSS + JS vanilla, sem bibliotecas
audit.py                       implementação de referência das métricas, em Python
crypt.js / encrypt.js          AES-256-GCM com chave PBKDF2 (600k iterações) da PAINEL_SENHA
SPEC.md                        contrato: formato dos dados, métricas, layout e regras de datas
tests/test_dashboard.py        testes
assets/                        fontes Campton, logo coruja branca, favicon
fontes/                        fotografias cifradas importadas à mão (MP HUB, YouTube)
.github/workflows/atualizar.yml  atualização automática + deploy no Pages
```

---

## Para mexer no painel

1. `python painel.py collect` (ou use o `data/dashboard.json` que já estiver na máquina);
2. edite `template.html`;
3. `python painel.py dev` e abra `data/dev.html` no navegador — sem senha, dados reais;
4. rode `python -m unittest discover tests` e, se mexeu em métrica, `python audit.py expected`;
5. commit e push na `main` → o Actions republica (≈1 min).

Dois placeholders do `template.html` são obrigatórios e não podem ser renomeados: `const ENC = /*__DATA__*/null;` (produção, cifrado) e `const RAW = /*__RAW__*/null;` (revisão local, puro).

---

Uso interno da Meu Patrimônio Investimentos. Material de apoio à gestão — não constitui recomendação de investimento.
