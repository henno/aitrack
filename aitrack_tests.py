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
print("TEST 14: _engine_cmd — pi/claude stdin, codex/gemini argv")
c_cmd, c_in = A._engine_cmd("claude", "/x/claude", "PROMPT", "")
check("claude: prompt stdin-i, mitte argv", c_cmd == ["/x/claude", "-p"] and c_in == "PROMPT")
p_cmd, p_in = A._engine_cmd("pi", "/x/pi", "PROMPT", "")
check("pi: prompt stdin-i ja ilma tööriistadeta", p_cmd == ["/x/pi", "-p", "--no-tools", "--no-context-files", "--no-session"] and p_in == "PROMPT")
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
A._db_ingest_rows(sdb, tok, [["2026-06-16", "11:00–12:00", "(1 prompt, automaatkokkuvõte puudub)", "(1 prompt, automaatkokkuvõte puudub)", "Ei olnud", "Ei olnud", "Pi"]], ["k:2026-06-16T11:00:00+00:00|__hour__"])
A._db_ingest_events(sdb, tok, [{"event_key": "e2", "tool": "Pi", "project": "/proj", "prompt_text": "tee nähtav päevavaate kokkuvõte", "started_at": HFL(11).isoformat(), "ended_at": HFL(11).isoformat(), "duration_seconds": 60}])
prompt_day_rows = A._db_rows_for_day(sdb, tok, "2026-06-16")
with A._db_connect(sdb) as conn:
    ev_count = conn.execute("SELECT COUNT(*) AS c FROM prompt_events").fetchone()["c"]
check("server salvestas prompt-eventi", ev_count == 2)
check("serveri päevavaade asendab prompt placeholderi lihtrahva tekstiga", any("Promptid:" not in r[3] and "automaatkokkuvõte" not in r[3] and not str(r[2]).startswith("Praktika -") for r in prompt_day_rows if r[1] == "11:00–12:00"))
check("serveri päevavaade tuletab promptidest uued teadmised", any(r[5] != A._NA for r in prompt_day_rows if r[1] == "11:00–12:00"))
check("praktikapäeviku heuristika täidab takistuse/teadmise", A._infer_takistus_from_texts(["paranda activity mittekuvamine"]) != A._NA and A._infer_teadmine_from_texts(["selgita heartbeat mudelit"]) != A._NA)
check("praktikapäeviku takistuse heuristika ei pea failiteed/faili veaks", A._infer_takistus_from_texts(["näita activity vaates failitee issue all", "ava claude.md fail"]) == A._NA)
learn_tail = A._infer_teadmine_from_texts(["kas tailscale töötab?", "kuidas ta saab minu võrku tulla?"])
learn_gnome = A._infer_teadmine_from_texts(["mis gnome mul on?", "tõmba https://github.com/daveprowse/Draw-On-Gnome"])
check("praktikapäeviku teadmise heuristika annab loetava teksti", "Selgus, kas" not in learn_tail and "Tailscale" in learn_tail and "https://" not in learn_gnome)
plain = A._plain_day_summary_from_texts(["aitrack uuenda globaalseid agent juhiseid", "aitrack work start ja tick käsuahel", "server login kasutajatele"], ["aitrack"])
check("praktikapäeviku fallback kasutab projekti nime ja ei kuva toorprompti", "aitrack uuenda" not in plain["objekt"] and plain["objekt"].startswith("aitrack -"))
check("praktikapäeviku merge tunneb toorpromptliku lahtri ära", A._rawish_day_text("aitrack uuenda juhiseid; aitrack work start käsuahel"))

# ============ TEST 42: repo URL normaliseerimine projektivõtmeks ============
print("TEST 42: repo URL normaliseerimine annab eri kloonidele sama project_key")
check("SSH ja HTTPS URL normaliseeruvad samaks",
      A._normalise_repo_url("git@github.com:Puhastusproff/pp-finar.git") ==
      A._normalise_repo_url("https://github.com/Puhastusproff/pp-finar.git") ==
      "github.com/puhastusproff/pp-finar")
