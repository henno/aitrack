"""aitrack korrektsustestid — monkeypatch'iga, ilma võrgu/LLM-ita.

Jooksuta:  python3 aitrack_tests.py
"""
import datetime as dt
import json
import os
import sys
import types
from pathlib import Path

os.environ["AITRACK_CONFIG_DIR"] = "/tmp/aitrack-selftest"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import aitrack as A  # noqa: E402

# päris-funktsioonid (setup() monkeypatchib mõned) — hoia originaalid alles
_REAL = {n: getattr(A, n) for n in ("append_rows", "fetch_existing_keys", "collect_records")}

CFG = Path(os.environ["AITRACK_CONFIG_DIR"])
UTC = dt.timezone.utc
D = lambda h, m=30: dt.datetime(2026, 6, 16, h, m, tzinfo=UTC)  # noqa: E731
HFL = lambda h: dt.datetime(2026, 6, 16, h, 0, tzinfo=UTC)      # noqa: E731

SINK_KEYS = set()
SENT = []
ADDED = []
SUMMARIZE_CALLS = []

def reset_sink():
    SINK_KEYS.clear(); SENT.clear(); ADDED.clear(); SUMMARIZE_CALLS.clear()

def fake_summarize(prompts, proj, label, cfg):
    SUMMARIZE_CALLS.append(label)
    # 4-väljaline dict (nagu päris summarize); objekt kannab proj-silti, et testid saaks kontrollida
    return {"objekt": f"OBJ[{proj}]", "saavutus": f"STUB({len(prompts)})",
            "takistus": A._NA, "teadmine": A._NA}

def fake_append(rows, keys, cfg):
    SENT.append((rows, keys))
    for row, k in zip(rows, keys):
        if k in SINK_KEYS:
            continue
        SINK_KEYS.add(k); ADDED.append(row)
    return True

def setup(records, state=None, allow=None):
    CFG.mkdir(parents=True, exist_ok=True)
    A.save_projects(allow or ["/proj"])
    (CFG / "config.json").write_text(json.dumps(
        {"timezone": "UTC", "sink": {"webapp_url": "x", "token": "t"}}), encoding="utf-8")
    if state is None:
        (CFG / "state.json").unlink(missing_ok=True)
    else:
        (CFG / "state.json").write_text(state, encoding="utf-8")
    A.collect_records = lambda since: [r for r in records if r.ts > since]
    A.summarize = fake_summarize
    A.append_rows = fake_append
    A.fetch_existing_keys = lambda cfg: None  # vaikimisi: ei küsi (tavakäitumine)
    (CFG / "aitrack.lock").unlink(missing_ok=True)
    (CFG / "notes.jsonl").unlink(missing_ok=True)  # käsitsi-märkmed: puhas leht iga testi eel
    A.WORK_STATE_FILE.unlink(missing_ok=True)
    A.LOCAL_DB.unlink(missing_ok=True)
    A.HOURS_CSV.unlink(missing_ok=True)  # sisemine algandmestik: isoleeri iga test (real-append testid)

def cur_state():
    try:
        return json.loads((CFG / "state.json").read_text())
    except FileNotFoundError:
        return {}

PASS = 0; FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {name}")
    else: FAIL += 1; print(f"  ✗ FAIL: {name}")

REC = lambda h: A.Record("Claude", "/proj", D(h), f"töö tunnil {h}")  # noqa: E731

# ============ TEST 1: iga lõpetatud tund täpselt korra (off-by-one) ============
print("TEST 1: off-by-one — iga tund täpselt korra, ilma vahede/duplikaatideta")
reset_sink(); setup([REC(10), REC(11), REC(12)])
cfg = A.load_config(); allow = A.load_projects()
for nh, nm in [(11, 15), (12, 15), (13, 15)]:
    A._now_utc = lambda nh=nh, nm=nm: dt.datetime(2026, 6, 16, nh, nm, tzinfo=UTC)
    A.run_once(cfg, allow)
check("3 rida kokku (tunnid 10,11,12)", len(ADDED) == 3)
check("kõik kolm erinevad", len({r[1] for r in ADDED}) == 3)
check("watermark = 13:00 UTC", cur_state().get("last_processed_hour") == HFL(13).isoformat())

# ============ TEST 2: idempotentsus — crash enne state-uuendust ei dubleeri ============
print("TEST 2: idempotentsus — sama tunni kordustöötlus ei tekita duplikaati")
reset_sink(); setup([REC(10), REC(11)])
cfg = A.load_config(); allow = A.load_projects()
A._now_utc = lambda: dt.datetime(2026, 6, 16, 11, 15, tzinfo=UTC)
A.run_once(cfg, allow)
before = len(ADDED)
(CFG / "state.json").write_text(json.dumps({"last_processed_hour": HFL(10).isoformat()}))
A.run_once(cfg, allow)
check("esimene käivitus lisas 1 rea", before == 1)
check("kordus EI lisanud duplikaati", len(ADDED) == 1)
check("teine POST saadeti, 0 lisati (dedup)", len(SENT) == 2)

# ============ TEST 3: backfill EI muuda watermarki ============
print("TEST 3: backfill ei nihuta watermarki")
reset_sink(); setup([REC(10), REC(11), REC(12)], state=json.dumps({"last_processed_hour": HFL(13).isoformat()}))
cfg = A.load_config(); allow = A.load_projects()
A._now_utc = lambda: dt.datetime(2026, 6, 16, 13, 15, tzinfo=UTC)
A.run_once(cfg, allow, backfill_hours=3)
check("backfill saatis read (3)", len(ADDED) == 3)
check("watermark MUUTUMATU (13:00)", cur_state().get("last_processed_hour") == HFL(13).isoformat())

