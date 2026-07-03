# aitrack — AI-tööriistade tunnipõhine tööpäevik

Vaatab iga tund läbi sinu **Claude Code**, **Codex** ja **Antigravity** (agy) sessioonid,
filtreerib **ainult sinu valitud projektid** ja kirjutab **Google Sheetsi** ühe rea iga
(tund × projekt) kohta — lühikese eestikeelse kokkuvõttega tehtud tööst.

**Töötab macOS / Windows / Linux peal.** Jagatav: anna kaust edasi, iga inimene seadistab
oma Google Sheeti ja jälgitavad projektid ise.

## Nõuded
- **Python 3.9+** (muud sõltuvused puuduvad — ainult standardteek)
- Vähemalt üks AI-CLI kokkuvõtete tegemiseks: **`claude`**, **`codex`** või **`gemini`**
  (automaattuvastus selles järjekorras; saab configis fikseerida)
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
   ├─ parsib kolme tööriista lokaalsed logid (Claude jsonl / Codex+agy history.jsonl)
   ├─ FILTREERIB ainult lubatud projektid (projects.json)
   ├─ grupeerib LÕPETATUD tunnid (tund × projekt) — arvestus UTC-s (DST-kindel)
   ├─ iga ämbri kohta → AI-CLI teeb 1-lauselise eestikeelse kokkuvõtte
   └─ lisab read Google Sheetsi (dedup-võtmega → kordussaatmine ei tekita duplikaate)
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
| `aitrack setup` | **Interaktiivne seadistus algusest lõpuni** (soovitatav). |
| `aitrack suggest [--days N]` | Näita logidest aktiivseid projektikaustu (pingerida). |
| `aitrack add/remove <tee>` | Lisa/eemalda jälgitav projekt. |
| `aitrack note "<tekst>"` | **Lisa käsitsi-märge praegusele tunnile** (nt õpitu, koosolek). Tühjalt = kuva märkmed. |
| `aitrack digest [--days N] [--notify]` | Päeva/nädala kokkuvõte (valikuliselt töölaua-teavitus). |
| `aitrack status` | Näita platvormi, mootorit, väljundit ja logiallikaid. |
| `aitrack preview --hours N` | Kuiv vaade — mida kirjutataks, väljundisse saatmata. |
| `aitrack backfill --hours N` | Töötle viimased N tundi tagasiulatuvalt (ei nihuta watermarki). |
| `aitrack test-sink` | Saada testrida väljundisse. |
| `aitrack list` | Näita jälgitavaid projekte. |
| `aitrack init` / `install` / `uninstall` | Käsitsi: config / ajasti seadistus / eemaldus. |

## Tööpäeva näide

**Põhimõte:** sina lihtsalt töötad nagu tavaliselt. Tööriist jookseb taustal iga tund (:05)
ja kirjutab ühe rea iga (tund × projekt) kohta. Ainus reegel — **käivita AI projektikaustast**:

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

**Tulemus väljundis** (rida tekib ~5 min pärast tunni lõppu):
```
| Kuupäev    | Tund        | Projekt        | Tööriist      | Töö kokkuvõte                                  |
| 2026-06-17 | 09:00–10:00 | kalaradar-mono | Claude        | Lisas PWA norra tõlked, parandas locale-bugi   |
| 2026-06-17 | 10:00–11:00 | kalaradar-mono | Claude, Codex | Kirjutas ekspordi testid, debugis API 500-vea  |
| 2026-06-17 | 13:00–14:00 | parkproduction | Claude        | Koostas müügipakkumise ja PageSpeed-paranduse  |
```

**Tööpäeva algus:** seadistuse mõttes ei tee midagi; soovi korral `aitrack status`.
**Tööpäeva lõpp:** automaatne 18:00 teavitus (kui lubasid), või käsitsi `aitrack digest --days 1`;
täisülevaate jaoks ava oma Google Sheet / CSV-fail.

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
- Tabeli veerud: `Kuupäev | Tund | Projekt | Tööriist | Töö kokkuvõte` (+ peidetav `_key`).
- **Käsitsi-märkmed (`aitrack note`):** iga kasutaja saab lisada praegusele tunnile märkme
  (nt õpitu, koosolek, otsus): `aitrack note "õppisin X"`. Märge liidetakse selle tunni rea
  kokkuvõttesse; kui sel tunnil AI-tegevust polnud, tekib eraldi `(märge)`-rida (ei kao kaotsi).
  Märkmed hoitakse masinapõhiselt failis `~/.config/aitrack/notes.jsonl` (UTC-tunni võtmega).
- **Rea tase (`group_by` config'is):** `"project"` (vaikimisi) → üks rida iga (tund × projekt)
  kohta. `"hour"` → **üks rida tunni kohta, kõik kaustad koos** (Projekt-veerus loetelu, nt
  `aitracker, praktika`; kokkuvõte katab kogu tunni tegevuse). Muuda:
  `python3 -c "import aitrack as A; c=A.load_config(); c['group_by']='hour'; A.save_config(c)"`.
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
