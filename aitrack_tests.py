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
    return f"STUB({len(prompts)})"

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
print("TEST 10: _fallback_summary on geneeriline (ei leki promptisisu)")
fb = A._fallback_summary(["SALAJANE API VÕTI sk-12345", "veel üks"])
check("ei sisalda toorest prompti", "SALAJANE" not in fb and "sk-12345" not in fb)
check("näitab promptide arvu", "2" in fb)

# ============ TEST 11: match_project — subpath jah, sibling ei ============
print("TEST 11: match_project tabab subpath'i, mitte sibling'it")
allow = ["/home/x/proj"]
check("subpath sobib", A.match_project("/home/x/proj/src", allow) == "/home/x/proj")
check("sibling EI sobi", A.match_project("/home/x/proj2", allow) is None)

# ============ TEST 12: backfill jätab juba-olemas tunni summeerimata (kuluvõit) ============
print("TEST 12: backfill ei summeeri tunde, mis on juba lehel")
reset_sink(); setup([REC(10), REC(11), REC(12)], state=json.dumps({"last_processed_hour": HFL(13).isoformat()}))
cfg = A.load_config(); allow = A.load_projects()
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
cfg = A.load_config(); allow = A.load_projects()
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

# ============ TEST 17: lokaalne CSV-sink — kirjutab + dedupib ============
print("TEST 17: lokaalne CSV-sink kirjutab ridu ja dedupib võtme järgi")
csvpath = CFG / "log.csv"
csvpath.unlink(missing_ok=True)
lcfg = {"sink": {"type": "local", "path": str(csvpath)}}
r1 = [["2026-06-16", "11:00–12:00", "proj", "Claude", "tegi asja"]]
k1 = ["k:2026-06-16T11:00:00+00:00|proj|abc123"]
ok1 = _REAL["append_rows"](r1, k1, lcfg)        # päris dispatch → lokaalne kirjutaja
ok2 = _REAL["append_rows"](r1, k1, lcfg)         # sama võti uuesti → ei tohi dubleerida
import csv as _csv
with csvpath.open(newline="", encoding="utf-8") as f:
    data_rows = [row for row in _csv.reader(f)]
check("kirjutamine õnnestus", ok1 and ok2)
check("päis + 1 andmerida (dedup töötab)", len(data_rows) == 2)
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
setup([REC(11)])
A.append_rows = _REAL["append_rows"]          # päris lokaalne kirjutaja
A.fetch_existing_keys = _REAL["fetch_existing_keys"]
A.summarize = lambda *a, **k: '=HYPERLINK("http://evil")'   # pahatahtlik kokkuvõte
A.save_config({"timezone": "UTC", "sink": {"type": "local", "path": str(csv2)}})
A.save_projects(["/proj"])
A._now_utc = lambda: dt.datetime(2026, 6, 16, 12, 15, tzinfo=UTC)
A.run_once(A.load_config(), A.load_projects())
content = csv2.read_text(encoding="utf-8")
check("kokkuvõte CSV-s ' prefiksiga", "'=HYPERLINK" in content)
check("kaitsmata =HYPERLINK rea/välja algust pole", "\n=HYPERLINK" not in content)

# ============ TEST 22: lokaalne CSV luuakse 0o600-ga ============
print("TEST 22: lokaalne CSV õigused 0600")
mode = oct(os.stat(csv2).st_mode)[-3:]
check("CSV režiim 0600", mode == "600")

# ============ TEST 23: _local_keys ignoreerib lühikest/võtmeta rida ============
print("TEST 23: _local_keys ei lange fantoomvõtmesse")
csv3 = CFG / "edited.csv"
csv3.write_text(
    "Kuupäev,Tund,Projekt,Tööriist,Töö kokkuvõte,_key\n"
    "2026-06-16,11:00–12:00,proj,Claude,tegi,k:2026-06-16T11:00:00+00:00|proj|aaa\n"
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

print(f"\n==== TULEMUS: {PASS} läbitud, {FAIL} ebaõnnestunud ====")
sys.exit(1 if FAIL else 0)