# ============ TEST 4: rikutud state katkestab ============
print("TEST 4: rikutud state.json → katkestab, ei saada midagi")
reset_sink(); setup([REC(11)], state="{see pole valiidne json")
cfg = A.load_config(); allow = A.load_projects()
A._now_utc = lambda: dt.datetime(2026, 6, 16, 12, 15, tzinfo=UTC)
A.run_once(cfg, allow)
check("rikutud state → 0 rida", len(ADDED) == 0)
check("state.json puutumata", (CFG / "state.json").read_text().startswith("{see"))

# ============ TEST 5: lukk hoiab paralleelset käivitust ============
print("TEST 5: lukk → teine käivitus väljub ilma töötlemata")
reset_sink(); setup([REC(11)])
cfg = A.load_config(); allow = A.load_projects()
A._now_utc = lambda: dt.datetime(2026, 6, 16, 12, 15, tzinfo=UTC)
(CFG / "aitrack.lock").write_text("99999")
A.run_once(cfg, allow)
check("lukk hõivatud → 0 rida", len(ADDED) == 0)
(CFG / "aitrack.lock").unlink(missing_ok=True)

# ============ TEST 6: dedup-võti UTC-põhine ja deterministlik ============
print("TEST 6: bucket_key UTC-põhine, deterministlik")
k1 = A.bucket_key(HFL(14), "proj")
k2 = A.bucket_key(HFL(14), "proj")
k3 = A.bucket_key(HFL(15), "proj")
check("sama UTC-tund → sama võti", k1 == k2)
check("erinev tund → erinev võti", k1 != k3)

# ============ TEST 7: võti STABIILNE üle ajavööndi/DST (regressioon) ============
print("TEST 7: dedup-võti ei muutu ajavööndi vahetusel (sama füüsiline tund)")
same_instant_other_tz = HFL(14).astimezone(dt.timezone(dt.timedelta(hours=5)))
check("sama hetk teises tsoonis → sama võti",
      A.bucket_key(HFL(14), "proj") == A.bucket_key(same_instant_other_tz, "proj"))

# ============ TEST 8: cmd_status ei kuku kokku rikutud state'iga ============
print("TEST 8: cmd_status rikutud state'iga ei viska erindit")
setup([], state="{rikutud")
cfg = A.load_config()
ok = True
try:
    A.cmd_status(types.SimpleNamespace(), cfg)
except Exception as e:  # noqa: BLE001
    ok = False; print("    erind:", e)
check("cmd_status töötas ilma erindita", ok)

# ============ TEST 9: config kirjutatakse atomaarselt + 0o600 ============
print("TEST 9: save_config 0o600 ja kehtiv JSON")
A.save_config({"timezone": "UTC", "sink": {"webapp_url": "u", "token": "salajane"}})
mode = oct(os.stat(CFG / "config.json").st_mode)[-3:]
check("config.json režiim 0600", mode == "600")
check("config loetav tagasi", A.load_config()["sink"]["token"] == "salajane")

# ============ TEST 10: varukokkuvõte ei leki toorest prompti ============
print("TEST 10: _fallback_item on geneeriline (ei leki promptisisu)")
fbi = A._fallback_item(["SALAJANE API VÕTI sk-12345", "veel üks"])
fb = " ".join(fbi.values())
check("ei sisalda toorest prompti", "SALAJANE" not in fb and "sk-12345" not in fb)
check("näitab promptide arvu", "2" in fbi["objekt"])

# ============ TEST 11: match_project — subpath jah, sibling ei ============
print("TEST 11: match_project tabab subpath'i, mitte sibling'it")
allow = ["/home/x/proj"]
check("subpath sobib", A.match_project("/home/x/proj/src", allow) == "/home/x/proj")
check("sibling EI sobi", A.match_project("/home/x/proj2", allow) is None)

# ============ TEST 12: backfill jätab juba-olemas tunni summeerimata (kuluvõit) ============
print("TEST 12: backfill ei summeeri tunde, mis on juba lehel")
reset_sink(); setup([REC(10), REC(11), REC(12)], state=json.dumps({"last_processed_hour": HFL(13).isoformat()}))
cfg = A.load_config(); cfg["group_by"] = "project"; allow = A.load_projects()  # test projekti-tasandi võtit
A._now_utc = lambda: dt.datetime(2026, 6, 16, 13, 15, tzinfo=UTC)
A.fetch_existing_keys = lambda cfg: {A.bucket_key(HFL(11), "/proj")}  # tund 11 juba lehel
A.run_once(cfg, allow, backfill_hours=3)
check("ainult 2 rida saadetud (10, 12)", len(ADDED) == 2)
check("summarize'i kutsuti 2× (mitte 3) — tund 11 vahele", len(SUMMARIZE_CALLS) == 2)
A.fetch_existing_keys = lambda cfg: None  # taasta

# ============ TEST 13: sama basename'iga projektid EI põrku (regressioon) ============
print("TEST 13: kaks samanimelist projekti (/a/web, /b/web) → mõlemad read, ei põrku")
reset_sink()
recs = [A.Record("Claude", "/a/web", D(11), "töö a"), A.Record("Claude", "/b/web", D(11), "töö b")]
setup(recs, allow=["/a/web", "/b/web"])
cfg = A.load_config(); cfg["group_by"] = "project"; allow = A.load_projects()  # projekti-tasandi read
A._now_utc = lambda: dt.datetime(2026, 6, 16, 12, 15, tzinfo=UTC)
A.run_once(cfg, allow)
check("mõlemad read kirjutatud (2)", len(ADDED) == 2)
check("kaks erinevat võtit (ei dedupitud)", len(SINK_KEYS) == 2)
check("/a/web ja /b/web võtmed erinevad", A.bucket_key(HFL(11), "/a/web") != A.bucket_key(HFL(11), "/b/web"))

