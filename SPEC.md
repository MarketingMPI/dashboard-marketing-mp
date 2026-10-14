# Dashboard do Marketing — Meu Patrimônio (spec v2)

Substitui o antigo "Painel CRM · Campanha TRD" (visão por atividade, só TRD).
Agora: **todos os leads do B2C Funil Alpha (pipeline 45)**, qualquer origem, **unidade = LEAD**.
Publicado em https://marketingmpi.github.io/dashboard-marketing-mp/ (repo renomeado de painel-atividades-trd em 14/10/2026), protegido por senha
(dados criptografados AES-GCM; ver template antigo `../template.html` para o mecanismo de lock/decrypt — reaproveitar).

## Arquivos
- `painel.py` — coleta (já pronto). Gera `data/dashboard.json`. `python painel.py dev [saida.html]` gera
  HTML **sem criptografia** para revisão local (substitui `/*__RAW__*/null` pelo JSON).
- `template.html` — o dashboard (ARQUIVO ÚNICO: HTML+CSS+JS vanilla, **sem bibliotecas externas**, gráficos em SVG/HTML próprios).
  Placeholders obrigatórios, exatamente assim, cada um uma vez:
  - `const ENC = /*__DATA__*/null;`  (produção: JSON criptografado)
  - `const RAW = /*__RAW__*/null;`   (dev: JSON puro; se RAW != null, pular a tela de senha e renderizar direto)
  - O template NÃO pode conter a string literal `"leads"` (com aspas duplas) nem `gerado_em` antes do marcador `/*__RAW__*/`
    (o build verifica que não há dado em texto puro). Use `D.leads`, `D.gerado_em` normalmente no código.
- Assets já publicados em `site/assets/`: `CamptonBook.otf`, `CamptonMedium.otf`, `CamptonSemiBold.otf`, `CamptonBold.otf`,
  `coruja-branca.png` (logo branca transparente, usar sobre verde), `favicon.png`. Referenciar como `assets/...`.

## Dados (`data/dashboard.json`)
```
{ gerado_em: "2026-10-14T09:59:00-03:00",
  stages: [{id:345,nome:"MQL - 1º Contato"},{id:346,"Agendar"},{348,"Reunião"},{349,"Negociação"},{350,"Fechamento"},{438,"Cultivo"}],  // ordem do funil
  tipos: {call:"Ligação", whatsapp_chat1:"WhatsApp", ...},   // nome amigável do tipo de contato
  meta: {pipeline, excluidos_teste, coleta},
  leads: [ {
    id: 15775,                 // deal id -> link https://meupatrimoniob2c.pipedrive.com/deal/{id}
    n: "TRD | Fulano",         // título do negócio (nome do lead)
    s: "open"|"lost"|"won",    // status
    sg: 345,                   // etapa atual (para lost/won: etapa em que estava ao encerrar)
    c: "Renan Lima",           // consultor (dono do negócio). "Sem consultor (Marketing)" quando dono = Marketing
    o: "Tríade",               // origem derivada (Tríade, Digital, Ex-cliente, Indicação, Eventos, MFO, RCON, Curitiba, Prospecção, Inbound, TRD-INT, WhatsApp/Chat, Outros)
    lb: ["Digital","TRIADE"],  // etiquetas do Pipedrive
    e: "2026-10-13T21:40:00",  // ENTRADA REAL do lead (BRT, sem fuso) — ver "Veracidade das datas"
    ec: "2026-10-14T09:00:00", // criação do negócio no CRM (add_time)
    ef: "Criação no CRM",      // fonte da entrada: Criação no CRM | Lead recuperado do Meta (nota) | Formulário Meta (nota 'Enviado em') |
                               // Importação CSV do Meta (nota) | Entrada no Meta/AC (nota) | 1º contato registrado (anterior à criação no CRM)
    b: 0|1,                    // 1 = base reativada (ex-clientes importados / vindos da Base de Relacionamento) — só informativo; entra por padrão
    dd: [54637],               // (opcional) outros negócios da MESMA pessoa absorvidos neste lead
    w: null|"...",             // ganho em (BRT)
    l: null|"...",             // perdido em (BRT)
    lr: null|"Sem contato",    // motivo da perda
    k: [["2026-10-14T09:10:00","call"], ...],   // CONTATOS REALIZADOS (atividades de contato concluídas), ordem cronológica, BRT
    pa: 3,                     // atividades pendentes (não mostrar como métrica principal; só detalhe do lead)
    nx: "2026-10-16"|null,     // próxima atividade pendente (data)
    h: [[345,"2026-10-13T21:40:00","2026-10-14T10:00:00"],[346,"2026-10-14T10:00:00",null]]  // jornada por etapa: [etapa, entrou, saiu|null(=ainda está, ou fechou)]
  } ] }
```
Datas são strings BRT `YYYY-MM-DDTHH:MM:SS` sem fuso. Trate-as como horário local de Brasília
(parse manual: `new Date(y, m-1, d, hh, mm, ss)` é aceitável já que tudo é comparado entre si; "agora" = `D.gerado_em` (parse do mesmo jeito, ignorando o `-03:00`)). **Nunca use o relógio do navegador como "agora"** — use `D.gerado_em`, para os números baterem com a auditoria.

