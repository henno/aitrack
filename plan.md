# aitrack edasine plaan

See plaan koondab senise arutelu põhjal töö, mis tuleks `aitrack`is ära teha. Fookus on sellel, et server oleks usaldusväärne raw/fact store ja et aktiivne töö oleks tuvastatav harnessi hookide, tool-call eventide ning progressi põhjal, mitte ainult protsessi elusoleku või lokaalse `active` lipu järgi.

## A. Kriitiline kontekst uuele AI-le

### Praegune seis

Repo asub siin:

```text
/home/park/aitracker
```

Põhifailid:

```text
aitrack.py          kogu CLI, server, UI ja DB loogika ühes failis
aitrack_tests.py    regressioonitestid
README.md           kasutaja dokumentatsioon
plan.md             see plaan
```

Oluline koodikaart `aitrack.py` sees:

```text
_db_init                         serveri SQLite skeem ja migratsioonid
_db_add_user / _db_set_user_password / _db_login
_db_work_start / _db_work_tick / _db_work_finish / _db_work_status
_db_ingest_rows / _db_ingest_events
_db_activity_log                 /api/activity andmete koostamine
_activity_page_html / _login_page_html
_AitrackHandler                  HTTP handler, API endpointid, auth, rate limit
_server_post / _server_get       kliendi API helperid
_local_db_init / _active_work_sessions / _local_update_work_session
_project_context / _normalise_repo_url / _issue_from_branch
cmd_work / cmd_tick / cmd_serve / cmd_user / cmd_connect
```

Uut API/DB tööd tehes lisa funktsioonid samasse serveri sektsiooni, lisa handlerisse endpoint ja lisa regressioonitest `aitrack_tests.py` lõppu uue TEST numbriga.

Server on deploytud diarainfra.com-i (ligipääs KAHE hüppega — diarainfra pole otse väljast SSH-itav):

```text
ssh root@test.diarainfra.com  ->  ssh root@diarainfra.com
/opt/aitrack                       (lihtsalt failid, EI ole git-repo)
https://aitrack.diarainfra.com
container: aitrack-server
server DB containeris: /data/server.db (püsib volume'is aitrack_aitrack-data)
avalik route: nginx (/etc/nginx/sites-enabled/aitrack.diarainfra.com.conf) -> aitrack-server konteiner
```

Brauseri login on juba olemas:

```text
https://aitrack.diarainfra.com/login
https://aitrack.diarainfra.com/activity
```

Praegu on serveri kasutaja:

```text
admin
```

Ära kirjuta paroole ega tokeneid reposse, plaani ega vestlusesse. Serveri admin-login ja -token hoitakse ainult diarainfra serveris rootile loetavates failides — küsi vajadusel serveri haldajalt, ära pane neid reposse.

CLI/API jaoks jääb token-auth alles. Brauseri jaoks kasutatakse `HttpOnly` session-cookie't. Serveris on juba lihtne rate limiter ja IP-ban tüüpiliste probe'ide ning korduvate vale-loginite jaoks.

### Praegused olulised DB tabelid

Serveris on juba olemas normaliseeritud põhiskeem:

```text
users
devices
customers
projects
project_remotes
project_checkouts
issues
work_items
work_sessions
minute_ticks
contracts
contract_rates
hour_rows
prompt_events
web_sessions
```

`work_sessions.session_uid` ehk `work_session_uid` on avalik globaalne session ID kujul `ws_...`. SQLite `work_sessions.id` on ainult sisemine DB ID.

Lokaalne agent state:

```text
~/.config/aitrack/local.db       aktiivsed local work_session'id + tulevane outbox
~/.config/aitrack/work-state.json legacy snapshot, mitte enam source of truth
```

### Praegused olulised käsud