# ============ TEST 14: _engine_cmd kuju (päris funktsioon, ilma monkeypatchita) ============
print("TEST 14: _engine_cmd — claude stdin, codex/gemini argv")
c_cmd, c_in = A._engine_cmd("claude", "/x/claude", "PROMPT", "")
check("claude: prompt stdin-i, mitte argv", c_cmd == ["/x/claude", "-p"] and c_in == "PROMPT")
x_cmd, x_in = A._engine_cmd("codex", "/x/codex", "PROMPT", "")
check("codex: argv, stdin None", x_cmd == ["/x/codex", "exec", "PROMPT"] and x_in is None)
g_cmd, g_in = A._engine_cmd("gemini", "/x/gemini", "PROMPT", "m")
check("gemini: argv + mudel, stdin None", g_cmd == ["/x/gemini", "-p", "PROMPT", "-m", "m"] and g_in is None)

# ============ TEST 15: codex_records päris parser (fixture) — cwd map + drop ============
print("TEST 15: codex_records fixture — mappib cwd, puuduv rollout kukutab AINULT selle kirje")
cxroot = CFG / "codex_fix"
(cxroot / "sessions" / "2026" / "06" / "16").mkdir(parents=True, exist_ok=True)
uuid1 = "019d395b-eba3-71f3-ad00-57af8b6abfbe"  # on rollout
uuid2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"  # rollout puudub
roll = cxroot / "sessions" / "2026" / "06" / "16" / f"rollout-2026-06-16T11-00-00-{uuid1}.jsonl"
roll.write_text(json.dumps({"type": "session_meta", "timestamp": "2026-06-16T11:00:00Z",
                            "payload": {"id": uuid1, "cwd": "/home/x/projekt"}}) + "\n", encoding="utf-8")
hist = cxroot / "history.jsonl"
hist.write_text(
    json.dumps({"session_id": uuid1, "ts": int(D(11).timestamp()), "text": "tee asi"}) + "\n" +
    json.dumps({"session_id": uuid2, "ts": int(D(11).timestamp()), "text": "orb prompt"}) + "\n",
    encoding="utf-8")
_orig_h, _orig_s = A.CODEX_HISTORY, A.CODEX_SESSIONS
A.CODEX_HISTORY, A.CODEX_SESSIONS = hist, cxroot / "sessions"
cx = A.codex_records(D(10))
A.CODEX_HISTORY, A.CODEX_SESSIONS = _orig_h, _orig_s
check("ainult 1 kirje (orb kukkus)", len(cx) == 1)
check("cwd õigesti mapitud", cx and cx[0].project == "/home/x/projekt")
check("säilis õige tekst", cx and cx[0].text == "tee asi")

# ============ TEST 16: load_config tüübikaitse (vale tüüp ei lõhu) ============
print("TEST 16: load_config — vale-tüübiga 'sink' ei viska AttributeError'it")
(CFG / "config.json").write_text(json.dumps({"sink": "RIKUTUD", "timezone": "UTC"}), encoding="utf-8")
ok = True
try:
    c = A.load_config()
    _ = c["sink"].get("token")  # peab olema dict, mitte string
except Exception as e:  # noqa: BLE001
    ok = False; print("    erind:", e)
check("vale-tüübiga sink → jääb dict-iks, ei lõhu", ok)

# ============ TEST 17: lokaalne sink — algandmestik dedupib, päevavaade renderdub ============
print("TEST 17: lokaalne sink kirjutab algandmestikku + renderdab päevavaate, dedup võtme järgi")
A.HOURS_CSV.unlink(missing_ok=True)  # isoleeri: puhas algandmestik
csvpath = CFG / "log.csv"
csvpath.unlink(missing_ok=True)
lcfg = {"sink": {"type": "local", "path": str(csvpath)}}
# raw-rida: [Kuupäev, Tund, Objekt, Saavutused, Takistused, Uued teadmised, Tööriist]
r1 = [["2026-06-16", "11:00–12:00", "objekt", "saavutus", "takistus", "teadmine", "Claude"]]
k1 = ["k:2026-06-16T11:00:00+00:00|proj|abc123"]
ok1 = _REAL["append_rows"](r1, k1, lcfg)        # päris dispatch → algandmestik + render
ok2 = _REAL["append_rows"](r1, k1, lcfg)         # sama võti uuesti → ei tohi dubleerida
import csv as _csv
with csvpath.open(newline="", encoding="utf-8") as f:
    data_rows = [row for row in _csv.reader(f)]
check("kirjutamine õnnestus", ok1 and ok2)
check("päevavaade: päis + 1 päevarida (dedup töötab)", len(data_rows) == 2)
check("päevarida: 1 punkt, nädalapäev T", data_rows[1][1] == "1" and data_rows[1][2] == "T")
check("fetch_existing_keys leiab võtme", _REAL["fetch_existing_keys"](lcfg) == set(k1))

# ============ TEST 18: suggest_projects loeb logidest projektid ============
print("TEST 18: suggest_projects pingerea logidest")
A.collect_records = lambda since: [
    A.Record("Claude", "/a/x", D(11), "1"), A.Record("Claude", "/a/x", D(11), "2"),
    A.Record("Codex", "/b/y", D(11), "3"),
]
sug = A.suggest_projects(30)
check("kaks kausta leitud", len(sug) == 2)
check("/a/x esikohal (2 prompti)", sug[0][0] == "/a/x" and sug[0][1] == 2)
check("tööriistad kaasas", "Claude" in sug[0][2])

# ============ TEST 19: compute_digest loeb tunnid/projektid ============
print("TEST 19: compute_digest aktiivsed tunnid ja projektid")
A.collect_records = lambda since: [
    A.Record("Claude", "/proj", D(10), "a"), A.Record("Claude", "/proj", D(10, 45), "b"),
    A.Record("Codex", "/proj", D(11), "c"),
]
th, per, pr = A.compute_digest({}, ["/proj"], 1)
check("2 aktiivset tundi (10 ja 11)", th == 2)
check("3 prompti loetud", pr == 3)
check("projekt /proj 2h", per.get("/proj") == 2)