## Definições de métricas (CONTRATO — a auditoria em Python implementa as mesmas)
Dias = diferença em milissegundos / 86400000 (fracionário). Médias com 1 casa decimal na exibição.

**População filtrada P**: leads com data de entrada `e` (só a parte da data, YYYY-MM-DD) entre `de` e `ate` inclusive,
E consultor ∈ selecionados (vazio = todos), E origem ∈ selecionadas, E status ∈ selecionados, E etapa atual `sg` ∈ selecionadas,
E (etiquetas: tem ao menos uma das selecionadas), E busca de texto (case/acento-insensível) contida em `n`.

- `leads` = |P|
- `contatados` = leads com `k.length > 0`;  `sem_contato` = |P| − contatados;  `pct_contatados` = contatados/|P|·100
- `fim_de_semana` = leads cuja data de entrada cai em sábado ou domingo
- `dias_1o_contato` (por lead contatado) = max(0, dias(k[0][0] − e)); KPI = **média** e **mediana** sobre os contatados
- `contatos_por_lead` = média de `k.length` sobre os contatados
- `agendado` = lead que chegou à etapa Reunião ou além: existe segmento em `h` com etapa ∈ {348,349,350}, OU `s=="won"`.
  `dias_ate_agendamento` = dias(início do primeiro segmento com etapa ∈ {348,349,350} − e) (se won sem segmento: dias(w − e)); mínimo 0
- `reuniao` = lead com algum contato do tipo `meeting` ou `qualificacao` em `k`; `dias_ate_reuniao` = dias(primeiro desses − e)
- `dias_ate_perda` = dias(l − e) para `s=="lost"`;  `dias_ate_ganho` = dias(w − e) para `s=="won"`
- `contatos_ate_perda` = nº de itens de `k` com data ≤ `l`
- `dias_sem_contato` (só **abertos**) = dias(agora − último contato), ou dias(agora − e) se nunca contatado
- `parados_7d` = abertos com `dias_sem_contato` > 7
- **Mapa de etapas** (para cada etapa do funil, sobre P):
  - `atuais` = leads `s=="open"` com `sg == etapa`
  - `passaram` = leads com algum segmento em `h` naquela etapa
  - `media_dias_na_etapa` = média, sobre os leads que passaram E já saíram da etapa (segmento com fim != null), da SOMA das durações dos
    segmentos daquela etapa (fim − início) por lead. Leads cujo último segmento naquela etapa está aberto (fim null) entram em
    `media_dias_atual` = média de (agora − início do segmento aberto) sobre os `atuais`.
- **Perdidos** (sobre P ∩ s=="lost"): total, média `dias_ate_perda`, média `contatos_ate_perda`, `perdidos_sem_contato` (contatos_ate_perda==0),
  contagem por motivo `lr`, por etapa `sg`, por consultor.
- **Consultor** (agrupado por `c`, sobre P): leads, contatados, pct_contatados, media/mediana dias_1o_contato, contatos_por_lead,
  agendados, reunioes, ganhos, perdidos, abertos, abertos_sem_contato, parados_7d.

Comparação com período anterior: mesmo nº de dias imediatamente antes de `de` (mesmos demais filtros) → variação nos KPIs (▲▼ com %).