```bash
aitrack status
aitrack start
aitrack serve --host 0.0.0.0 --port 8765 --db /data/server.db
aitrack user add admin --role admin --db /data/server.db
aitrack user password admin --db /data/server.db
aitrack connect --url https://aitrack.diarainfra.com --token TOKEN
aitrack project-id --json
aitrack work start --tool pi "short summary"
aitrack tick
aitrack work done --result kept "summary"
aitrack install --minute-tracking
```

Iga muudatuse järel käivita vähemalt:

```bash
python3 -m py_compile aitrack.py
python3 aitrack_tests.py
```

Praegune oodatud tulemus:

```text
128 läbitud, 0 ebaõnnestunud
```

Deploy workflow (lähtekood henno repos, produktsioon diarainfra.com-is):

```bash
git add ...
git commit -m "..."
git push henno <branch>        # henno = git@github.com:henno/aitrack.git (source of truth), tee PR -> main

# Deploy diarainfra serverisse. /opt/aitrack EI ole git-repo ja server ei pulli GitHubist —
# kopeeri muudetud failid läbi hüppe (image sisaldab aitrack.py, README.md, aitrack_core/):
cat aitrack.py                | ssh root@test.diarainfra.com "ssh root@diarainfra.com 'cat > /opt/aitrack/aitrack.py'"
cat aitrack_core/templates.py | ssh root@test.diarainfra.com "ssh root@diarainfra.com 'cat > /opt/aitrack/aitrack_core/templates.py'"
ssh root@test.diarainfra.com "ssh root@diarainfra.com 'cd /opt/aitrack && docker compose up -d --build'"
```

Andmed püsivad volume'is (`aitrack_aitrack-data`), rebuild neid ei puuduta. Kontroll: `curl https://aitrack.diarainfra.com/login` (200) + mõni muudetud/uus endpoint.

### Mida mitte valesti mõista

- `minute_ticks` ei tõesta iseenesest, et agent päriselt töötas; see tõestab ainult, et aktiivse local sessioni kohta saadeti heartbeat.
- Protsessi elusolek ei võrdu töö tegemisega. Vaja on progress-evente.
- Suur tickide auk, nt laptop sleep, ei ole pidev töö. Tihendamisel tuleb teha eraldi active intervalid.
- System prompt ei ole sobiv koht kohustusliku heartbeat'i tegemiseks. Kasuta harness hooke/extensions.
- `before_tool_call` heartbeat peab toimuma enne tööriista käivitamist. Kui `after_tool_call` ei tule, näeme, millise tooli juures agent hangus.
- Server ei peaks lõpuks olema invoice generator; server peab hoidma raw/fact andmeid ja pakkuma export endpointid.
- Ära murra olemasolevat local visualizerit (`aitrack start`) ega Google Sheets HTML copy flow'd (`aitrack day --html`).
- Ära pane repo failidesse tegelikke serveri paroole/tokeneid.

### Soovitus uuele AI-le alustamiseks

Ära ürita kogu plaani korraga teha. Alusta väikesest kontrollitavast sammust:

1. Loe `aitrack.py` serveri DB/API osa ja `aitrack_tests.py` viimased testid.
2. Tee Phase 1 MVP: `raw_events` tabel + `POST /api/events` + `GET /api/export/raw-events` + testid.
3. Alles pärast seda tee Pi extension või hook adapter.
4. Pärast iga faasi testi, commiti ja deploy ainult siis, kui kasutaja soovib või deploy on ülesande osa.

## 0. Põhimõtted

- Server salvestab normaliseeritud faktid ja raw/event-andmed; arve/praktikaaruande renderdamine toimub eraldi kihis või export endpointide põhjal.
- `work_session_uid` (`ws_...`) on kanooniline avalik sessiooni ID. SQLite integer ID jääb sisemiseks viiteks.
- Sama projekt tuvastatakse normaliseeritud Git remote URL-i põhjal; Gitita fallback on `local:<kaustanimi>`.
- Sama projekt + sama issue koonduvad ühise `work_item` alla, aga igal kasutajal/arvutil/checkoutil/agentil on oma `work_session`.
- Aktiivseks tööks ei tohi lugeda pelgalt seda, et protsess on elus või `local.db` järgi sessioon aktiivne.
- Aktiivsust tuleb tõendada progressiga: prompt lifecycle, tool-call lifecycle, token/output/tool events, heartbeat.
- Saladusi, API tokeneid ja paroole ei tohi logida, plaani kirjutada ega UI-s nähtavaks teha.