# ============ TEST 20: write_ready_apps_script asendab ainult omistuse ============
print("TEST 20: valmis Apps Script — token omistuses, vaikesaladuse-kontroll puutumata")
ready = A.write_ready_apps_script("SALA123")
txt = ready.read_text(encoding="utf-8") if ready else ""
check("token omistatud", 'var SECRET = "SALA123";' in txt)
check("vaikesaladuse-kontroll alles", '=== "MUUDA-SEE-ARA"' in txt)

# ============ TEST 21: CSV-valemisüst — ohtlik kokkuvõte saab ' prefiksi ============
print("TEST 21: CSV/Sheets valemisüst neutraliseeritud")
check("_cell_safe prefiksib =", A._cell_safe("=1+1") == "'=1+1")
check("_cell_safe prefiksib @+-", all(A._cell_safe(c + "x")[0] == "'" for c in "@+-"))
check("_cell_safe ei muuda tavalist", A._cell_safe("tavaline") == "tavaline")
reset_sink()
csv2 = CFG / "inj.csv"; csv2.unlink(missing_ok=True)
setup([REC(11)])  # setup nullib ka HOURS_CSV → puhas algandmestik
A.append_rows = _REAL["append_rows"]          # päris lokaalne kirjutaja
A.fetch_existing_keys = _REAL["fetch_existing_keys"]
A.summarize = lambda *a, **k: {"objekt": '=HYPERLINK("http://evil")', "saavutus": "x",
                               "takistus": A._NA, "teadmine": A._NA}  # pahatahtlik objekt
A.save_config({"timezone": "UTC", "sink": {"type": "local", "path": str(csv2)}})
A.save_projects(["/proj"])
A._now_utc = lambda: dt.datetime(2026, 6, 16, 12, 15, tzinfo=UTC)
A.run_once(A.load_config(), A.load_projects())
content = csv2.read_text(encoding="utf-8")
raw_content = A.HOURS_CSV.read_text(encoding="utf-8")
check("algandmestikus objekt ' prefiksiga", "'=HYPERLINK" in raw_content)
check("päevavaates objekt ' prefiksiga", "'=HYPERLINK" in content)
check("kaitsmata =HYPERLINK rea/välja algust pole", "\n=HYPERLINK" not in content)

# ============ TEST 22: lokaalne CSV luuakse 0o600-ga ============
print("TEST 22: lokaalne CSV õigused 0600")
mode = oct(os.stat(csv2).st_mode)[-3:]
check("CSV režiim 0600", mode == "600")

# ============ TEST 23: _local_keys ignoreerib lühikest/võtmeta rida ============
print("TEST 23: _local_keys ei lange fantoomvõtmesse")
csv3 = CFG / "edited.csv"
csv3.write_text(
    "Kuupäev,Tund,Objekt ja ülesanne,Saavutused,Takistused,Uued teadmised,Tööriist,_key\n"
    "2026-06-16,11:00–12:00,objekt,saav,tak,teadm,Claude,k:2026-06-16T11:00:00+00:00|proj|aaa\n"
    "käsitsi lisatud rida ilma võtmeta\n", encoding="utf-8")
ks = A._local_keys(csv3)
check("ainult kehtiv k: võti, mitte fantoom", ks == {"k:2026-06-16T11:00:00+00:00|proj|aaa"})

# ============ TEST 24: _notify macOS — em-dash literaalselt, ilma \\u-escape'ita ============
print("TEST 24: _notify ei emiteeri \\u-escape'i ega reavahetust")
captured = {}
def _fake_run(cmd, **kw):
    captured["cmd"] = cmd
    return None
_orig_run, _orig_plat = A.subprocess.run, A._platform
A.subprocess.run = _fake_run
A._platform = lambda: "macos"
A._notify("aitrack", "AI-töö — rida1\nrida2")
A.subprocess.run, A._platform = _orig_run, _orig_plat
joined = " ".join(captured.get("cmd", []))
check("em-dash literaalne (ei \\u2014)", "\\u2014" not in joined and "—" in joined)
check("reavahetus asendatud ' · '-ga", "\\n" not in joined and "rida1 · rida2" in joined)

# ============ TEST 25: cmd_setup non-TTY → keeldub ilma promptimata ============
print("TEST 25: cmd_setup non-TTY guard")
class _NoTTY:
    def isatty(self):
        return False
_orig_stdin = A.sys.stdin
A.sys.stdin = _NoTTY()
ok25 = True
try:
    A.cmd_setup(types.SimpleNamespace(), A.load_config())
except Exception as e:  # noqa: BLE001
    ok25 = False; print("    erind:", e)
finally:
    A.sys.stdin = _orig_stdin
check("non-TTY setup ei viska erindit", ok25)

# ============ TEST 26: group_by="hour" → üks rida tunni kohta, kõik kaustad koos ============
print("TEST 26: group_by='hour' — mitu kausta samal tunnil → ÜKS rida, Projekt-veerus loetelu")
reset_sink()
recs = [A.Record("Claude", "/a/web", D(11), "töö web"),
        A.Record("Codex",  "/b/api", D(11), "töö api"),
        A.Record("Claude", "/a/web", D(12), "töö web hiljem")]