### Função de contrato (obrigatória)
Expor `window.MP_DASH = { compute(leads, filtros, agoraStr) }` retornando EXATAMENTE:
```
{ kpis: {leads, contatados, sem_contato, pct_contatados, fim_de_semana, media_dias_1o_contato, mediana_dias_1o_contato,
         contatos_por_lead, agendados, reunioes, ganhos, perdidos, abertos, parados_7d},
  etapas: [{id, atuais, passaram, media_dias_na_etapa, media_dias_atual}],          // na ordem de D.stages
  consultores: [{consultor, leads, contatados, pct_contatados, media_dias_1o_contato, mediana_dias_1o_contato,
                 contatos_por_lead, agendados, reunioes, ganhos, perdidos, abertos, abertos_sem_contato, parados_7d}],  // ordenado por leads desc, depois nome
  perdidos: {total, media_dias_ate_perda, media_contatos_ate_perda, sem_contato, motivos: {motivo: n}},
  lead_time: {media_dias_1o_contato, media_dias_ate_agendamento, media_dias_ate_reuniao, media_dias_ate_perda, media_dias_ate_ganho} }
```
`filtros = {de:"YYYY-MM-DD", ate:"YYYY-MM-DD", consultores:[], origens:[], status:[], etapas:[], etiquetas:[], busca:""}`.
Valores numéricos SEM arredondamento (arredondar só na exibição); média de conjunto vazio = `null`. Mediana de nº par = média dos dois centrais.
A UI deve usar essa mesma função (uma fonte de verdade).

## Layout / UX (BI, dinâmico, identidade MP)
Identidade: fonte **Campton** em tudo; verde `#00503c` (principal), `#003326` (profundo), `#00a611`/`#61ce70` (accent/CTA),
neutros `#1c1c1c`/`#69727d`; fundo branco/`#f4f7f5`; vermelho só para perdas/alertas (`#b23b2e`). Cantos 14px, bordas `#e3e9e6`.
Título: **Dashboard do Marketing** · subtítulo "B2C Funil Alpha · Meu Patrimônio". Logo coruja branca no header verde.
Header mostra "Dados do Pipedrive em dd/mm/aaaa hh:mm" + selo "atualização automática a cada 1 hora".

1. **Barra de filtros fixa (sticky)** no topo do conteúdo:
   - Período com presets: Hoje, Últimos 7 dias (padrão), Esta semana (seg→hoje), Semana passada, Últimos 30 dias, Este mês, Mês passado, Tudo,
     + seletor De/Até (`<input type=date>`); botões ◀ ▶ para deslocar o período pelo próprio tamanho.
   - Multi-selects (dropdown com checkboxes + contagem): Consultor, Origem, Status (Aberto/Ganho/Perdido), Etapa, Etiqueta. Busca por nome.
   - Chips dos filtros ativos com ✕ e "Limpar filtros". Estado dos filtros persistido na URL (hash) para compartilhar link.
   - Cross-filter: clicar numa barra/fatia de consultor/origem/etapa/motivo aplica aquele filtro.
2. **Abas** (navegação tipo BI, abas no topo ou sidebar à esquerda; todas respeitam os filtros):
   - **Visão Macro** — KPIs com variação vs período anterior: Leads, Com contato (+%), Sem contato, Entraram no fim de semana,
     Tempo médio até 1º contato (dias; mediana em subtítulo), Agendados, Reuniões, Ganhos, Perdidos.
     Gráficos: leads por dia de entrada (barras empilhadas contatado/sem contato, fim de semana destacado);
     distribuição do tempo até 1º contato (mesmo dia <1d, 1–2d, 2–3d, 3–7d, >7d, sem contato);
     leads por origem; leads por consultor; mini-funil Leads → Contatados → Agendados → Reuniões → Ganhos.
   - **Lead a Lead** — tabela de P com: Lead (link Pipedrive), Consultor, Origem, Etapa (pill), Status, Entrada (dd/mm hh:mm + dia da semana, badge FDS),
     1º contato (dias), Contatos realizados, Último contato, Dias sem contato, Até agendamento, Até reunião, Até perda/ganho.
     Ordenável por coluna, paginação ou scroll, exportar CSV (do que está filtrado). Clicar na linha abre **drawer lateral** do lead:
     dados, linha do tempo (entrada → contatos → etapas → perda/ganho) e jornada por etapa com dias em cada uma.
   - **Lead-time** — cards: médias até 1º contato, até agendamento, até reunião, até perda, até ganho (com n de cada);
     "régua" visual do tempo médio entre marcos; envelhecimento dos abertos por dias sem contato (0–2, 3–7, 8–14, 15–30, >30) clicável → lista;
     tabela dos abertos mais parados.
   - **Funil / Mapa do CRM** — etapa por etapa: atuais, passaram, % que passou (vs leads), tempo médio na etapa, tempo médio atual;
     visual de funil horizontal + matriz Consultor × Etapa (heatmap de leads atuais).
   - **Perdidos** — KPIs (perdidos, % do período, tempo médio até perda, contatos médios até a perda, perdidos sem nenhum contato);
     motivos (barras), perdas por etapa e por consultor; tabela lead a lead dos perdidos: lead, consultor, motivo, etapa na perda,
     entrada, perdido em, lead-time até a perda (dias), contatos até a perda — linha abre o mesmo drawer.
     Toggle local "Período pela data da perda" (em vez da entrada) — se ativo, nesta aba o recorte usa `l` em vez de `e`.
   - **Consultores** — "fluxo de eficiência": para cada consultor um funil horizontal Leads → Contatados → Agendados → Reuniões → Ganhos
     (+ perdidos), com tempo médio até 1º contato, contatos/lead, abertos sem contato e parados >7d; ranking/tabela comparativa.
     Todo lead mostrado em qualquer aba exibe seu consultor.