## 1. Raw event storage

Lisada serverisse append-only sündmuste tabel.

Soovituslik tabel:

```text
raw_events
- id INTEGER PRIMARY KEY
- user_id INTEGER REFERENCES users(id)
- work_session_id INTEGER REFERENCES work_sessions(id)
- work_session_uid TEXT
- agent_uid TEXT
- parent_agent_uid TEXT
- event_type TEXT
- tool_name TEXT
- occurred_at_utc TEXT
- received_at_utc TEXT
- payload_json TEXT
```

Esialgsed event tüübid:

```text
prompt_started
prompt_finished
agent_started
agent_heartbeat
agent_finished
before_tool_call
after_tool_call
tool_output
tool_error
subagent_started
subagent_heartbeat
subagent_finished
subagent_stuck
session_orphaned
session_stale
```

Nõuded:

- append-only; ära uuenda vanu ridu, välja arvatud vajadusel eraldi derived/rollup tabelites;
- payload JSON peab olema piiratud suurusega;
- ära salvesta täistekstilisi saladusi ega suuri tool outpute;
- lisa indeksid `work_session_id`, `work_session_uid`, `occurred_at_utc`, `event_type`, `agent_uid`.

## 2. Event API-d

Lisada endpointid:

```text
POST /api/events
POST /api/prompt/start
POST /api/prompt/done
POST /api/agent/heartbeat
POST /api/agent/tool-start
POST /api/agent/tool-end
GET  /api/export/raw-events
GET  /api/export/work-sessions
GET  /api/export/activity
```

`POST /api/events` võib olla üldine batch endpoint, mida harness adapterid kasutavad.

Nõuded:

- token auth CLI/adapterite jaoks;
- cookie auth brauseri jaoks;
- idempotentsus `event_key` või `(agent_uid,event_type,occurred_at_utc,tool_call_id)` põhjal;
- serveri `received_at_utc` lisatakse alati serveris;
- kõik eventid seotakse võimalusel `work_session_uid`-ga.

## 3. Prompt contexti parandamine

Praegu prompt-eventid ei pruugi alati kanda piisavat konteksti. Tuleb saata kliendi poolt:

```text
project_key
repo_url
checkout_id
local_path
branch
issue_key
work_session_uid
agent_uid
parent_agent_uid
tool/harness
```

Eesmärk:

- server ei pea tuletama `local:<folder>` serveris nähtamatust local pathist;
- promptid ja tool-callid seotakse õige `work_session_uid` külge;
- sama projekti eri checkoutid seotakse korrektselt ühise project/work_item mudeliga.

## 4. Harness hook adapterid

### 4.1 Pi extension

Luua globaalne Pi extension näiteks:

```text
~/.pi/agent/extensions/aitrack.ts
```

Pi hook mapping:

```text
before_agent_start       -> prompt_started + vajadusel work start + tick
agent_start              -> agent_started
turn_start / turn_end    -> turn progress eventid
tool_call                -> before_tool_call + heartbeat
tool_result              -> after_tool_call + progress
tool_execution_end       -> tool_finished detailid
agent_end                -> prompt_finished / agent_finished / final tick
session_shutdown         -> cleanup / possible orphan mark
```

Oluline reegel:

```text
before_tool_call -> heartbeat/progress event -> alles siis tool-call
```

See on usaldusväärsem kui süsteemi promptis mudelile käskimine.

### 4.2 Claude Code hooks

Claude jaoks kasutada hooke, kui saadaval:

```text
UserPromptSubmit -> prompt_started / work start / tick
PreToolUse       -> before_tool_call heartbeat
PostToolUse      -> after_tool_call progress
Stop             -> prompt_finished / final tick
```

Lisada paigaldusjuhis ja test, et hookid käivituvad.

### 4.3 OpenCode / Codex / Gemini

Uurida iga harnessi puhul:

- kas on native hooks;
- kas on JSON/RPC mode, mille ümber saab adapteri teha;
- kas tuleb kasutada wrapperit;
- kas fallback on logifaili tailimine.

Eesmärk on sama event vocabulary kõigi harnessite jaoks.

## 5. Prompti alguse töövoog

Senine käsitsi põhimõte jääb:

```bash
aitrack work start --tool TOOL "short summary of the user's prompt" && aitrack tick && <perform the user's prompt/task>
```

Aga ideaalis teeb selle harness hook automaatselt:

```text
user prompt submitted
-> hook kutsub aitrack prompt start/work start
-> saadab esimese heartbeat/tick
-> agent alustab päris tööga
```

Reeglid:

- kui sama task/projekt/issue/tool session on juba aktiivne, ära loo duplikaati;
- follow-up prompt samal teemal teeb tick/progress eventid;
- teema/projekti/issue vahetusel tee `work switch` või lõpeta eelmine session;
- prompt summary ei tohi sisaldada saladusi.

## 6. Owner process ja agent identity

Lisada lokaalsesse agent DB-sse ja serveri eventidesse owner-info:

```text
owner_pid
owner_start_time
owner_command
owner_cli
agent_uid
parent_agent_uid
work_session_uid
```

Miks:

- PID üksi ei piisa PID reuse tõttu;
- protsess võib olla elus, aga idle või hangunud;
- alam-agentid vajavad parent-child seost.

Lokaalne `aitrack tick` peab enne heartbeatit kontrollima:

- kas owner PID on elus;
- kas owner start time klapib;
- kas command/process tree on endiselt sama harness;
- kas session pole stale/orphan.

Kui owner puudub või ei klapi:

```text
status = orphan/stale
heartbeatit ei saadeta
```

## 7. Alam-agentid ja stuck detection

Kui harness kutsub välja alam-agendi, tuleb ta registreerida:

```text
subagent_started
- parent_agent_uid
- agent_uid
- owner_pid
- owner_start_time
- owner_command
```

Alam-agent peab enne iga tool-call'i saatma:

```text
before_tool_call heartbeat/progress
```

Ja pärast tool-call'i:

```text
after_tool_call progress/result
```

Server/lokaalne watchdog jälgib:

```text
last_heartbeat_at
last_progress_at
last_tool_event_at
last_output_at
current_tool_name
current_tool_started_at
```

Staatused:

```text
active
tool_running
idle_suspect
stuck
orphan
done
discarded
```

Soovituslikud algsed timeoutid:

```text
no heartbeat > 2 min       -> stale
no progress > 5 min        -> idle_suspect
tool_running > 10 min      -> stuck
owner pid gone             -> orphan
```

Oluline: `last_progress_at` on olulisem kui `pid_is_alive`.

## 8. Watchdog

Lisada watchdog kas lokaalse käsuna või serveripoolse perioodilise tööna:

```bash
aitrack watchdog
```

Vastutus:

- tuvastab stale/orphan/stuck sessioonid;
- ei saada ticke sessioonidele, mis ei tee progressi;
- märgib serverisse diagnostic eventid;
- UI-s tekivad hoiatused.

Võimalikud eventid:

```text
session_marked_stale
session_marked_orphan
agent_marked_stuck
agent_recovered
```

## 9. Minute ticks, intervallid ja sleep-gapid

Praegune `minute_ticks` mudel sobib detailandmeteks, kuid raportite jaoks tuleb teha rollup.

Lisada derived tabel:

```text
work_session_active_intervals
- id
- work_session_id
- start_minute_utc
- end_minute_utc
- minutes
- source
- created_at
```