setup(recs, allow=["/a/web", "/b/api"], state=json.dumps({"last_processed_hour": HFL(11).isoformat()}))
cfg = A.load_config(); cfg["group_by"] = "hour"; allow = A.load_projects()
A._now_utc = lambda: dt.datetime(2026, 6, 16, 13, 15, tzinfo=UTC)
A.run_once(cfg, allow)
check("2 rida (tunnid 11 ja 12), MITTE 3", len(ADDED) == 2)
row11 = [r for r in ADDED if r[1].startswith("11:00")][0]
check("tund 11 objekt katab mõlemad kaustad", "api" in row11[2] and "web" in row11[2])
check("tund 11 Tööriist-veerus (viimane) mõlemad tööriistad", row11[6] == "Claude, Codex")
check("tunni-tasandi võti (ei sõltu kaustast)", A.bucket_key(HFL(11), None) in SINK_KEYS)
check("kaks eri tunni võtit", A.bucket_key(HFL(11), None) != A.bucket_key(HFL(12), None))

# ============ TEST 27: group_by="hour" idempotentne — kordustöötlus ei dubleeri ============
print("TEST 27: group_by='hour' — sama tunni kordus ei tekita duplikaati")
reset_sink()
setup([A.Record("Claude", "/a/web", D(11), "w"), A.Record("Codex", "/b/api", D(11), "a")],
      allow=["/a/web", "/b/api"])
cfg = A.load_config(); cfg["group_by"] = "hour"; allow = A.load_projects()
A._now_utc = lambda: dt.datetime(2026, 6, 16, 12, 15, tzinfo=UTC)
A.run_once(cfg, allow)
(CFG / "state.json").write_text(json.dumps({"last_processed_hour": HFL(11).isoformat()}))
A.run_once(cfg, allow)
check("hour-režiim: 1 rida kokku (kordus dedupitud)", len(ADDED) == 1)

# ============ TEST 28: käsitsi-märge liidetakse aktiivse tunni ritta ============
print("TEST 28: aitrack note — märge liidetakse selle tunni kokkuvõttesse")
reset_sink(); setup([REC(11)])
cfg = A.load_config(); allow = A.load_projects()
A.append_note(HFL(11), "õppisin claude --resume käsu")
A._now_utc = lambda: dt.datetime(2026, 6, 16, 12, 15, tzinfo=UTC)
A.run_once(cfg, allow)
check("1 rida (tund 11)", len(ADDED) == 1)
check("märge 'Uued teadmised' veerus", "õppisin claude --resume käsu" in ADDED[0][5])
check("märge eristub 'Märge:' sildiga", "Märge:" in ADDED[0][5])

# ============ TEST 29: märge tunnil ILMA tegevuseta → eraldi '(märge)'-rida ============
print("TEST 29: märge tühjal tunnil → '(märge)'-rida (ei kao kaotsi)")
reset_sink()  # AI-tegevus AINULT tunnil 11; state alates 11 → töötle tunnid 11 ja 12
setup([REC(11)], state=json.dumps({"last_processed_hour": HFL(11).isoformat()}))
cfg = A.load_config(); allow = A.load_projects()
A.append_note(HFL(12), "koosolek kliendiga")  # märge tunnil 12, kus AI-tegevust POLE
A._now_utc = lambda: dt.datetime(2026, 6, 16, 13, 15, tzinfo=UTC)
A.run_once(cfg, allow)
obj_col = {r[2] for r in ADDED}
check("kaks rida (tund 11 tegevus + tund 12 märge)", len(ADDED) == 2)
check("üks rida on '(märge)'", "(märge)" in obj_col)
note_row = [r for r in ADDED if r[2] == "(märge)"][0]
check("märkme-real õige tekst", "koosolek kliendiga" in note_row[5])
check("märkme-rea Tund on 12:00", note_row[1].startswith("12:00"))

# ============ TEST 30: märge dedup — kordustöötlus ei tekita duplikaati ============
print("TEST 30: '(märge)'-rida idempotentne (kordus ei dubleeri)")
reset_sink(); setup([REC(11)])
cfg = A.load_config(); allow = A.load_projects()
A.append_note(HFL(12), "koosolek")
A._now_utc = lambda: dt.datetime(2026, 6, 16, 13, 15, tzinfo=UTC)
A.run_once(cfg, allow)
(CFG / "state.json").write_text(json.dumps({"last_processed_hour": HFL(11).isoformat()}))
A.run_once(cfg, allow)
check("märkme-rida ei dubleeru", len([r for r in ADDED if r[2] == "(märge)"]) == 1)

# ============ TEST 31: load_notes round-trip + UTC-normaliseeritud võti ============
print("TEST 31: append_note/load_notes — UTC-tunni võti, mitu märget koondub")
reset_sink(); setup([REC(11)])
A.append_note(HFL(14), "esimene")
other_tz = HFL(14).astimezone(dt.timezone(dt.timedelta(hours=5)))  # sama hetk, teine tsoon
A.append_note(other_tz, "teine")
notes = A.load_notes()
check("mõlemad märkmed sama UTC-tunni all", notes.get(HFL(14).isoformat()) == ["esimene", "teine"])

# ============ TEST 32: vaikeväljund on lokaalne CSV + vaiketee fallback ============
print("TEST 32: vaikimisi sink=local ja _local_path annab vaiketee (sama süsteem kõigile)")
check("DEFAULT_CONFIG sink tüüp on local", A.DEFAULT_CONFIG["sink"]["type"] == "local")
check("_local_path → vaiketee kui path tühi + local",
      A._local_path({"sink": {"type": "local", "path": ""}}) == A.DEFAULT_CSV_PATH)
check("_local_path austab seatud teed",
      A._local_path({"sink": {"type": "local", "path": "/x/y.csv"}}) == Path("/x/y.csv"))
check("_local_path → None kui sheets ja path tühi",
      A._local_path({"sink": {"type": "google_sheets", "path": ""}}) is None)

