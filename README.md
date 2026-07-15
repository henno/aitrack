# aitrack — AI-tööriistade tunnipõhine tööpäevik

Vaatab iga tund läbi sinu **Claude Code**, **Codex**, **Antigravity** (agy), **Pi** ja **OpenCode** sessioonid,
filtreerib **ainult sinu valitud projektid** ja kirjutab **Google Sheetsi** ühe rea iga
(tund × projekt) kohta — lühikese eestikeelse kokkuvõttega tehtud tööst.

**Töötab macOS / Windows / Linux peal.** Jagatav: anna kaust edasi, iga inimene seadistab
oma Google Sheeti ja jälgitavad projektid ise.

## Nõuded
- **Python 3.9+** (muud sõltuvused puuduvad — ainult standardteek)
- Vähemalt üks AI-CLI kokkuvõtete tegemiseks: **`claude`**, **`codex`** või **`gemini`**
  (automaattuvastus selles järjekorras; saab configis fikseerida)
  > Logide lugemiseks toetab aitrack Claude/Codex/Antigravity/Pi/OpenCode sessioone.
- Väljund: **lokaalne CSV-fail** (vaikimisi — `~/aitrack-log.csv`, kohe, ilma Google'ita)
  VÕI **Google'i konto** (jagatav Sheet). Vaikeväljund töötab ilma seadistuseta.

Logide asukohad on kõigil OS-idel samad (`~/.claude`, `~/.codex`, `~/.gemini`);
Windowsis vastab `~` kaustale `C:\Users\<nimi>`.

## Kuidas see töötab

```
OS-i tunniajasti (iga tund):  Linux→systemd · macOS→launchd · Windows→Task Scheduler
        │
        ▼
  aitrack run
   ├─ võtab luku (väldib paralleelseid käivitusi)
   ├─ loeb state-failist viimati töödeldud tunni → jätkab katkenud kohast
   ├─ parsib tööriistade lokaalsed logid (Claude/Codex/Antigravity/Pi/OpenCode)
   ├─ FILTREERIB ainult lubatud projektid (projects.json)
   ├─ grupeerib LÕPETATUD tunnid (vaikimisi kõik kaustad koos) — arvestus UTC-s (DST-kindel)
   ├─ iga tunni kohta → AI-CLI jagab 4 välja: Objekt/Saavutused/Takistused/Uued teadmised
   ├─ lisab tunnipunkti algandmestikku (dedup-võtmega → kordus ei dubleeri)
   └─ renderdab päevavaate (üks rida päevas, punktid nummerdatult) → ~/aitrack-log.csv
```

Töökindlus (kontrollitud automaattestidega — `python3 aitrack_tests.py`):
- **Iga tund täpselt korra** — pool-avatud vahemik `[viimane, praegune)`, ei vahesid ega duplikaate.
- **Idempotentne** — iga real on UTC-tunnist tuletatud deterministlik võti; katkestus/kordussaatmine ega ajavööndi-muutus ei dubleeri ridu.
- **Järelejõudmine** — kui arvuti oli kinni, töödeldakse vahepealsed tunnid järele (max 48h/käivitus, ülejäänu järgmisel korral).
- **DST-kindel** — kogu arvestus UTC-s, kohalik aeg ainult kuvasiltide jaoks.
- **Lukk + atomaarne state/config** — paralleelsed käivitused serialiseeruvad, rikutud state katkestab (ei nulli).
- **Ainult valitud projektid**, ainult aktiivsed tunnid (tühje ridu ei teki, LLM-i ei kutsuta asjata).

## Paigaldus (lihtne tee)

**macOS / Linux:**
```bash
cd aitrack
bash install.sh
```
**Windows (PowerShell):**
```powershell
cd aitrack
powershell -ExecutionPolicy Bypass -File install.ps1
```

See loob lühikese **`aitrack`** käsu ja käivitab **seadistusnõustaja** (`aitrack setup`), mis teeb
kõik ühe vooluga:
1. **Vali väljund:** Lokaalne CSV-fail (kohe, ilma Google'ita) **või** Google Sheets.
   - Sheetsi valikul **genereerib ise tokeni** ja annab valmis `apps-script-ready.gs` faili (token juba
     sees) — ava Google Sheet → Laiendused → Apps Script → kleebi → Deploy (Web app, "Me" / "Anyone") →
     kleebi URL tagasi.
2. **Soovitab projekte** sinu logidest (valid linnukestega — ei pea teid trükkima).
3. Seadistab tunniajasti, valikuliselt **päevase kokkuvõtte-teavituse**, ja teeb testi.

Kui kasutad **keskserverit** (mitme kasutaja vaade), ava veebis `Kasutaja → Installi aitrack arvutisse`.
Leht loob ühekordse 15-minutilise installikoodi ja annab ühe käsu Linuxile, macOS-ile või Windowsile.
See käsk kloonib GitHubi repo, ühendab kliendi serveriga ning paigaldab minute-trackingu ja Pi extensioni.

Käsitsi sama töövoog on:

```bash
aitrack connect --url https://aitrack.example.com --token TOKEN
aitrack add /tee/projektini
aitrack install --minute-tracking --pi-extension
```

Pi sees tee seejärel `/reload` või ava uus Pi sessioon. `aitrack add` on oluline ja käib ainult päris
terminalist: globaalne Pi extension, `aitrack work start`, `aitrack tick` ja serveri vastuvõtt lubavad vaikimisi
ainult lisatud projekte. Nii ei logita kogemata mõnda muud arvutis avatud repo't ning AI agent ei saa uut projekti
ise allowlisti lisada.

> Juba seadistatud? Kõik on edaspidi käsuga **`aitrack <...>`** (lühivorm; ka `python3 aitrack.py <...>` töötab).

## Käsud

| Käsk | Tähendus |
|---|---|
| `aitrack help` / `aitrack --help` | Näita praktilist abi ja sinu OS-iga sobivaid kopeerimiskäske Google Sheetsi jaoks. |
| `aitrack start` | Ava lokaalne brauseri-UI, kus saad tänaseid ja varasemaid päevi vaadata, muuta, ridu lisada ning ühe nupuga Sheetsi kopeerida. |
| `aitrack serve` | Käivita keskserver SQLite andmebaasiga mitme kasutaja jaoks. |
| `aitrack user add/list/password` | Lisa/listi keskserveri kasutajaid, API token'eid ja brauseri login'i paroole. |
| `aitrack connect --url ... --token ...` | Ühenda klient keskserveriga; `aitrack run` saadab tunniread ja prompt-eventid serverisse. |
| `aitrack project-id` | Näita serveri ühist projektivõtit: Git repo korral normaliseeritud remote URL, muidu `local:<kaust>`. |
| `aitrack work start/done/status/switch` | Serveripõhine work-session ajamõõtmine ühe projekti/issue all; töö algus õnnestub ainult `aitrack add` projektis. |
| `aitrack tick` | Saada allowlistis olevate aktiivsete work-session'ite jooksva minuti heartbeat serverisse. |
| `https://SERVER/activity` | Serveri veebivaade work session'ite, prompt-eventide ja tegevuste vaatamiseks. |
| `aitrack install --minute-tracking` | Lisa tavapärase tunniajasti kõrvale OS-i iga-minuti tick timer aktiivsete work-session'ite heartbeat'iks. |
| `aitrack install --pi-extension` | Paigalda globaalne Pi extension, mis saadab prompt/tool-call raw evente ja lühikokkuvõtteid. Logib ainult `aitrack add` projektides. |
| `aitrack watchdog --stale-minutes N --stuck-minutes N` | Märgi progressita või liiga kaua tooli sees olevad sessioonid `stale`/`stuck` olekusse. |
| `aitrack cleanup --older-than 90d [--apply]` | Retention cleanup: raw eventid ja rollupitud vanad minute tickid; vaikimisi dry-run. |
| `aitrack events status/flush` | Lokaalse offline raw-event outboxi olek ja uuestisaatmine. |
| `aitrack report month --period YYYY-MM --project-key ... --rate 82` | Ekspordi serverist issue-põhine kuuraport minutite, tehtud töö ja tõendusviidetega. |
| `aitrack customer/contract/rate ...` | Kliendi, lepingu ja tunnihinna baastöövoog serveri DB jaoks. |
| `aitrack project assign-customer PROJECT_KEY CUSTOMER` | Seo olemasolev projekt kliendiga. |
| `aitrack setup` | **Interaktiivne seadistus algusest lõpuni** (soovitatav). |
| `aitrack suggest [--days N]` | Näita logidest aktiivseid projektikaustu (pingerida). |
| `aitrack add/remove <tee>` | Lisa/eemalda jälgitav projekt; `add` peab tulema päris terminalist, mitte AI agendi käsust; serverirežiimis sünkroniseerib ka serveripoolse allowlisti. |
| `aitrack note "<tekst>"` | **Lisa käsitsi-märge praegusele tunnile** (nt õpitu, koosolek). Läheb "Uued teadmised" veergu. Tühjalt = kuva märkmed. |
| `aitrack day [KUUPÄEV]` | **Prindi päeva sisuveerud D–G** (Objekt/Saavutused/Takistused/Uued teadmised, tab-eraldus) — vali Sheetsis lahter `D<rida>` ja Ctrl+V. Vaikimisi viimane päev; `--all` = kõik; `--header` = päiserida; `--full` = kõik 7 veergu; `--html` = clipboardi jaoks, säilitab punktid lahtris eri ridadel; `--flat` = üks füüsiline rida. |
| `aitrack day-summary [KUUPÄEV]` | Keskserveri päevavaate ridade põhjal loob lokaalne AI-harness loetava tervikkokkuvõtte ja salvestab selle veebis vastava päeva ridade kohale kopeeritavasse välja. |
| `aitrack digest [--days N] [--notify]` | Päeva/nädala kokkuvõte (valikuliselt töölaua-teavitus). |
| `aitrack status` | Näita platvormi, mootorit, väljundit ja logiallikaid. |
| `aitrack preview --hours N` | Kuiv vaade — mida kirjutataks, väljundisse saatmata. |
| `aitrack backfill --hours N` | Töötle viimased N tundi tagasiulatuvalt (ei nihuta watermarki). |
| `aitrack test-sink` | Saada testrida väljundisse. |
| `aitrack list` | Näita jälgitavaid projekte. |
| `aitrack init` / `install` / `uninstall` | Käsitsi: config / ajasti seadistus / eemaldus. |

## Tööpäeva näide

**Põhimõte:** sina lihtsalt töötad nagu tavaliselt. Tööriist jookseb taustal iga tund (:05)
ja lisab päevaritta ühe nummerdatud punkti tunni kohta. Ainus reegel — **käivita AI projektikaustast**:

```bash
cd ~/kalaradar-mono && claude     # ✓ töö läheb 'kalaradar-mono' alla
cd ~ && claude                    # ✗ kodukaustast → ei eristu projektiks
```
(Alamkaustad sobivad: `cd ~/kalaradar-mono/apps/pwa` loetakse ikka samasse projekti.)

| Kell | Mida SINA teed | Mida TÖÖRIIST teeb |
|---|---|---|
| 09:10 | `cd ~/kalaradar-mono && claude` → tood norra i18n, parandad locale-bugi | — |
| 10:05 | (kohv) | ⚙️ Võtab kokku **09:00–10:00** → kirjutab rea |
| 10:20 | Jätkad; lülitud `codex`ile API-vea jaoks | — |
| 11:05 | — | ⚙️ Kokkuvõte **10:00–11:00** (Claude + Codex) |
| 11–13 | Lõuna + koosolek (AI-d ei kasuta) | timer käib, aga **tühje ridu ei teki** |
| 13:15 | `cd ~/parkproduction && claude` → müügipakkumine, PageSpeed | — |
| 18:00 | Pakid asju | 🔔 **Päeva-digest:** *"AI-töö 1p: 4 aktiivset tundi, 47 prompti — kalaradar-mono 2h, parkproduction 2h"* |

**Väljund on üks rida PÄEVA kohta** (praktikapäeviku vorm). Iga tund lisab sellesse päevaritta
ühe **nummerdatud punkti** ja numbrid on kõigis neljas sisuveerus kohakuti. Päeviku sõnastus tehakse
kliendi arvutis iga tunni AI-vestlusest: kasutaja küsimustest ja võimalusel AI vastustest. Serverisse
saadetakse valmis tunnirida, mitte kogu transkript. Minuti heartbeat'id ja raw-eventid on ainult aja/progressi
tõenduseks. LLM jagab iga tunni neli välja: *Objekt ja ülesanne / Saavutused / Takistused / Uued teadmised*.

| Kuupäev | Punkte | Nädalapäev | Objekt ja ülesanne | Saavutused | Takistused | Uued teadmised |
|---|---|---|---|---|---|---|
| 2026-06-17 | 3 | K | 1. …<br>2. …<br>3. … | 1. …<br>2. …<br>3. … | 1. Ei olnud<br>… | 1. …<br>… |

**Kleepimine Google Sheetsi:** vaata alati enda OS-i käsku: `aitrack help`.
Linux/Waylandis on tavaliselt `aitrack day --html | wl-copy -t text/html`; Linux/X11-s
`aitrack day --html | xclip -selection clipboard -t text/html`; macOS-is `aitrack day | pbcopy`;
Windows PowerShellis `aitrack day | Set-Clipboard`. Käsk prindib viimase päeva **sisuveerud D–G**;
vali lehel lahter `D<rida>` ja Ctrl+V. A/B/C = Kuupäev/Punkte/Nädalapäev täidad ise;
`--full` annab ka need. Kogu ajalugu on failis `~/aitrack-log.csv` (renderdatakse iga tund ümber).

**Visuaalne vaade/editor:** `aitrack start` avab lokaalse lehe `http://127.0.0.1:8765`, kus saad
valida ka varasema kuupäeva, muuta olemasolevaid ridu, lisada käsitsi ridu ning kopeerida D–G või A–G
Google Sheetsi jaoks. Kõik muudatused salvestatakse `~/.config/aitrack/hours.csv` algandmestikku ja
päevavaade renderdatakse uuesti.

**Mitme kasutaja server:** `aitrack serve --host 0.0.0.0 --port 8765 --db /data/server.db` käivitab
SQLite-põhise keskserveri. Serveris loo administraator `aitrack user add admin --role admin --db /data/server.db` ja sea
brauseri login'i parool `aitrack user password admin --db /data/server.db` (või turvalises skriptis
`--password-stdin`). Kliendis seadista API jaoks `aitrack connect --url https://aitrack.example.com --token TOKEN`.
Seejärel saadab kliendi `aitrack run` tunniread ja minuti täpsusega prompt-eventid serverisse. Dockeris kasuta
repo juures `docker compose up -d --build` (vaikimisi seob `127.0.0.1:3103`).

**Normaliseeritud work tracking:** serveri DB hoiab projektid/issue'd/sessioonid/minutitickid 3NF-laadselt
eraldi tabelites (`projects`, `project_remotes`, `issues`, `work_items`, `work_sessions`, `minute_ticks`).
Git repo korral on ühine `project_key` normaliseeritud remote URL, nt
`git@github.com:Puhastusproff/pp-finar.git` ja `https://github.com/Puhastusproff/pp-finar.git` →
`github.com/puhastusproff/pp-finar`; Gitita kaustal kasutatakse `local:<kaustanimi>`.

Näide sama issue paralleelseks lahendamiseks eri agentidega/checkout'ides. `work start` vastuseks annab
server avaliku unikaalse `work_session_uid` kujul `ws_...`; klient salvestab selle lokaalse agendi
SQLite DB-sse `~/.config/aitrack/local.db` ja kasutab seda edaspidi `tick`/`done` sidumiseks.

```bash
cd ~/agents/pp-finar-pi
aitrack work start --issue 662 --tool pi "asendaja pühadetasu vea parandamine"

cd ~/agents/pp-finar-claude
aitrack work start --issue 662 --tool claude "alternatiivne lahendus Claude'iga"

# automaatselt: aitrack install --minute-tracking
# käsitsi/cron/systemd/launchd/Task Scheduler võib kutsuda iga minut:
aitrack tick

aitrack work done --result kept "Claude lahendus sobis, testid läbivad"
```

Serveri päevavaates (`https://aitrack.example.com/`) saab lisaks tunniridadele näha päeva tervikkokkuvõtet.
See tekst ei teki serveris ise: käivita oma ühendatud kliendis `aitrack day-summary YYYY-MM-DD` ning lokaalne
AI-harness küsib serverist selle päeva read, kirjutab mitte-tehnilisele lugejale sobiva kokkuvõtte ja salvestab selle
vastava päeva ridade kohale. Välja kõrval on `Kopeeri` nupp. Admin saab koostada teise kasutaja päeva kohta:
`aitrack day-summary YYYY-MM-DD --user kasutajanimi`.

Serveri tegevuste veebivaade on `https://aitrack.example.com/activity`. Brauser suunatakse vajadusel
`/login` lehele; pärast kasutajanime/parooliga sisselogimist hoiab server `HttpOnly` session-cookie't.
Tavakasutaja näeb enda work session'eid, prompt-evente ja tegevuste ajalugu; admin näeb kõiki kasutajaid.
Activity vaates saab filtreerida projekti, kasutaja, issue, tööriista, agent'i ja staatuse järgi ning
kuupäevakalendris valida ühe päeva või vahemiku (1. klikk algus, 2. klikk lõpp). Koondajalugu kuvab
sama `event_key` prompt-kirje ühe korra ja ühendab Pi sama tööetapi `agent_finished` + `prompt_finished`
üheks visuaalseks lõpetamiseks; täielik auditijälg jääb eraldi Raw eventide tabelisse. Lifecycle-rida kasutab
enda sündmuse kirjeldust, mitte sessioni hiljem muutunud lõppkokkuvõtet, ning lähestikku tekkinud sündmustel
näidatakse eristamiseks millisekundeid. Ülemised „Projekti kestus” kaardid näitavad iga projekti eraldi ning
ühendavad selle kattuvad agendi- ja work-session'i intervallid, mistõttu paralleelsed agendid ei korruta tegelikku projektile kulunud aega;
work-session'i tabeli „Agendi min” jääb eraldi auditinfoks. Detailmodaal eraldab indekseeritava DB-kirje parsitud
payloadist, et sama JSON ei oleks escaped stringina ja korduvate väljadena kaks korda näha. Staatused:
`active` = hiljutise progressiga töö, `stale` = üle 10 minuti progressita töö, `stuck` = üle 10 minuti
pooleliolev tool-call.

Admini kasutajavaade on `https://aitrack.example.com/admin`: seal saab kasutajaid lisada, paroole seada,
web-sessioone tühistada ja turvaauditit vaadata. Loginid, ebaõnnestunud loginid, paroolimuudatused,
admin-toimingud, rate-limit ban'id ja kahtlased probe'id lähevad `security_events` tabelisse.
API/CLI jaoks jääb token-põhine autentimine alles. Server rakendab lihtsat mälupõhist rate limiterit;
liigsed päringud, korduvad valed login'id ja tüüpilised probe'id (`/.env`, `/.git`, `wp-login.php` jne)
saavad ajutise IP-ban'i.

Harnessi/hookide jaoks salvestab server append-only `raw_events` ridu. Saada batch `POST /api/events`
kaudu (`events: [...]`) või kasuta spets-endpointe `POST /api/prompt/start`, `/api/prompt/done`,
`/api/agent/heartbeat`, `/api/agent/tool-start`, `/api/agent/tool-end`. Tavapärane `aitrack run` teeb
praktikapäeviku tunni sõnastuse lokaalselt kogu kättesaadava AI-vestluse põhjal ja saadab serverisse valmis
kokkuvõtterea. Prompt-eventid lähevad kliendist serverisse vaikimisi ainult metadatana (aeg, projekt,
tööriist, kestus ja tekstipikkus; mitte prompti tekst). Pi prompti lõpetamisel saadetakse eraldi lokaalselt
valminud tehtu kokkuvõte. Activity näitab selle kõrval ainult konteksti tokenite arvu; muid tehnilisi
kontekstiloendureid ega varasemat vestlustranskripti serverisse ei saadeta. Ainult jooksva prompti täistekst on
opt-in seadistusega `server_prompt_events: "full"`;
`"off"` lülitab prompt-eventide batch-saatmise välja. Kui valmis tunnirida pole,
eelistab serveri fallback sisulisi `prompt_events` ridu ning kasutab work-session/minute infot ainult viimase
varuvariandina. Ekspordi raw evente
`GET /api/export/raw-events?period=YYYY-MM` kaudu; payload'id piiratakse ning tüüpilised token/parool/saladuse
võtmed redigeeritakse enne talletamist. `before_tool_call` uuendab jooksva tooli välja ja watchdog saab
näidata `stuck` sessioone. Work-session'id ja sleep-gap'e arvestavad aktiivsed intervallid on eksporditavad
`GET /api/export/work-sessions` ja `GET /api/export/active-intervals` endpointidest; intervallid lõigatakse
päringu perioodipiiride järgi.

Pi automaatjälgimiseks:

```bash
aitrack connect --url https://aitrack.example.com --token TOKEN
aitrack add /tee/projektini
aitrack install --minute-tracking --pi-extension
# Pi sees: /reload või ava uus pi session
```

Pi extension saadab promptide alguse/lõpu, tool-call'id ja harnessi lühikokkuvõtted serverisse.
Kui Pi kasutab `bash` tooli, proovib aitrack käsu põhjal kuvada päris tegevuse (`testid`, `ssh`,
`docker compose`, `git`, `read`, `write` jne), mitte ainult `bash`.

Arve jaoks server dokumenti ei tee, vaid annab export-andmed välisele arvegeneraatorile. Kuuraporti jaoks kasuta
`/api/report/monthly` endpointi või CLI käsku `aitrack report month`. See koondab töö issue järgi, arvutab minutid,
aja ja summa, tagastab `work_done[]` kokkuvõtted ning tõendusviited `work_session_uid` ja raw-event ID-dega.
Kui sessioonil pole explicit issue seost, proovib eksport üheselt tuvastada issue numbrit tekstidest kujul `#667`,
`issue 667` või `GH-667`. Admin-token näeb kõigi kasutajate ridu; tavakasutaja token ainult enda omi.

```bash
aitrack report month --period 2026-06 \
  --project-key github.com/puhastusproff/pp-finar \
  --rate 82 --format json

curl -H "User-Agent: aitrack/1.0" \
  "https://aitrack.example.com/api/report/monthly?token=TOKEN&period=2026-06&project_key=github.com/puhastusproff/pp-finar&hourly_rate=82"
```

Vana `/api/billing/invoice-lines` endpoint jääb ühilduvuseks alles, kuid vastuses on `deprecated: true`;
eelista uue raporti jaoks `/api/report/monthly` ning madalama taseme ekspordiks `/api/export/work-sessions` ja
`/api/export/active-intervals`.

Praktikakokkuvõtte saab serverist:

```bash
curl -H "X-Aitrack-Token: TOKEN" \
  "https://aitrack.example.com/api/practice/summary?period=2026-06"
```

**Tööpäeva algus:** seadistuse mõttes ei tee midagi; soovi korral `aitrack status` või `aitrack start`.
**Tööpäeva lõpp:** `aitrack start` → kontrolli/muuda → “Kopeeri D–G” → kleebi lehele. (Digest/teavitus töötab nagu enne.)

| Olukord | Käsk |
|---|---|
| "Mis täna kirja läks?" | `aitrack digest --days 1` |
| "Kontrolli enne, mida kirjutataks" | `aitrack preview --hours 8` |
| "Alustasin uut projekti" | `aitrack add ~/uus-projekt` |
| "Arvuti oli paar tundi kinni" | `aitrack backfill --hours 4` |

## Uue keskserveri kasutaja juhend

1. **Logi veebis sisse.** Ava `https://aitrack.example.com/login`, sisesta kasutajanimi ja ajutine parool.
   Mine `Kasutaja` lehele ja vaheta ajutine parool kohe ära.
2. **Installi klient ühe käsuga.** Ava `Kasutaja → Installi aitrack arvutisse`, vali oma OS ja kopeeri käsk terminali.
   Installikood on ühekordne ja aegub 15 minutiga. Skript kloonib repo, teeb `aitrack connect`, küsib projekti tee ning
   paigaldab `--minute-tracking --pi-extension`.
3. **Lisa ainult need projektid, mida tohib jälgida.** Kui jätsid installi ajal projekti lisamata, tee hiljem:

   ```bash
   aitrack add ~/Projects/pp-finar
   aitrack list
   ```

   Pi extension on globaalne, aga aitrack saadab serverisse ainult `aitrack add` kaudu lisatud projektide tegevused.
4. **Pi reload.** Pi sees tee `/reload` või ava uus Pi sessioon.
5. **Issue sidumine.** Anna issue käsitsi `--issue 123` või kasuta branchi nime nagu
   `fix/123-luhikirjeldus`; aitrack seob töö selle issue'ga automaatselt.
6. **Vaata tulemusi.** Ava `https://aitrack.example.com/activity`. Filtrid rakenduvad kohe; kuupäevaga saab valida
   ühe päeva või vahemiku.

Ära pane paroole ega API token'eid README-sse, issue'sse, chatti ega commit'i. Ajutine parool on ainult esimeseks loginiks.

## Teisele inimesele jagamine
1. Anna talle see kaust (zip / git).
2. Tema jooksutab `install.sh` / `install.ps1` ja läbib `setup` nõustaja.
3. Iga inimese andmed lähevad **tema enda** väljundisse (oma Sheet või oma fail) — privaatne.
   Kokkuvõtted teeb **tema enda** AI-CLI.
4. Kui kasutate keskserverit, järgi ülal olevat "Uue keskserveri kasutaja juhendit".

## Ajavöönd
- Linux/macOS tuvastab IANA-tsooni automaatselt (`/etc/localtime`).
- **Windowsis (või kui kuvaajad on valed)** määra tsoon selgelt:
  `python3 aitrack.py init --timezone Europe/Tallinn`
  (Windowsil võib vaja minna `pip install tzdata`.)
- Serveri export/activity päringutes saab anda `timezone=Europe/Tallinn` või `tz=Europe/Tallinn`; kuupäeva- ja kuupiirid lõigatakse siis kohaliku päeva/kuu järgi, DB-s jäävad ajatemplid UTC-sse.

## Veaotsing
- Logi: `~/.config/aitrack/aitrack.log` ja `aitrack status`
- **Linux:** `systemctl --user list-timers aitrack.timer` · käsitsi: `systemctl --user start aitrack.service`
  · väljalogituna jooksmiseks: `sudo loginctl enable-linger $USER`
- **macOS:** `launchctl list | grep aitrack` · logid `~/.config/aitrack/launchd.*.log`
- **Windows:** Task Scheduler → ülesanne `aitrack` · käsitsi: `schtasks /Run /TN aitrack`

## Märkused / piirangud
- **Projekti tuvastus käib `cwd`/`workspace` järgi.** Kui allowlistis on konkreetne Git repo,
  seotakse selle alamkaustad sama repoga. Kui allowlistis on üldine juurkaust (nt `~/projects`),
  otsib aitrack iga logikirje `cwd`-st ülespoole lähima Git tööpuu juure (`.git` kataloog või `.git`
  fail worktree puhul), aga ainult allowlisti juure piires. Nii eristuvad `~/projects/a` ja
  `~/projects/team/b` eraldi projektidena. Kui `cwd` on allowlisti juure all, aga Git tööpuud ei leita,
  kasutatakse projektina allowlisti juure esimest alamkausta: `~/praktika/ttjo` → `ttjo`.
  **Käivita AI projektikaustast** (`cd projekt && pi/claude`), et filtreerimine töötaks.
- **Server kaitseb samuti allowlistiga.** `aitrack add` saadab lubatud juure serverisse ja peab tulema päris
  terminalist; AI agenti või skripti käest uut projekti jälgimisse ei lisata. Kui klient või agent proovib
  `work start`/evente saata projektist, mida pole lisatud, server ei salvesta neid. Kui lisasid projekti enne serveriga
  ühendamist, tee pärast `aitrack connect` uuesti `aitrack add /tee/projektini` või käivita mõni tavaline aitrack käsk,
  mis allowlisti sünkroniseerib.
- **Kliendi sunduuendus.** Klient saadab iga serveripäringuga `CLIENT_VERSION` täisarvu. Serveri `.env` väärtus
  `MIN_CLIENT_VERSION` määrab miinimumi. Kui klient on vanem, vastab server `upgrade_required` ja klient uuendab end
  ise ametlikust `aitrack` Git repost ning käivitab sama käsu ühe korra uuesti. Server ei saada shell-käske.
- **Kaks faili.** Sisemine tunnipõhine algandmestik `~/.config/aitrack/hours.csv`
  (veerud `Kuupäev | Tund | Objekt ja ülesanne | Saavutused | Takistused | Uued teadmised | Tööriist | _key`)
  on **allikas** — dedup ja päevavaate renderdamine. Kasutaja kleebitav **päevavaade**
  `~/aitrack-log.csv` (veerud `Kuupäev | Punkte | Nädalapäev | Objekt… | Saavutused | Takistused | Uued teadmised`)
  renderdatakse sellest ümber iga käivituse järel. Vana 5-veeru log migreeritakse esimesel
  käivitusel automaatselt (kogu tekst läheb "Objekt"-veergu — täpsusta soovi korral käsitsi).
- **Objekti ärinimed (`object_names` config'is):** kaardista kaust → ärinimi, et väljundis
  poleks kaustanime, nt `{"pp-finar": "Puhastusproff – Finar"}`. Rakendub uutele tundidele
  (LLM kirjeldab objekti selle nime järgi; kaardistamata kausta nime ei lekitata).
- **Käsitsi-märkmed (`aitrack note`):** `aitrack note "õppisin X"` lisab praegusele tunnile
  märkme, mis läheb **"Uued teadmised"** veergu (sildiga `Märge:`). Kui sel tunnil AI-tegevust
  polnud, tekib eraldi `(märge)`-punkt (ei kao kaotsi).
  Märkmed hoitakse failis `~/.config/aitrack/notes.jsonl` (UTC-tunni võtmega).
- **Punkti tase (`group_by` config'is):** `"hour"` (vaikimisi) → **üks punkt tunni kohta, kõik
  kaustad koos**. `"project"` → eraldi punkt iga (tund × projekt) kohta. Muuda:
  `python3 -c "import aitrack as A; c=A.load_config(); c['group_by']='project'; A.save_config(c)"`.
- **Config ja andmed on masinapõhised.** `config.json`, `projects.json`, `notes.jsonl` ja
  **CSV-logi ise** (`~/aitrack-log.csv`) sisaldavad selle inimese isiklikke andmeid — neid **EI
  commitita** (kõik `.gitignore`-s); jagatakse ainult kood (`aitrack.py` jne). Iga kasutaja saab
  sama süsteemi (vaikimisi lokaalne CSV `~/aitrack-log.csv`), aga **oma** privaatse logifaili.
  `config.json` ja CSV kirjutatakse õigustega `0o600`.
- Harv: kui AI-tööriist kirjutab logirea >5 min pärast tunnipiiri, võib see kirje ühest
  tunnist välja jääda (tööriistad kirjutavad tavaliselt kohe). Lisaveerud (failid, git-haru,
  promptide arv, kategooria) on lihtne lisada — küsi.
- **Privaatsus — ole teadlik:** kokkuvõtte tegemiseks saadetakse promptide tekst sinu
  AI-CLI-le, mis tavaliselt kasutab **pilve-API-t** (nt `claude -p` → Anthropicu API), ja
  valmis kokkuvõtte rida läheb **Google Sheetsi**. St promptisisu ja kokkuvõtted lahkuvad
  masinast nendesse teenustesse. Toorest promptisisu lehele ei panda (ka mootori puudumisel
  on varurida geneeriline). Kui tund sisaldab tundlikku infot, kaalu projekti mittejälgimist.