Konsolideerimise reegel:

- järjestikused tickid tihendatakse intervalliks;
- kui tickide vahe on suurem kui threshold, algab uus intervall;
- näiteks threshold 2-3 min.

Näide sleepi kohta:

```text
22:13-22:47 tickid järjest
22:48-09:59 läpakas sleep / ticke pole
10:00 järgmine tick
```

Õige tõlgendus:

```text
active interval 1: 22:13-22:48
auk/sleep: ei loeta tööks
active interval 2: 10:00-...
```

Mitte:

```text
22:13-10:00 pidev töö
```

Sessiooni lõpetamisel:

```text
work done
-> arvuta minutes_final
-> ehita active_intervals
-> märgi rollup_finalized_at
```

## 10. Retention ja kettamaht

Detailandmed võivad kasvada. Lahendus ei ole kohe kõik üheks reaks kustutada, vaid mitu taset:

```text
raw_events                         lühike/keskmine retention
minute_ticks                       keskmine retention
work_session_active_intervals      pikaajaline fakt
work_sessions.minutes_final        pikaajaline kokkuvõte
```

Soovituslik algne retention:

```text
raw_events: 90-180 päeva
minute_ticks: 90 päeva pärast rollupit
active_intervals: alaliselt
work_sessions: alaliselt
```

Cleanup job:

```text
aitrack cleanup --older-than 90d --after-rollup
```

Kõige suurem kettarisk on pigem:

```text
raw_events.payload_json
prompt_text
tool outputid
```

Seetõttu:

- payload size limit;
- tool output truncation;
- optional hash-only mode tundlike andmete jaoks.

## 11. Export endpointid ja invoice endpointi deprecate

Server peaks olema faktide hoidla, mitte invoice generator.

Lisada:

```text
GET /api/export/work-sessions
GET /api/export/activity
GET /api/export/raw-events
GET /api/export/active-intervals
```

Deprecate või asendada:

```text
GET /api/billing/invoice-lines
```

Põhjus:

- invoice endpoint sisaldab `hourly_rate`, `amount`, invoice-spetsiifilist grupeerimist;
- raport/arve peaks tekkima välises kihis raw/export andmete põhjal;
- perioodipiiride lõikamine peab kasutama intervalle või ticke, mitte ainult session start/end.

## 12. UI / activity vaade

Täiendada `/activity`:

Filtrid:

```text
kuupäev/periood
kasutaja
projekt
issue
tool/harness
status
agent_uid
```

Näidata:

```text
work sessions
prompt events
raw events timeline
agent/subagent tree
last_progress_at
current_tool_name
current_tool_duration
stuck/orphan/stale hoiatused
minute/intervalli kokkuvõte
```

Admin näeb kõiki, tavakasutaja ainult enda andmeid.

## 13. Login, admin ja turvalisus

Olemas:

- brauseri login;
- `HttpOnly` session-cookie;
- admin kasutaja;
- rate limiter;
- IP-ban kahtlaste päringute ja korduvate vale-loginite puhul;
- CLI/API token auth jääb alles.

Edasised turvatööd:

```text
security_events tabel
login audit
failed login audit
session revocation
admin UI kasutajate haldamiseks
parooli vahetamise flow
rate limit konfiguratsioon env/config kaudu
```

Mitte panna repo/plaani sisse tegelikke paroole ega tokeneid.

## 14. Timezone ja perioodipiirid

Praegu on osa activity/report loogikat UTC-põhine. Lisada:

```text
server timezone config
user timezone config
Europe/Tallinn vaikimisi sellele installile
```

Perioodiraportid peavad:

- lõikama intervalle perioodipiiride järgi;
- mitte topeltlugema sessioone, mis ületavad päeva/kuu piiri;
- näitama UI-s lokaalaega, aga säilitama DB-s UTC.

## 15. Customer/contract/rate workflow

Tabelid on olemas, aga workflow puudub.