# ============ TEST 33: git worktree kaardistub lubatud põhiprojekti alla ============
print("TEST 33: match_project tunneb sibling git-worktree põhiprojekti ära")
main = CFG / "mainrepo"
wt = CFG / "mainrepo-662"
(main / ".git" / "worktrees" / "mainrepo-662").mkdir(parents=True, exist_ok=True)
(wt / "src").mkdir(parents=True, exist_ok=True)
(wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/mainrepo-662\n", encoding="utf-8")
A._WORKTREE_MAIN_CACHE.clear()
check("worktree juur sobib põhiprojektiga", A.match_project(str(wt), [str(main)]) == str(main))
check("worktree alamkaust sobib põhiprojektiga", A.match_project(str(wt / "src"), [str(main)]) == str(main))

# ============ TEST 34: pi_records fixture ============
print("TEST 34: pi_records loeb Pi JSONL-sessioonist kasutaja prompti")
piroot = CFG / "pi_sessions"
(piroot / "--home-x-proj--").mkdir(parents=True, exist_ok=True)
pifile = piroot / "--home-x-proj--" / "s.jsonl"
pifile.write_text(
    json.dumps({"type": "session", "timestamp": D(10).isoformat(), "cwd": "/home/x/proj"}) + "\n" +
    json.dumps({"type": "message", "timestamp": D(11).isoformat(),
                "message": {"role": "user", "content": [{"type": "text", "text": "tee pi töö"}]}}) + "\n",
    encoding="utf-8")
_orig_pi = A.PI_SESSIONS
A.PI_SESSIONS = piroot
pir = A.pi_records(D(10))
A.PI_SESSIONS = _orig_pi
check("Pi parser tagastas 1 kirje", len(pir) == 1)
check("Pi parser projekt ja tekst õiged", pir and pir[0].tool == "Pi" and pir[0].project == "/home/x/proj" and pir[0].text == "tee pi töö")

# ============ TEST 35: opencode_records fixture ============
print("TEST 35: opencode_records loeb OpenCode SQLite baasist kasutaja tekstiosa")
import sqlite3 as _sqlite3
odb = CFG / "opencode.db"
odb.unlink(missing_ok=True)
conn = _sqlite3.connect(odb)
conn.execute("CREATE TABLE session(id text primary key, directory text)")
conn.execute("CREATE TABLE message(id text primary key, session_id text, data text)")
conn.execute("CREATE TABLE part(id text primary key, message_id text, session_id text, time_created integer, data text)")
conn.execute("INSERT INTO session VALUES (?, ?)", ("s1", "/home/x/proj"))
conn.execute("INSERT INTO message VALUES (?, ?, ?)", ("m1", "s1", json.dumps({"role": "user"})))
conn.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?)",
             ("p1", "m1", "s1", int(D(11).timestamp() * 1000), json.dumps({"type": "text", "text": "tee opencode töö"})))
conn.commit(); conn.close()
_orig_oc = A.OPENCODE_DB
A.OPENCODE_DB = odb
ocr = A.opencode_records(D(10))
A.OPENCODE_DB = _orig_oc
check("OpenCode parser tagastas 1 kirje", len(ocr) == 1)
check("OpenCode parser projekt ja tekst õiged", ocr and ocr[0].tool == "OpenCode" and ocr[0].project == "/home/x/proj" and ocr[0].text == "tee opencode töö")

# ============ TEST 36: day --flat on clipboard-kindel D–G TSV ============
print("TEST 36: day --flat väljastab ühe füüsilise D–G TSV-rea")
import io as _io
import contextlib as _contextlib
A.HOURS_CSV.write_text(
    ",".join(A.RAW_HEADER) + "\n" +
    "2026-06-16,11:00–12:00,1. objekt,1. saavutus,1. takistus,1. teadmine,Claude,k:flat\n",
    encoding="utf-8")
buf = _io.StringIO()
with _contextlib.redirect_stdout(buf):
    A.cmd_day(types.SimpleNamespace(date="2026-06-16", all=False, header=False, full=False, flat=True), {"sink": {"type": "local", "path": ""}})
flat_out = buf.getvalue()
check("flat väljundis üks reavahetus lõpus", flat_out.count("\n") == 1)
check("flat D–G = 4 veergu / 3 tabi", flat_out.rstrip("\n").count("\t") == 3)
check("flat ei sisalda kuupäeva A-veerust", not flat_out.startswith("2026-06-16\t"))

# ============ TEST 37: day --html säilitab lahtrisisese reavahetuse ============
print("TEST 37: day --html väljastab HTML-tabeli D–G lahtritega ja <br> punktidega")
A.HOURS_CSV.write_text(
    ",".join(A.RAW_HEADER) + "\n" +
    "2026-06-16,11:00–12:00,esimene,saav,tak,tead,Claude,k:h1\n" +
    "2026-06-16,12:00–13:00,teine,saav2,tak2,tead2,Pi,k:h2\n",
    encoding="utf-8")
buf = _io.StringIO()
with _contextlib.redirect_stdout(buf):
    A.cmd_day(types.SimpleNamespace(date="2026-06-16", all=False, header=False, full=False, flat=False, html=True), {"sink": {"type": "local", "path": ""}})
html_out = buf.getvalue()
check("html väljund on tabel", html_out.startswith("<table><tr><td>") and html_out.rstrip().endswith("</table>"))
check("html D–G = 4 lahtrit", html_out.count("<td>") == 4)
check("html säilitab punktid <br>-idega", "1. esimene<br>2. teine" in html_out)
check("html ei sisalda kuupäeva A-veerust", "2026-06-16" not in html_out)