check("branchist leitakse issue number", A._issue_from_branch("fix/662-asendaja-puhadetasu") == "662")
check("issue combobox labelist leitakse issue number", A._normalise_issue_key("662 - Asendaja pühadetasu") == "662")
check("bash raw-eventist tuletatakse päris tööliik", A._raw_event_display_tool("bash", {"payload": {"tool_input": {"command": "docker compose up -d && curl -I https://x"}}}) == "docker compose" and A._raw_event_display_tool("bash", {"payload": {"tool_input": {"command": "python3 aitrack_tests.py"}}}) == "testid" and A._raw_event_display_tool("bash", {"payload": {"tool_input": {"command": "ssh root@example uptime"}}}) == "ssh")
hook_allowed = CFG / "hook-allowed"
hook_other = CFG / "hook-other"
(hook_allowed / "sub").mkdir(parents=True, exist_ok=True)
hook_other.mkdir(parents=True, exist_ok=True)
A.save_projects([str(hook_allowed)])
check("Pi hook logib ainult aitrack allowlistis oleva projekti", A._hook_project_tracked({"cwd": str(hook_allowed / "sub"), "local_path": str(hook_allowed)}) is True and A._hook_project_tracked({"cwd": str(hook_other), "local_path": str(hook_other)}) is False)

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
A._db_ingest_rows(wdb, wtok, [["2026-06-16", "10:00–11:00", "(2 prompti, automaatkokkuvõte puudub)", "(2 prompti, automaatkokkuvõte puudub)", "Ei olnud", "Ei olnud", "Pi"]], ["k:2026-06-16T10:00:00+00:00|__hour__"])
day_rows_with_work = A._db_rows_for_day(wdb, wtok, "2026-06-16")
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
check("activity sisaldab checkouti failiteed", activity["sessions"][0]["local_path"] == "/tmp/pp-finar-pi")
check("serveri päevavaade asendab automaatkokkuvõtte placeholderi work-session kokkuvõttega", "parandus valmis" in day_rows_with_work[0][3])
check("serveri päevavaade tuletab work-sessionist uued teadmised", day_rows_with_work[0][5] != A._NA)

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
check("activity leht näitab projekti all failiteed", "x.local_path || x.cwd" in activity_page and "class=\"small path\"" in activity_page)
check("activity leht kasutab login cookie authi", "Server token" not in activity_page and "/api/me" in activity_page and "/api/logout" in activity_page)
check("activity leht sisaldab raw event timeline'i", "Raw eventid" in activity_page and "renderRawEvents" in activity_page and "raw_events" in activity_page)
check("activity filtrid jõustuvad automaatselt ja status on nupud", "setSessionStatusFilter('active')" in activity_page and "scheduleActivityLoad" in activity_page and ">Ava</button>" not in activity_page)
check("activity kuupäevafilter toetab kalendri vahemikku", "datePicker" in activity_page and "selectDateRangeDay" in activity_page and "params.set('from', dateRangeStart)" in activity_page and "params.set('to', dateRangeEnd)" in activity_page)
server_start_page = A._start_page_html(server_mode=True)
check("serveri päevavaade kasutab cookie authi, mitte tokenivälja", "Server token" not in server_start_page and "credentials:'same-origin'" in server_start_page and "const SERVER_MODE = true" in server_start_page)
check("serveri päevavaate Abi asemel on kasutaja nupp", "Kasutaja" in server_start_page and ">Abi<" not in server_start_page and "/account" in server_start_page)
check("serveri päevavaates saab admin kasutajat valida", "userSelect" in server_start_page and "/api/activity/filters" in server_start_page and "selectedUserParam" in server_start_page)
account_page = A._account_page_html()
check("kasutaja lehel saab parooli muuta", "/api/me/password" in account_page and "current-password" in account_page and "new-password" in account_page)
check("kasutaja lehel on kolme OS-i installikäsk", "/api/install-code" in account_page and "install-client.sh" in account_page and "install-client.ps1" in account_page and "Linux" in account_page and "macOS" in account_page and "Windows" in account_page)
check("Linux/macOS installikäsk sobib ka fish shellile", "bash <(" not in account_page and "| bash -s -- --code" in account_page)
check("installiskriptid ühendavad serveriga ja paigaldavad Pi extensioni", "install --minute-tracking --pi-extension" in A._install_client_sh("https://aitrack.example.com") and "Install-AitrackClient" in A._install_client_ps1("https://aitrack.example.com"))
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
change = A._db_change_user_password(ldb, int(session_user["id"]), "väga-salajane-123", "uus-salajane-456")
login2 = A._db_login(ldb, "loginuser", "uus-salajane-456")
wrong_change = False
try:
    A._db_change_user_password(ldb, int(session_user["id"]), "väga-salajane-123", "kolmas-salajane-789")
except PermissionError:
    wrong_change = True
check("kasutaja saab praeguse parooliga parooli muuta", change.get("ok") is True and login2.get("ok") is True)
check("vana parooliga muutmine enam ei õnnestu", wrong_change is True)
install_code = A._db_create_install_code(ldb, ltok, ip="127.0.0.1", req_path="/api/install-code")
install_exchange = A._db_exchange_install_code(ldb, install_code["code"], ip="127.0.0.1", req_path="/api/install-code/exchange")
install_reuse_failed = False
try:
    A._db_exchange_install_code(ldb, install_code["code"], ip="127.0.0.1", req_path="/api/install-code/exchange")
except PermissionError:
    install_reuse_failed = True
check("installikood vahetub tokeniks ja on ühekordne", install_exchange.get("token") == ltok and install_reuse_failed is True)
admin_tok = A._db_add_user(ldb, "admin2", "admin")
target_tok = A._db_add_user(ldb, "targetuser")
A._db_ingest_rows(ldb, target_tok, [["2026-06-16", "09:00–10:00", "target objekt", "target saavutus", "Ei olnud", "target teadmine", "Pi"]], ["k:target:1"])
admin_target_rows = A._db_rows_for_day(ldb, admin_tok, "2026-06-16", None, {"user": ["targetuser"]})
admin_target_days = A._db_days(ldb, admin_tok, {"user": ["targetuser"]})
non_admin_rows = A._db_rows_for_day(ldb, target_tok, "2026-06-16", None, {"user": ["admin2"]})
check("admin saab päevavaates teise kasutaja praktikat vaadata", admin_target_rows and admin_target_rows[0][2] == "target objekt" and "2026-06-16" in admin_target_days)
check("tavakasutaja ei saa user parameetriga teise päevikut lugeda", non_admin_rows and non_admin_rows[0][2] == "target objekt")

