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

> Juba seadistatud? Kõik on edaspidi käsuga **`aitrack <...>`** (lühivorm; ka `python3 aitrack.py <...>` töötab).

## Käsud

| Käsk | Tähendus |
|---|---|
| `aitrack help` / `aitrack --help` | Näita praktilist abi ja sinu OS-iga sobivaid kopeerimiskäske Google Sheetsi jaoks. |
| `aitrack start` | Ava lokaalne brauseri-UI, kus saad tänaseid ja varasemaid päevi vaadata, muuta, ridu lisada ning ühe nupuga Sheetsi kopeerida. |
| `aitrack serve` | Käivita keskserver SQLite andmebaasiga mitme kasutaja jaoks. |
| `aitrack user add/list` | Lisa/listi keskserveri kasutajaid ja token'eid. |
| `aitrack connect --url ... --token ...` | Ühenda klient keskserveriga; `aitrack run` saadab tunniread ja prompt-eventid serverisse. |
| `aitrack setup` | **Interaktiivne seadistus algusest lõpuni** (soovitatav). |
| `aitrack suggest [--days N]` | Näita logidest aktiivseid projektikaustu (pingerida). |
| `aitrack add/remove <tee>` | Lisa/eemalda jälgitav projekt. |
| `aitrack note "<tekst>"` | **Lisa käsitsi-märge praegusele tunnile** (nt õpitu, koosolek). Läheb "Uued teadmised" veergu. Tühjalt = kuva märkmed. |
| `aitrack day [KUUPÄEV]` | **Prindi päeva sisuveerud D–G** (Objekt/Saavutused/Takistused/Uued teadmised, tab-eraldus) — vali Sheetsis lahter `D<rida>` ja Ctrl+V. Vaikimisi viimane päev; `--all` = kõik; `--header` = päiserida; `--full` = kõik 7 veergu; `--html` = clipboardi jaoks, säilitab punktid lahtris eri ridadel; `--flat` = üks füüsiline rida. |
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
ühe **nummerdatud punkti** ja numbrid on kõigis neljas sisuveerus kohakuti. LLM jagab iga tunni
nelja välja: *Objekt ja ülesanne / Saavutused / Takistused / Uued teadmised*.

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
SQLite-põhise keskserveri. Serveris loo kasutaja `aitrack user add karl --db /data/server.db`, kliendis
seadista `aitrack connect --url https://aitrack.example.com --token TOKEN`. Seejärel saadab kliendi
`aitrack run` tunniread ja minuti täpsusega prompt-eventid serverisse. Dockeris kasuta repo juures
`docker compose up -d --build` (vaikimisi seob `127.0.0.1:3102`).

**Tööpäeva algus:** seadistuse mõttes ei tee midagi; soovi korral `aitrack status` või `aitrack start`.
**Tööpäeva lõpp:** `aitrack start` → kontrolli/muuda → “Kopeeri D–G” → kleebi lehele. (Digest/teavitus töötab nagu enne.)

| Olukord | Käsk |
|---|---|
| "Mis täna kirja läks?" | `aitrack digest --days 1` |
| "Kontrolli enne, mida kirjutataks" | `aitrack preview --hours 8` |
| "Alustasin uut projekti" | `aitrack add ~/uus-projekt` |
| "Arvuti oli paar tundi kinni" | `aitrack backfill --hours 4` |

## Teisele inimesele jagamine
1. Anna talle see kaust (zip / git).
2. Tema jooksutab `install.sh` / `install.ps1` ja läbib `setup` nõustaja.
3. Iga inimese andmed lähevad **tema enda** väljundisse (oma Sheet või oma fail) — privaatne.
   Kokkuvõtted teeb **tema enda** AI-CLI.

## Ajavöönd
- Linux/macOS tuvastab IANA-tsooni automaatselt (`/etc/localtime`).
- **Windowsis (või kui kuvaajad on valed)** määra tsoon selgelt:
  `python3 aitrack.py init --timezone Europe/Tallinn`
  (Windowsil võib vaja minna `pip install tzdata`.)

## Veaotsing
- Logi: `~/.config/aitrack/aitrack.log` ja `aitrack status`
- **Linux:** `systemctl --user list-timers aitrack.timer` · käsitsi: `systemctl --user start aitrack.service`
  · väljalogituna jooksmiseks: `sudo loginctl enable-linger $USER`
- **macOS:** `launchctl list | grep aitrack` · logid `~/.config/aitrack/launchd.*.log`
- **Windows:** Task Scheduler → ülesanne `aitrack` · käsitsi: `schtasks /Run /TN aitrack`

## Märkused / piirangud
- **Projekti tuvastus käib `cwd`/`workspace` järgi.** Kui käivitad AI-d kodukaustast
  (`/home/sina`) mitte projektikaustast, ei saa tööd projektidesse jagada.
  **Käivita AI projektikaustast** (`cd projekt && claude`), et filtreerimine töötaks.
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