# ============ TEST 38: start UI salvestus asendab ühe päeva read ============
print("TEST 38: start UI helper _replace_day_rows asendab päeva ja renderdab päevavaate")
A.HOURS_CSV.unlink(missing_ok=True)
ui_csv = CFG / "ui-day.csv"; ui_csv.unlink(missing_ok=True)
ui_cfg = {"sink": {"type": "local", "path": str(ui_csv)}}
A._replace_day_rows("2026-06-16", [
    {"hour": "10:00–11:00", "objekt": "obj", "saavutus": "saav", "takistus": "Ei olnud", "teadmine": "tead", "tool": "Käsitsi"},
    {"hour": "11:00–12:00", "objekt": "obj2", "saavutus": "saav2", "takistus": "tak2", "teadmine": "tead2", "tool": "Pi"},
], ui_cfg)
with A.HOURS_CSV.open(newline="", encoding="utf-8") as f:
    raw_rows_ui = list(_csv.reader(f))[1:]
rendered = ui_csv.read_text(encoding="utf-8")
check("UI salvestas 2 rida algandmestikku", len([r for r in raw_rows_ui if r[0] == "2026-06-16"]) == 2)
check("UI renderdas päevavaate 2 punktiga", ",2,T," in rendered)
A._replace_day_rows("2026-06-16", [
    {"hour": "12:00–13:00", "objekt": "uus", "saavutus": "uus saav", "takistus": "Ei olnud", "teadmine": "uus tead", "tool": "Käsitsi"},
], ui_cfg)
with A.HOURS_CSV.open(newline="", encoding="utf-8") as f:
    raw_rows_ui2 = list(_csv.reader(f))[1:]
raw2 = "\n".join(",".join(r) for r in raw_rows_ui2)
check("UI teine salvestus asendas sama päeva read", len([r for r in raw_rows_ui2 if r[0] == "2026-06-16"]) == 1 and "uus" in raw2 and "obj2" not in raw2)

# ============ TEST 39: start UI copy payload HTML ============
print("TEST 39: _html_table_for_day annab Sheetsile sobiva HTML-payloadi")
html_payload, text_payload = A._html_table_for_day("2026-06-16", full=False)
check("copy payload on tbody tabel", html_payload.startswith("<table><tbody><tr><td>"))
check("copy payload D–G = 4 td", html_payload.count("<td>") == 4)
check("copy text fallback sisaldab tabe", text_payload.count("\t") == 3)

# ============ TEST 40: start UI kustutuskinnitus + textarea autosize ============
print("TEST 40: start UI sisaldab kustutuskinnitust ja automaatselt kasvavaid tekstikaste")
page = A._start_page_html()
check("Kustuta kasutab deleteRow kinnitusega", "function deleteRow" in page and "confirm('Kas kustutan selle rea?" in page)
check("textarea autosize olemas", "function autoResizeTextarea" in page and "scrollHeight" in page)
check("textarea overflow hidden", "overflow:hidden" in page)

# ============ TEST 41: SQLite keskserveri helperid ============
print("TEST 41: SQLite keskserver salvestab tunniread ja prompt-eventid tokeniga")
sdb = CFG / "server-test.db"
sdb.unlink(missing_ok=True)
tok = A._db_add_user(sdb, "karl")
A._db_ingest_rows(sdb, tok, [["2026-06-16", "10:00–11:00", "obj", "saav", "tak", "tead", "Pi"]], ["k:server:1"])
check("server keys sisaldab ingestitud võtit", A._db_keys(sdb, tok) == {"k:server:1"})
check("server day row loetav", A._db_rows_for_day(sdb, tok, "2026-06-16")[0][2] == "obj")
A._db_ingest_events(sdb, tok, [{"event_key": "e1", "tool": "Pi", "project": "/proj", "prompt_text": "tee", "started_at": HFL(10).isoformat(), "ended_at": HFL(10).isoformat(), "duration_seconds": 60}])
with A._db_connect(sdb) as conn:
    ev_count = conn.execute("SELECT COUNT(*) AS c FROM prompt_events").fetchone()["c"]
check("server salvestas prompt-eventi", ev_count == 1)

# ============ TEST 42: repo URL normaliseerimine projektivõtmeks ============
print("TEST 42: repo URL normaliseerimine annab eri kloonidele sama project_key")
check("SSH ja HTTPS URL normaliseeruvad samaks",
      A._normalise_repo_url("git@github.com:Puhastusproff/pp-finar.git") ==
      A._normalise_repo_url("https://github.com/Puhastusproff/pp-finar.git") ==
      "github.com/puhastusproff/pp-finar")
check("branchist leitakse issue number", A._issue_from_branch("fix/662-asendaja-puhadetasu") == "662")

# ============ TEST 43: 3NF work_session + invoice/practice vaated ============
print("TEST 43: serveri normaliseeritud work_session'id toidavad arve- ja praktikavaadet")
wdb = CFG / "server-work-test.db"
wdb.unlink(missing_ok=True)
wtok = A._db_add_user(wdb, "karl")
payload = {
    "project": {"project_key": "github.com/puhastusproff/pp-finar", "repo_url": "git@github.com:Puhastusproff/pp-finar.git", "name": "pp-finar", "local_path": "/tmp/pp-finar-pi", "checkout_id": "co-pi", "branch": "662-test"},
    "issue": {"provider": "github", "issue_key": "662", "title": "Asendaja pühadetasu"},
    "work": {"title": "asendaja pühadetasu parandamine", "summary": "algus", "billable": True},
    "session": {"client_id": "client-test", "device_name": "testbox", "platform": "linux", "checkout_id": "co-pi", "tool": "pi", "local_path": "/tmp/pp-finar-pi", "cwd": "/tmp/pp-finar-pi", "branch": "662-test"},
    "started_at": HFL(10).isoformat(),
}
start = A._db_work_start(wdb, wtok, payload)
check("server väljastab avaliku work_session_uid", str(start.get("work_session_uid", "")).startswith("ws_"))
A._db_work_tick(wdb, wtok, {"work_session_uid": start["work_session_uid"], "tick_at": HFL(10).isoformat()})
A._db_work_tick(wdb, wtok, {"work_session_uid": start["work_session_uid"], "tick_at": (HFL(10) + dt.timedelta(minutes=1)).isoformat()})
done = A._db_work_finish(wdb, wtok, {"work_session_uid": start["work_session_uid"], "summary": "parandus valmis", "ended_at": (HFL(10) + dt.timedelta(minutes=2)).isoformat(), "result": "kept"})
invoice = A._db_invoice_lines(wdb, wtok, {"period": ["2026-06"], "hourly_rate": ["82"]})
A._db_ingest_events(wdb, wtok, [{"event_key": "work-e1", "tool": "Pi", "project": "/tmp/pp-finar-pi", "prompt_text": "tee issue 662", "started_at": HFL(10).isoformat(), "ended_at": (HFL(10) + dt.timedelta(minutes=1)).isoformat(), "duration_seconds": 60}])
practice = A._db_practice_summary(wdb, wtok, {"period": ["2026-06"]})
activity = A._db_activity_log(wdb, wtok, {"period": ["2026-06"]})
with A._db_connect(wdb) as conn:
    project_count = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
    issue_count = conn.execute("SELECT COUNT(*) AS c FROM issues").fetchone()["c"]
    session_count = conn.execute("SELECT COUNT(*) AS c FROM work_sessions").fetchone()["c"]