Lisada hiljem:

```text
aitrack customer add/list
aitrack project assign-customer
aitrack contract add/list
aitrack rate add/list
```

Kuid arvestada, et invoice generation ise jääb aitrackist välja või eraldi moodulisse.

## 16. Local DB ja offline/outbox

Lokaalne agent DB peab jääma allikaks aktiivsete sessioonide jaoks:

```text
~/.config/aitrack/local.db
```

Lisada/viimistleda:

- event outbox offline režiimiks;
- retry/backoff;
- dedup key igale eventile;
- failed upload diagnostika;
- `work-state.json` jääb ainult legacy snapshotiks.

## 17. Google Sheets ja local visualizer

Säilitada olemasolev lokaalne visualizer:

```bash
aitrack start
```

Nõuded jäävad:

- mineviku päevade vaatamine;
- ridade muutmine/lisamine/kustutamine;
- delete confirmation;
- adaptive textareas;
- Google Sheets HTML clipboard:

```bash
aitrack day YYYY-MM-DD --html | wl-copy -t text/html
```

Ära murra `--html`, sest see säilitab nummerdatud punktid lahtris eri ridadel.

## 18. Testid ja kvaliteet

Iga koodimuutuse järel:

```bash
python3 -m py_compile aitrack.py
python3 aitrack_tests.py
```

Praegune oodatud tulemus:

```text
128 läbitud, 0 ebaõnnestunud
```

Lisada testid:

- raw_events insert/idempotentsus;
- `/api/events` auth;
- Pi extension event payload contract;
- tool-call heartbeat enne tool-call'i;
- `agent_end` lõpetab prompti;
- subagent stuck detection;
- sleep-gap rollup;
- interval clipping üle päeva/kuu piiri;
- retention cleanup ei kustuta enne rollupit;
- rate limiter / IP-ban regressioonid;
- login cookie flow.

## 19. Rakendamise järjekord

### Phase 1: raw events MVP

- `raw_events` tabel;
- `/api/events` endpoint;
- export endpoint `GET /api/export/raw-events`;
- testid.

### Phase 2: Pi hook adapter

- globaalne Pi extension;
- `before_agent_start`, `tool_call`, `tool_result`, `agent_end`;
- eventid serverisse;
- UI-s raw timeline.

### Phase 3: work_session sidumine promptidega

- prompt start loob/leiab work_sessioni;
- prompt eventid kannavad `work_session_uid`;
- active session duplicate vältimine;
- follow-up prompt tick/progress.

### Phase 4: owner/subagent model

- `agent_uid`, `parent_agent_uid`, owner PID/start/command;
- lokaalne alive-check;
- subagent eventid.

### Phase 5: watchdog ja stuck/orphan status

- `aitrack watchdog`;
- timeout reeglid;
- stuck/orphan eventid;
- UI hoiatused.

### Phase 6: intervallid ja rollup

- `work_session_active_intervals`;
- sleep-gap handling;
- `minutes_final` lõplik arvutus;
- cleanup/retention.

### Phase 7: Claude ja teised harnessid

- Claude hooks;
- OpenCode/Codex/Gemini adapterite uurimine;
- wrapper/log-tail fallback.

### Phase 8: export/report cleanup

- raw/export endpointid;
- invoice endpoint deprecate;
- timezone/perioodipiiride korrektne lõikamine.

## 20. Edukriteeriumid

Süsteem on järgmises etapis hea, kui saame UI/API kaudu öelda:

- milline prompt algatas töö;
- milline harness/agent töötas;
- millal iga tool-call algas ja lõppes;
- millal viimane päris progress toimus;
- kas alam-agent jäi stuck;
- millised minutid olid päriselt aktiivsed;
- millised intervallid läksid millisele päevale/perioodile;
- miks mõni session ei saanud minuteid juurde;
- kuidas andmed eksporditakse ilma invoice-loogikat serverisse lukustamata.