# ============ TEST 48: serveri rate-limit ja IP-ban abid ============
print("TEST 48: rate limiter tuvastab kahtlased päringud ja login failure ban'i")
check(".env päring on kahtlane", A._is_suspicious_request_path("/.env") is True)
check("/activity ei ole kahtlane", A._is_suspicious_request_path("/activity") is False)
check("CF/XFF IP normaliseerub", A._normalise_request_ip("203.0.113.7, 10.0.0.1") == "203.0.113.7")
A._AitrackHandler._login_failures.clear()
A._AitrackHandler._banned_until.clear()
dummy = object.__new__(A._AitrackHandler)
dummy.headers = {}
dummy.client_address = ("203.0.113.77", 12345)
for _ in range(A.LOGIN_FAIL_MAX - 1):
    banned = dummy._record_login_failure()
check("login failure ei banni enne limiiti", banned is False and "203.0.113.77" not in A._AitrackHandler._banned_until)
banned = dummy._record_login_failure()
check("login failure ban rakendub limiidil", banned is True and A._AitrackHandler._banned_until.get("203.0.113.77", 0) > 0)

# ============ TEST 49: raw_events MVP ==========
print("TEST 49: raw_events tabel, /api/events ingest helper ja export")
rdb = CFG / "server-raw-events-test.db"
rdb.unlink(missing_ok=True)
rtok = A._db_add_user(rdb, "rawuser")
rpayload = {
    "project": {"project_key": "github.com/parkkarl/aitrack", "name": "aitrack", "local_path": "/tmp/aitrack"},
    "issue": {"provider": "github", "issue_key": "662", "title": "Asendaja pühadetasu"},
    "work": {"title": "raw events test", "summary": "raw events"},
    "session": {"client_id": "client-raw", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/aitrack", "cwd": "/tmp/aitrack"},
    "started_at": HFL(12).isoformat(),
}
rstart = A._db_work_start(rdb, rtok, rpayload)
raw_event = {
    "event_key": "raw-e1",
    "event_type": "before_tool_call",
    "tool_name": "read",
    "work_session_uid": rstart["work_session_uid"],
    "agent_uid": "agent-1",
    "parent_agent_uid": "root-agent",
    "tool_call_id": "tool-1",
    "occurred_at_utc": HFL(12).isoformat(),
    "payload": {"path": "aitrack.py", "token": "SALA", "output": "x" * 20000},
}
raw_res1 = A._db_ingest_events(rdb, rtok, [raw_event])
raw_res2 = A._db_ingest_events(rdb, rtok, [raw_event])
raw_export = A._db_export_raw_events(rdb, rtok, {"period": ["2026-06"], "event_type": ["before_tool_call"]})
raw_activity = A._db_activity_log(rdb, rtok, {"period": ["2026-06"], "stale_minutes": ["9999"], "stuck_minutes": ["9999"]})
raw_activity_filtered = A._db_activity_log(rdb, rtok, {"period": ["2026-06"], "project": ["parkkarl"], "user": ["raw"], "stale_minutes": ["9999"], "stuck_minutes": ["9999"]})
raw_activity_issue_filtered = A._db_activity_log(rdb, rtok, {"period": ["2026-06"], "issue": ["662 - Asendaja pühadetasu"], "stale_minutes": ["9999"], "stuck_minutes": ["9999"]})
raw_filter_options = A._db_activity_filter_options(rdb, rtok, {"project": ["parkkarl"]})
with A._db_connect(rdb) as conn:
    raw_count = conn.execute("SELECT COUNT(*) AS c FROM raw_events").fetchone()["c"]
    prompt_count = conn.execute("SELECT COUNT(*) AS c FROM prompt_events").fetchone()["c"]
exported_raw = raw_export["events"][0] if raw_export["events"] else {}
check("raw event sisestus on idempotentne", raw_res1["raw_events"]["inserted"] == 1 and raw_res2["raw_events"]["inserted"] == 0 and raw_count == 1)
check("raw event ei tekita legacy prompt-eventi", prompt_count == 0)
check("raw export leiab sündmuse ja seob work_session_uid-ga", raw_export["count"] == 1 and exported_raw.get("work_session_uid") == rstart["work_session_uid"] and exported_raw.get("work_session_id") == rstart["work_session_id"])
check("activity helper tagastab raw event timeline'i", len(raw_activity.get("raw_events", [])) == 1 and any(x.get("type") == "raw_event" for x in raw_activity.get("activity", [])))
check("raw event activity tekst kasutab prompti/sessiooni kokkuvõtet, mitte event_type", raw_activity.get("raw_events", [{}])[0].get("summary") == "raw events")
check("activity filter otsib projekti ja kasutajat osalise tekstiga", len(raw_activity_filtered.get("raw_events", [])) == 1)
check("activity issue filter oskab combobox labelit kasutada", len(raw_activity_issue_filtered.get("raw_events", [])) == 1)
check("activity autocomplete valikud sisaldavad projekti, kasutajat ja issue pealkirja", any("parkkarl" in p.get("project_key", "") for p in raw_filter_options.get("projects", [])) and any(u.get("name") == "rawuser" for u in raw_filter_options.get("users", [])) and any(i.get("issue_key") == "662" and "Asendaja" in i.get("title", "") for i in raw_filter_options.get("issues", [])))
check("raw payload on piiratud ja saladus redigeeritud", "SALA" not in exported_raw.get("payload_json", "") and len(exported_raw.get("payload_json", "")) <= A.RAW_EVENT_PAYLOAD_MAX_BYTES)
check("raw export filter töötab", A._db_export_raw_events(rdb, rtok, {"period": ["2026-06"], "event_type": ["after_tool_call"]})["count"] == 0)

# ============ TEST 50: active interval rollup käsitleb sleep-gap'i ==========
print("TEST 50: active interval rollup tihendab tickid ja jätab sleep-gap'i auguks")
idb = CFG / "server-intervals-test.db"
idb.unlink(missing_ok=True)
itok = A._db_add_user(idb, "intervaluser")
ipayload = {
    "project": {"project_key": "github.com/parkkarl/aitrack", "name": "aitrack", "local_path": "/tmp/aitrack"},
    "work": {"title": "interval test", "summary": "rollup"},
    "session": {"client_id": "client-interval", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/aitrack", "cwd": "/tmp/aitrack"},
    "started_at": HFL(10).isoformat(),
}
istart = A._db_work_start(idb, itok, ipayload)
for minute in (0, 1, 10):
    A._db_work_tick(idb, itok, {"work_session_uid": istart["work_session_uid"], "tick_at": (HFL(10) + dt.timedelta(minutes=minute)).isoformat()})
ifinish = A._db_work_finish(idb, itok, {"work_session_uid": istart["work_session_uid"], "ended_at": (HFL(10) + dt.timedelta(minutes=11)).isoformat(), "summary": "rollup valmis", "result": "kept"})
interval_export = A._db_export_active_intervals(idb, itok, {"period": ["2026-06"]})
work_export = A._db_export_work_sessions(idb, itok, {"period": ["2026-06"]})
intervals = sorted(interval_export.get("intervals", []), key=lambda x: x["start_minute_utc"])
check("finish tagastab active_intervals", len(ifinish.get("active_intervals", [])) == 2)
check("sleep-gap jagab tickid kaheks intervalliks", len(intervals) == 2 and [i["minutes"] for i in intervals] == [2, 1])
check("active interval export summeerib ainult tickidega minutid", interval_export["minutes"] == 3)
check("active interval export sisaldab work_session_uid", intervals and intervals[0]["work_session_uid"] == istart["work_session_uid"])
check("work-sessions export sisaldab interval minuteid", work_export["count"] == 1 and work_export["sessions"][0]["interval_minutes"] == 3)

# ============ TEST 51: watchdog märgib stale sessiooni ==========
print("TEST 51: watchdog märgib heartbeatita aktiivse sessiooni stale olekusse")
stdb = CFG / "server-watchdog-test.db"
stdb.unlink(missing_ok=True)
stok = A._db_add_user(stdb, "watchuser")
A._now_utc = lambda: HFL(10)
st_payload = {
    "project": {"project_key": "github.com/parkkarl/aitrack", "name": "aitrack", "local_path": "/tmp/aitrack"},
    "work": {"title": "watchdog test", "summary": "watchdog"},
    "session": {"client_id": "client-watch", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/aitrack", "cwd": "/tmp/aitrack"},
    "started_at": HFL(10).isoformat(),
}
st_start = A._db_work_start(stdb, stok, st_payload)
A._db_work_tick(stdb, stok, {"work_session_uid": st_start["work_session_uid"], "tick_at": HFL(10).isoformat()})
A._now_utc = lambda: HFL(10) + dt.timedelta(minutes=20)
stale = A._db_watchdog(stdb, stok, {"stale_minutes": 5})
stale_again = A._db_watchdog(stdb, stok, {"stale_minutes": 5})
with A._db_connect(stdb) as conn:
    st_row = conn.execute("SELECT status FROM work_sessions WHERE session_uid = ?", (st_start["work_session_uid"],)).fetchone()
    stale_events = conn.execute("SELECT COUNT(*) AS c FROM raw_events WHERE event_type = 'session_stale'").fetchone()["c"]
check("watchdog märgib sessiooni stale", stale["marked_count"] == 1 and st_row["status"] == "stale")
check("watchdog lisab session_stale raw eventi", stale_events == 1 and stale_again["marked_count"] == 0)
check("stale sessioon ei ole enam aktiivses work/status vaates", A._db_work_status(stdb, stok) == [])
A._now_utc = lambda: dt.datetime.now(dt.timezone.utc)

# ============ TEST 51B: activity auto-watchdog ja status filter ==========
print("TEST 51B: activity märgib live-aknas vanad active sessioonid stale'iks")
adb = CFG / "server-activity-watchdog-test.db"
adb.unlink(missing_ok=True)
atok = A._db_add_user(adb, "activitywatch")
A._now_utc = lambda: HFL(9)
astart = A._db_work_start(adb, atok, {
    "project": {"project_key": "github.com/parkkarl/aitrack", "name": "aitrack", "local_path": "/tmp/aitrack"},
    "work": {"title": "activity watchdog", "summary": "activity watchdog"},
    "session": {"client_id": "client-activity-watch", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/aitrack", "cwd": "/tmp/aitrack"},
    "started_at": (HFL(8) + dt.timedelta(minutes=40)).isoformat(),
})
A._db_ingest_event_endpoint(adb, atok, {"work_session_uid": astart["work_session_uid"], "agent_uid": "agent-watch", "tool": "pi", "prompt": "watchdog prompt", "occurred_at_utc": (HFL(8) + dt.timedelta(minutes=41)).isoformat()}, "prompt_started")
activity_stale = A._db_activity_log(adb, atok, {"date": ["2026-06-16"], "timezone": ["UTC"], "status": ["stale"]})
activity_active = A._db_activity_log(adb, atok, {"date": ["2026-06-16"], "timezone": ["UTC"], "status": ["active"]})
check("activity auto-watchdog muudab vana active sessiooni stale'iks", len(activity_stale.get("sessions", [])) == 1 and activity_stale["sessions"][0]["status"] == "stale" and len(activity_active.get("sessions", [])) == 0)
check("status filter rakendub ka prompt-eventidele", len(activity_stale.get("prompt_events", [])) == 1 and len(activity_active.get("prompt_events", [])) == 0)
A._now_utc = lambda: dt.datetime.now(dt.timezone.utc)

# ============ TEST 52: event endpointid seovad prompti ja tool progressi ==========
print("TEST 52: event endpointid seovad prompti work_sessioniga ja uuendavad tool progressi")
edb = CFG / "server-event-endpoint-test.db"
edb.unlink(missing_ok=True)
etok = A._db_add_user(edb, "eventuser")
epayload = {
    "project": {"project_key": "github.com/parkkarl/aitrack", "name": "aitrack", "local_path": "/tmp/aitrack"},
    "work": {"title": "event endpoint test", "summary": "events"},
    "session": {"client_id": "client-event", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/aitrack", "cwd": "/tmp/aitrack", "agent_uid": "agent-a"},
    "started_at": HFL(9).isoformat(),
}
estart = A._db_work_start(edb, etok, epayload)
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-a", "tool": "pi", "prompt": "palun tee test", "occurred_at_utc": HFL(9).isoformat()}, "prompt_started")
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-a", "tool_name": "read", "tool_call_id": "tc-1", "occurred_at_utc": (HFL(9) + dt.timedelta(minutes=3)).isoformat()}, "before_tool_call")
activity_agent = A._db_activity_log(edb, etok, {"period": ["2026-06"], "agent_uid": ["agent-a"]})
with A._db_connect(edb) as conn:
    erow = conn.execute("SELECT current_tool_name, current_tool_call_id, agent_uid FROM work_sessions WHERE session_uid = ?", (estart["work_session_uid"],)).fetchone()
    prompt_link = conn.execute("SELECT work_session_id FROM prompt_events WHERE event_key != ''").fetchone()
check("prompt/start endpoint tekitab prompt_eventi ja seob sessiooniga", prompt_link is not None and prompt_link["work_session_id"] == estart["work_session_id"])
check("tool-start endpoint uuendab jooksva tooli välja", erow["current_tool_name"] == "read" and erow["current_tool_call_id"] == "tc-1" and erow["agent_uid"] == "agent-a")
check("activity agent filter leiab raw eventid", len(activity_agent.get("raw_events", [])) >= 2 and all(x.get("agent_uid") == "agent-a" for x in activity_agent.get("raw_events", [])))
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-a", "tool_name": "read", "tool_call_id": "tc-1", "occurred_at_utc": (HFL(9) + dt.timedelta(minutes=4)).isoformat()}, "after_tool_call")
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-a", "summary": "muudatused valmis", "occurred_at_utc": (HFL(9) + dt.timedelta(minutes=5)).isoformat()}, "agent_finished")
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-a", "summary": "prompti kokkuvõte", "occurred_at_utc": (HFL(9) + dt.timedelta(minutes=6)).isoformat()}, "prompt_finished")
activity_done = A._db_activity_log(edb, etok, {"period": ["2026-06"], "agent_uid": ["agent-a"]})
with A._db_connect(edb) as conn:
    done_row = conn.execute("SELECT current_tool_name, summary, status_detail FROM work_sessions WHERE session_uid = ?", (estart["work_session_uid"],)).fetchone()
    done_prompt = conn.execute("SELECT prompt_text FROM prompt_events WHERE prompt_text = 'prompti kokkuvõte'").fetchone()
check("tool-end endpoint puhastab jooksva tooli", done_row["current_tool_name"] == "")
check("done event uuendab sessiooni kokkuvõtte", done_row["summary"] == "prompti kokkuvõte" and str(done_row["status_detail"]).startswith("done:"))
check("prompt-done event salvestab tehtu kokkuvõtte prompt-eventina", done_prompt is not None)
check("activity raw done event näitab kokkuvõtet", any(x.get("event_type") == "agent_finished" and x.get("summary") == "muudatused valmis" for x in activity_done.get("raw_events", [])))

# ============ TEST 53: watchdog tuvastab stuck tool-call'i ==========
print("TEST 53: watchdog märgib pika poolelioleva tool-call'i stuck olekusse")
sdb2 = CFG / "server-stuck-test.db"
sdb2.unlink(missing_ok=True)
stok2 = A._db_add_user(sdb2, "stuckuser")
A._now_utc = lambda: HFL(8)
spayload = {
    "project": {"project_key": "github.com/parkkarl/aitrack", "name": "aitrack", "local_path": "/tmp/aitrack"},
    "work": {"title": "stuck test", "summary": "stuck"},
    "session": {"client_id": "client-stuck", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/aitrack", "cwd": "/tmp/aitrack"},
    "started_at": HFL(8).isoformat(),
}
sstart = A._db_work_start(sdb2, stok2, spayload)
A._db_ingest_events(sdb2, stok2, [{"event_key": "stuck-tool-start", "event_type": "before_tool_call", "work_session_uid": sstart["work_session_uid"], "tool_name": "bash", "tool_call_id": "tc-stuck", "occurred_at_utc": HFL(8).isoformat()}])
A._now_utc = lambda: HFL(8) + dt.timedelta(minutes=20)
stuck = A._db_watchdog(sdb2, stok2, {"stale_minutes": 100, "stuck_minutes": 5})
with A._db_connect(sdb2) as conn:
    stuck_row = conn.execute("SELECT status FROM work_sessions WHERE session_uid = ?", (sstart["work_session_uid"],)).fetchone()
    stuck_events = conn.execute("SELECT COUNT(*) AS c FROM raw_events WHERE event_type = 'agent_marked_stuck'").fetchone()["c"]
check("watchdog märgib sessiooni stuck", stuck["marked_stuck_count"] == 1 and stuck_row["status"] == "stuck")
check("watchdog lisab agent_marked_stuck raw eventi", stuck_events == 1)
A._now_utc = lambda: dt.datetime.now(dt.timezone.utc)

# ============ TEST 54: cleanup kustutab ainult lubatud retention andmed ==========
print("TEST 54: cleanup dry-run/apply ja minute_ticks ainult pärast rollupit")
cdb = CFG / "server-cleanup-test.db"
cdb.unlink(missing_ok=True)
ctok = A._db_add_user(cdb, "cleanupuser")
A._now_utc = lambda: dt.datetime(2026, 7, 1, tzinfo=UTC)
cpayload = {
    "project": {"project_key": "github.com/parkkarl/aitrack", "name": "aitrack", "local_path": "/tmp/aitrack"},
    "work": {"title": "cleanup test", "summary": "cleanup"},
    "session": {"client_id": "client-clean", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/aitrack", "cwd": "/tmp/aitrack"},
    "started_at": HFL(7).isoformat(),
}
cstart = A._db_work_start(cdb, ctok, cpayload)
old_minute = dt.datetime(2026, 1, 1, 8, 0, tzinfo=UTC).isoformat()
with A._db_connect(cdb) as conn:
    wsrow = conn.execute("SELECT work_item_id, user_id FROM work_sessions WHERE id = ?", (cstart["work_session_id"],)).fetchone()
    conn.execute("INSERT INTO minute_ticks(work_session_id, work_item_id, user_id, minute_start_utc, source, created_at) VALUES (?, ?, ?, ?, 'test', ?)", (cstart["work_session_id"], wsrow["work_item_id"], wsrow["user_id"], old_minute, old_minute))
    conn.execute("INSERT INTO raw_events(user_id, work_session_id, work_session_uid, event_type, dedup_key, occurred_at_utc, received_at_utc, payload_json) VALUES (?, ?, ?, 'old', 'old-clean', ?, ?, '{}')", (wsrow["user_id"], cstart["work_session_id"], cstart["work_session_uid"], old_minute, old_minute))
dry = A._db_cleanup(cdb, ctok, {"older_than": "90d"})
apply1 = A._db_cleanup(cdb, ctok, {"older_than": "90d", "apply": True})
with A._db_connect(cdb) as conn:
    left_ticks_before_rollup = conn.execute("SELECT COUNT(*) AS c FROM minute_ticks").fetchone()["c"]
    conn.execute("UPDATE work_sessions SET rollup_finalized_at = ? WHERE id = ?", (A._now_utc().isoformat(), cstart["work_session_id"]))
apply2 = A._db_cleanup(cdb, ctok, {"older_than": "90d", "apply": True})
with A._db_connect(cdb) as conn:
    left_ticks_after_rollup = conn.execute("SELECT COUNT(*) AS c FROM minute_ticks").fetchone()["c"]
    left_raw = conn.execute("SELECT COUNT(*) AS c FROM raw_events").fetchone()["c"]
check("cleanup dry-run loendab vanad read", dry["dry_run"] and dry["raw_events"] == 1 and dry["minute_ticks"] == 0)
check("cleanup kustutab raw eventid, aga mitte rollupita ticke", apply1["raw_events"] == 1 and left_raw == 0 and left_ticks_before_rollup == 1)
check("cleanup kustutab tickid pärast rollupit", apply2["minute_ticks"] == 1 and left_ticks_after_rollup == 0)
A._now_utc = lambda: dt.datetime.now(dt.timezone.utc)

# ============ TEST 55: active interval export lõikab perioodipiire ==========
print("TEST 55: active interval export lõikab intervalli päeva piiridesse")
clipdb = CFG / "server-clip-test.db"
clipdb.unlink(missing_ok=True)
cltok = A._db_add_user(clipdb, "clipuser")
clip_payload = {
    "project": {"project_key": "github.com/parkkarl/aitrack", "name": "aitrack", "local_path": "/tmp/aitrack"},
    "work": {"title": "clip test", "summary": "clip"},
    "session": {"client_id": "client-clip", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/aitrack", "cwd": "/tmp/aitrack"},
    "started_at": dt.datetime(2026, 6, 16, 23, 50, tzinfo=UTC).isoformat(),
}
clstart = A._db_work_start(clipdb, cltok, clip_payload)
with A._db_connect(clipdb) as conn:
    wsrow = conn.execute("SELECT work_item_id, user_id FROM work_sessions WHERE id = ?", (clstart["work_session_id"],)).fetchone()
    conn.execute("INSERT INTO work_session_active_intervals(work_session_id, work_item_id, user_id, start_minute_utc, end_minute_utc, minutes, source, created_at) VALUES (?, ?, ?, ?, ?, 20, 'test', ?)", (clstart["work_session_id"], wsrow["work_item_id"], wsrow["user_id"], dt.datetime(2026, 6, 16, 23, 50, tzinfo=UTC).isoformat(), dt.datetime(2026, 6, 17, 0, 10, tzinfo=UTC).isoformat(), HFL(0).isoformat()))
clip_export = A._db_export_active_intervals(clipdb, cltok, {"date": ["2026-06-17"]})
clip_interval = clip_export["intervals"][0] if clip_export["intervals"] else {}
check("interval export loeb ainult perioodi sisse jäävad minutid", clip_export["minutes"] == 10 and clip_interval.get("minutes") == 10 and clip_interval.get("start_minute_utc").startswith("2026-06-17T00:00:00"))

# ============ TEST 56: timezone bounds lõikavad kohaliku päeva järgi ==========
print("TEST 56: timezone query bounds kasutavad kohalikku päeva/perioodi")
tz_start, tz_end, _ = A._activity_bounds({"date": ["2026-06-17"], "timezone": ["Europe/Tallinn"]})
month_start, month_end, _ = A._period_bounds({"period": ["2026-06"], "timezone": ["Europe/Tallinn"]})
check("Tallinna päev algab UTC-s eelmisel õhtul", tz_start.startswith("2026-06-16T21:00:00") and tz_end.startswith("2026-06-17T21:00:00"))
check("Tallinna kuu piirid teisenduvad UTC-sse", month_start.startswith("2026-05-31T21:00:00") and month_end.startswith("2026-06-30T21:00:00"))

# ============ TEST 57: admin kasutajahaldus ja security_events ==========
print("TEST 57: admin API helperid haldavad kasutajaid ja auditit")
adb = CFG / "server-admin-test.db"
adb.unlink(missing_ok=True)
admintok = A._db_add_user(adb, "root", role="admin", password="rootpass123")
new_user = A._db_admin_add_user(adb, admintok, {"name": "worker", "role": "user", "password": "workerpass123"})
A._db_admin_set_user_password(adb, admintok, {"name": "worker", "password": "newpass123"})
login_res = A._db_login(adb, "worker", "newpass123")
revoked = A._db_admin_revoke_user_sessions(adb, admintok, {"name": "worker"})
users = A._db_admin_users(adb, admintok)
security = A._db_admin_security_events(adb, admintok, {})
check("admin saab kasutaja lisada ja parooli seada", new_user["user"]["name"] == "worker" and login_res["ok"])
check("admin saab web sessioonid tühistada", revoked["revoked"] == 1)
check("admin user list sisaldab uut kasutajat", any(u["name"] == "worker" for u in users["users"]))
check("security_events salvestab admin toimingud", security["count"] >= 2 and any(e["event_type"] == "admin_user_created" for e in security["events"]))

# ============ TEST 58: customer/contract/rate workflow ==========
print("TEST 58: customer, contract ja rate workflow")
wdb = CFG / "server-workflow-test.db"
wdb.unlink(missing_ok=True)
wtok = A._db_add_user(wdb, "workflow")
wstart = A._db_work_start(wdb, wtok, {
    "project": {"project_key": "github.com/example/acme", "name": "acme", "local_path": "/tmp/acme"},
    "work": {"title": "workflow", "summary": "workflow"},
    "session": {"client_id": "client-workflow", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/acme", "cwd": "/tmp/acme"},
    "started_at": HFL(6).isoformat(),
})
A._db_work_tick(wdb, wtok, {"work_session_uid": wstart["work_session_uid"], "tick_at": HFL(6).isoformat()})
customer = A._db_customer_add(wdb, "Acme OÜ")
assigned = A._db_project_assign_customer_direct(wdb, "github.com/example/acme", "Acme OÜ")
contract = A._db_contract_add(wdb, "Acme OÜ", "Põhileping")
rate = A._db_rate_add(wdb, contract["contract"]["id"], "2026-06-01", 82.0)
invoice = A._db_invoice_lines(wdb, wtok, {"period": ["2026-06"], "customer_id": [str(customer["customer"]["id"])]})
check("project assign-customer seob projekti kliendiga", assigned["customer"] == "Acme OÜ")
check("contract ja rate lisanduvad", contract["contract"]["id"] > 0 and rate["rate"]["hourly_rate"] == 82.0)
check("invoice endpoint on deprecate märgisega ja customer filter töötab", invoice.get("deprecated") is True and invoice["lines"] and invoice["lines"][0]["customer"] == "Acme OÜ")

# ============ TEST 59: agent tree raw eventidest ==========
print("TEST 59: activity tagastab agent/subagent tree")
treedb = CFG / "server-agent-tree-test.db"
treedb.unlink(missing_ok=True)
treetok = A._db_add_user(treedb, "treeuser")
tstart = A._db_work_start(treedb, treetok, {
    "project": {"project_key": "github.com/parkkarl/aitrack", "name": "aitrack", "local_path": "/tmp/aitrack"},
    "work": {"title": "tree", "summary": "tree"},
    "session": {"client_id": "client-tree", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/aitrack", "cwd": "/tmp/aitrack"},
    "started_at": HFL(5).isoformat(),
})
A._db_ingest_events(treedb, treetok, [
    {"event_key": "root-agent-event", "event_type": "agent_started", "work_session_uid": tstart["work_session_uid"], "agent_uid": "root-agent", "occurred_at_utc": HFL(5).isoformat()},
    {"event_key": "child-agent-event", "event_type": "subagent_started", "work_session_uid": tstart["work_session_uid"], "agent_uid": "child-agent", "parent_agent_uid": "root-agent", "tool_name": "subagent", "occurred_at_utc": HFL(5).isoformat()},
])
tree_activity = A._db_activity_log(treedb, treetok, {"period": ["2026-06"]})
root = tree_activity.get("agent_tree", [{}])[0]
check("agent tree sisaldab parent-child seost", root.get("agent_uid") == "root-agent" and root.get("children") and root["children"][0].get("agent_uid") == "child-agent")

# ============ TEST 60: activity detail modal ja event-detail helper ==========
print("TEST 60: activity detail modal laadib sündmuse toorandmed")
detdb = CFG / "server-detail-test.db"
detdb.unlink(missing_ok=True)
dettok = A._db_add_user(detdb, "detailuser")
detstart = A._db_work_start(detdb, dettok, {
    "project": {"project_key": "github.com/parkkarl/aitrack", "name": "aitrack", "local_path": "/tmp/aitrack"},
    "work": {"title": "detail", "summary": "detail"},
    "session": {"client_id": "client-detail", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/aitrack", "cwd": "/tmp/aitrack"},
    "started_at": HFL(4).isoformat(),
})
A._db_ingest_events(detdb, dettok, [{"event_key": "detail-raw", "event_type": "before_tool_call", "work_session_uid": detstart["work_session_uid"], "agent_uid": "agent-detail", "tool_name": "bash", "tool_call_id": "tc-detail", "occurred_at_utc": HFL(4).isoformat(), "payload": {"command": "echo detail"}}])
with A._db_connect(detdb) as conn:
    raw_id = conn.execute("SELECT id FROM raw_events WHERE event_key = 'detail-raw'").fetchone()["id"]
raw_detail = A._db_event_detail(detdb, dettok, {"type": ["raw_event"], "id": [str(raw_id)]})
session_detail = A._db_event_detail(detdb, dettok, {"type": ["work_session"], "work_session_uid": [detstart["work_session_uid"]]})
activity_html = A._activity_page_html()
check("event-detail tagastab raw event payload_json välja", raw_detail["detail"]["event_key"] == "detail-raw" and "echo detail" in raw_detail["detail"]["payload_json"])
check("event-detail tagastab work_session toorrea", session_detail["detail"]["session_uid"] == detstart["work_session_uid"])
check("activity HTML sisaldab detail modalit ja nuppe", "detailModal" in activity_html and "showDetail" in activity_html and "Toorandmed" in activity_html)
check("detail modal värvib JSON-i süntaksit", "syntaxHighlightJson" in activity_html and "json-key" in activity_html and "json-string" in activity_html)

print(f"\n==== TULEMUS: {PASS} läbitud, {FAIL} ebaõnnestunud ====")
sys.exit(1 if FAIL else 0)