check("normaliseeritud tabelites on projekt/issue/session", project_count >= 1 and issue_count >= 1 and session_count == 1)
check("done arvutas minutid tickidest", done["minutes"] == 2)
check("invoice endpointi helper grupeerib work_item'i", len(invoice["lines"]) == 1 and invoice["lines"][0]["issue"] == "#662")
check("invoice evidence sisaldab work_session_uid väärtust", invoice["lines"][0]["evidence"]["work_session_uids"] == [start["work_session_uid"]])
check("invoice sisaldab aega, hinda ja summat", invoice["lines"][0]["time"] == "00:02" and invoice["lines"][0]["amount"] == 2.73)
check("praktikavaade genereerib päeva", len(practice["days"]) == 1 and "pp-finar" in practice["days"][0]["text"])
check("activity endpoint helper näitab sessioone ja prompt-evente", len(activity["sessions"]) == 1 and len(activity["prompt_events"]) == 1)
check("activity sisaldab serveri work_session_uid väärtust", activity["sessions"][0]["work_session_uid"] == start["work_session_uid"])

# ============ TEST 44: lokaalse agendi DB hoiab work_session_uid ============
print("TEST 44: lokaalse agendi SQLite DB salvestab aktiivse work_session_uid")
A.LOCAL_DB.unlink(missing_ok=True)
A.WORK_STATE_FILE.write_text(json.dumps({"sessions": [{
    "work_session_uid": "ws_test_uid", "work_session_id": 42, "work_item_id": 7,
    "project_key": "github.com/puhastusproff/pp-finar", "issue_key": "662",
    "checkout_id": "co-local", "tool": "pi", "client_id": "client-test",
    "summary": "kohalik sessioon", "status": "active", "started_at": HFL(11).isoformat(),
}]}), encoding="utf-8")
active_local = A._active_work_sessions(tool="pi", checkout_id="co-local")
with A._local_db_connect() as conn:
    local_row = conn.execute("SELECT work_session_uid, status FROM local_work_sessions WHERE work_session_uid = 'ws_test_uid'").fetchone()
check("legacy snapshot imporditi local.db-sse", local_row is not None and local_row["status"] == "active")
check("aktiivne sessioon leitakse UID järgi", len(active_local) == 1 and active_local[0]["work_session_uid"] == "ws_test_uid")
A._save_work_state({"sessions": [{**active_local[0], "status": "done", "ended_at": HFL(12).isoformat()}]})
check("done sessioon ei ole enam aktiivne", A._active_work_sessions(tool="pi", checkout_id="co-local") == [])

# ============ TEST 45: serveri tegevuste leht olemas ============
print("TEST 45: serveri tegevuste HTML leht ja link päevavaatest")
activity_page = A._activity_page_html()
start_page = A._start_page_html()
check("activity leht kutsub /api/activity endpointi", "/api/activity" in activity_page and "work_session_uid" in activity_page)
check("activity leht kasutab login cookie authi", "Server token" not in activity_page and "/api/me" in activity_page and "/api/logout" in activity_page)
check("login leht postitab /api/login endpointi", "/api/login" in A._login_page_html() and "password" in A._login_page_html())
check("päevavaates on link serveri tegevustele", "Server tegevused" in start_page and "location.href='/activity'" in start_page)

# ============ TEST 46: server client saadab explicit User-Agent ============
print("TEST 46: server client kasutab explicit User-Agent headerit")
headers = A._server_headers({"Content-Type": "application/json"})
check("server headerites on User-Agent", headers.get("User-Agent", "").startswith("aitrack/"))
check("server headerid säilitavad Content-Type", headers.get("Content-Type") == "application/json")

# ============ TEST 47: brauseri login parool ja web session ============
print("TEST 47: serveri brauseri login loob sessiooni ilma API tokenit avaldamata")
ldb = CFG / "server-login-test.db"
ldb.unlink(missing_ok=True)
ltok = A._db_add_user(ldb, "loginuser")
A._db_set_user_password(ldb, "loginuser", "väga-salajane-123")
login = A._db_login(ldb, "loginuser", "väga-salajane-123")
with A._db_connect(ldb) as conn:
    session_user = A._db_user_by_session(ldb, login["session_token"])
check("õige parool loob web sessioni", login.get("ok") is True and login.get("user", {}).get("name") == "loginuser")
check("web session mapib kasutaja tokenile", session_user is not None and session_user["token"] == ltok)
check("vale parool ei valideeru", A._verify_password("vale", session_user["password_hash"]) is False)

print(f"\n==== TULEMUS: {PASS} läbitud, {FAIL} ebaõnnestunud ====")
sys.exit(1 if FAIL else 0)