3. Tooltips em hover nos gráficos; animações sutis de transição; estados vazios ("Nenhum lead no período").
4. Responsivo (desktop primeiro, funcional em celular). Sem dados pessoais além do nome do negócio (sem e-mail/telefone).
5. Rodapé "Como ler este dashboard" com as definições acima em linguagem simples
   (contato = atividade de contato concluída; atividades pendentes da cadência não contam; datas em horário de Brasília; testes excluídos).


## Veracidade das datas (coleta, painel.py)
- **Entrada real ≠ criação no CRM.** Ordem de prioridade: (1) data declarada na nota do negócio — `entrou de verdade em DD/MM/AAAA às HH:MM`
  (lead recuperado), `Enviado em: DD/MM/AAAA HH:MM` (form Meta), `Entrada (Meta/form): MM/DD/AAAA h:mmam` (CSV, formato US),
  `Entrada no Meta|AC: ISO` (com fuso; sem fuso = BRT); (2) `add_time`. Se houver contato concluído ANTES dessa data, o lead já
  existia: entrada = 1º contato.
- **Data do contato** = `marked_as_done_time`, exceto atividade criada já concluída (add≈done ±120 s) com vencimento anterior
  (registro retroativo): vale `due_date`+`due_time` (UTC→BRT) ou `due_date` 12:00. Vencimento > 3 anos antes do registro = erro de
  digitação, ignora.
- **Veio de outro funil:** se o negócio chegou ao Alpha (troca de etapa de um funil de fora para uma etapa do Alpha) mais de
  1 dia depois da criação, entrada = chegada ao Alpha e os contatos anteriores (ciclo passado) saem de `k`.
- **Base reativada (`b=1`, informativo; entra por padrão desde 14/10/2026 a pedido do usuário):** (a) importação em lote — origem `Import`, ≥ 10 negócios do mesmo criador na mesma
  hora, sem data real em nota; (b) negócio trazido de volta da *B2C Base de Relacionamento*.
- **Uma pessoa = um lead POR CICLO:** negócios com mesmo e-mail/telefone (hash; 8 últimos dígitos) criados com até 14 dias de
  diferença viram um lead (canônico = aberto/ganho antes de perdido, depois o mais recente; entrada = a mais antiga; contatos =
  união; `dd` = absorvidos). Conversão nova depois disso = lead novo no próprio período, com `re` = data da entrada anterior.
- **Contatos:** cada atividade de contato CONCLUÍDA = 1 contato (sem agrupar conclusões no mesmo minuto, decisão do usuário 14/10/2026).
- Filtros extras no contrato: `filtros.incluir_base` (padrão **true** = funil inteiro; false tira `b=1`) e `filtros.so_trd`
  (bool, padrão false; true = só títulos `^\s*TRD\s*\|`, sem TRD-INT). A UI mostra o funil inteiro e oferece o seletor "Somente leads TRD".
- Conferência contra a fonte: `python data/verif/crosscheck.py` refaz pela API v2 uma amostra (semana + 20 aleatórios + duplicados)
  e compara entrada, contatos, 1º contato, status, etapa, perda/ganho e motivo.
