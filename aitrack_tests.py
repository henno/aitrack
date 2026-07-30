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
_REAL = {n: getattr(A, n) for n in ("append_rows", "fetch_existing_keys", "collect_records", "collect_transcript_records")}

CFG = Path(os.environ["AITRACK_CONFIG_DIR"])
UTC = dt.timezone.utc
D = lambda h, m=30: dt.datetime(2026, 6, 16, h, m, tzinfo=UTC)  # noqa: E731
HFL = lambda h: dt.datetime(2026, 6, 16, h, 0, tzinfo=UTC)      # noqa: E731

SINK_KEYS = set()
SENT = []
ADDED = []
SUMMARIZE_CALLS = []
SUMMARY_INPUTS = []

def reset_sink():
    SINK_KEYS.clear(); SENT.clear(); ADDED.clear(); SUMMARIZE_CALLS.clear(); SUMMARY_INPUTS.clear()

def fake_summarize(prompts, proj, label, cfg):
    SUMMARIZE_CALLS.append(label)
    SUMMARY_INPUTS.append(list(prompts))
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

def allow_server_project(db, tok, root="/tmp/aitrack", project_key="github.com/parkkarl/aitrack", name="aitrack"):
    return A._db_allow_projects(db, tok, {"projects": [{"root_path": root, "local_path": root, "project_key": project_key, "name": name}]})


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
    A.collect_transcript_records = lambda since: []
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

# ============ TEST 1B: tunnikokkuvõte kasutab kogu vestlust, mitte ainult prompti ============
print("TEST 1B: tunnikokkuvõte eelistab lokaalse AI-vestluse kasutaja+assistendi teksti")
reset_sink(); setup([REC(10)])
A.collect_transcript_records = lambda since: [
    A.TranscriptRecord("Pi", "/proj", D(10, 10), "user", "küsimus ainult algatas teema"),
    A.TranscriptRecord("Pi", "/proj", D(10, 40), "assistant", "vastuses selgus päris lahendus ja kontrollitulemus"),
]
cfg = A.load_config(); allow = A.load_projects()
A._now_utc = lambda: dt.datetime(2026, 6, 16, 11, 15, tzinfo=UTC)
A.run_once(cfg, allow)
joined_summary_input = "\n".join(SUMMARY_INPUTS[-1]) if SUMMARY_INPUTS else ""
check("summarizer sai assistendi vastuse tekstiosa", "AI: vastuses selgus päris lahendus" in joined_summary_input)
check("tunnikokkuvõtte sisend ei piirdu ainult ühe promptiga", len(SUMMARY_INPUTS[-1]) == 2)

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

# ============ TEST 11: match_project — subpath jah, sibling ei, allow-juure all Git-repod eraldi ============
print("TEST 11: match_project tabab subpath'i, mitte sibling'it, ja eristab allow-juure all Git-repod")
allow = ["/home/x/proj"]
check("subpath sobib", A.match_project("/home/x/proj/src", allow) == "/home/x/proj")
check("sibling EI sobi", A.match_project("/home/x/proj2", allow) is None)
allow_root = CFG / "allow-root"
repo_a = allow_root / "client-a"
repo_b = allow_root / "nested" / "client-b"
repo_wt = allow_root / "client-worktree"
for p in (repo_a / "src", repo_b / "app", repo_wt / "src", allow_root / "nogit" / "deep"):
    p.mkdir(parents=True, exist_ok=True)
(repo_a / ".git").mkdir(exist_ok=True)
(repo_b / ".git").mkdir(exist_ok=True)
(repo_wt / ".git").write_text("gitdir: /tmp/aitrack-worktree/gitdir\n", encoding="utf-8")
check("allowlisti juur leiab sügava Git repo A", A.match_project(str(repo_a / "src"), [str(allow_root)]) == str(repo_a.resolve()))
check("allowlisti juur leiab sügava Git repo B", A.match_project(str(repo_b / "app"), [str(allow_root)]) == str(repo_b.resolve()))
check("allowlisti juur tunneb .git failiga worktree ära", A.match_project(str(repo_wt / "src"), [str(allow_root)]) == str(repo_wt.resolve()))
check("allowlisti gitita alamkaust muutub eraldi projektiks", A.match_project(str(allow_root / "nogit" / "deep"), [str(allow_root)]) == str((allow_root / "nogit").resolve()))
reset_sink()
setup([A.Record("Pi", str(repo_a / "src"), D(11), "repo a töö"), A.Record("Pi", str(repo_b / "app"), D(11), "repo b töö"), A.Record("Pi", str(allow_root / "nogit" / "deep"), D(11), "gitita töö")], allow=[str(allow_root)])
cfg = A.load_config(); cfg["group_by"] = "project"; allow = A.load_projects()
A._now_utc = lambda: dt.datetime(2026, 6, 16, 12, 15, tzinfo=UTC)
A.run_once(cfg, allow)
objs = sorted(r[2] for r in ADDED)
check("tunnipõhine run ei koonda kõike allow-juure nime alla", objs == ["OBJ[client-a]", "OBJ[client-b]", "OBJ[nogit]"])

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

# ============ TEST 40: start UI mitme-päeva ülevaade (Sheetsi-laadne) ============
print("TEST 40: start UI on Sheetsi-laadne mitme-päeva ülevaade värvikoodiga")
page = A._start_page_html()
check("ülevaade laeb kõik päevad /api/diary kaudu", "/api/diary" in page and "function loadDiary" in page and "diaryBody" in page)
check("üks rida = üks päev: kuupäev, tunnid, nädalapäev veergudena", "function diaryRow" in page and 'class="date"' in page and 'class="hours ' in page and 'class="wd"' in page)
check("tundide lahter läheb roheliseks 8+ ja muidu oranžiks", "function hoursClass" in page and ">= 8 ? 'ok' : 'low'" in page and "td.hours.ok" in page and "td.hours.low" in page)
check("spreadsheet-laadne ruudustik joonega lahtritega", "table.sheet" in page and "--grid" in page and "sheet-wrap" in page)
check("iga päeva saab Sheetsi kopeerida", "function copyDay" in page and "/api/copy" in page and 'class="mini good"' in page)

# ============ TEST 41: SQLite keskserveri helperid ============
print("TEST 41: SQLite keskserver salvestab tunniread ja prompt-eventid tokeniga")
sdb = CFG / "server-test.db"
sdb.unlink(missing_ok=True)
tok = A._db_add_user(sdb, "karl")
allow_server_project(sdb, tok, "/proj", "local:proj", "proj")
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
merged_same_hour = A._merge_same_hour_day_rows([
    ["2026-06-16", "09:00–10:00", "obj1", "saav1", A._NA, "tead1", "Pi", "k:m1"],
    ["2026-06-16", "09:00–10:00", "obj2", "saav2", "tak2", A._NA, "Claude", "k:m2"],
])
merged_day = A._day_row("2026-06-16", merged_same_hour)
check("sama tunni mitu teemat kuvatakse ühe tunnirea sees", len(merged_same_hour) == 1 and "• obj2" in merged_same_hour[0][2] and merged_same_hour[0][6] == "Pi, Claude")
check("päevarea punktide arv loeb sama tunni üheks punktiks", merged_day[1] == 1)
A._db_upsert_day_summary(sdb, tok, "2026-06-16", "Päev oli arusaadav ja tulemustega.", source="test", model="stub")
day_summary = A._db_day_summary(sdb, tok, "2026-06-16")
check("server salvestab päeva tervikkokkuvõtte", day_summary["summary"]["summary"] == "Päev oli arusaadav ja tulemustega." and day_summary["summary"]["source"] == "test")
check("päevade nimekiri sisaldab ka ainult kokkuvõttega päeva", "2026-06-16" in A._db_days(sdb, tok))
fallback_day_text = A._fallback_day_summary("2026-06-16", [{"objekt": "aitrack - päevavaate kokkuvõte", "saavutus": "kokkuvõtte väli lisatud", "takistus": "Ei olnud", "teadmine": "Kuidas mitte-tehnilisele lugejale tööpäeva kirjeldada", "tool": "Pi"}])
check("päeva fallback-kokkuvõte on loetav terviktekst", "kokkuvõtte väli lisatud" in fallback_day_text and "toorprompt" not in fallback_day_text.lower())
old_get, old_post, old_gen = A._server_get, A._server_post, A._generate_day_summary
posted_day_summary = {}
try:
    A._server_get = lambda op, params, cfg: {"ok": True, "rows": [{"hour": "10:00–11:00", "objekt": "aitrack", "saavutus": "valmis"}]}
    A._generate_day_summary = lambda date, rows, cfg: ("Lokaalne kokkuvõte", "stub", "mudel")
    def fake_day_summary_post(op, payload, cfg):
        posted_day_summary.update({"op": op, "payload": payload})
        return {"ok": True, "summary": {"summary": payload["summary"]}}
    A._server_post = fake_day_summary_post
    with _contextlib.redirect_stdout(_io.StringIO()):
        A.cmd_day_summary(types.SimpleNamespace(date="2026-06-16", user="", model=None, print_only=False, allow_empty=False), {"sink": {"type": "server", "server_url": "http://srv", "token": "tok"}})
finally:
    A._server_get, A._server_post, A._generate_day_summary = old_get, old_post, old_gen
check("day-summary käsk küsib serverist päeva ja postitab kokkuvõtte", posted_day_summary.get("op") == "day-summary" and posted_day_summary.get("payload", {}).get("summary") == "Lokaalne kokkuvõte")
check("praktikapäeviku heuristika täidab takistuse/teadmise", A._infer_takistus_from_texts(["paranda activity mittekuvamine"]) != A._NA and A._infer_teadmine_from_texts(["selgita heartbeat mudelit"]) != A._NA)
check("praktikapäeviku takistuse heuristika ei pea failiteed/faili veaks", A._infer_takistus_from_texts(["näita activity vaates failitee issue all", "ava claude.md fail"]) == A._NA)
learn_tail = A._infer_teadmine_from_texts(["kas tailscale töötab?", "kuidas ta saab minu võrku tulla?"])
learn_gnome = A._infer_teadmine_from_texts(["mis gnome mul on?", "tõmba https://github.com/daveprowse/Draw-On-Gnome"])
check("praktikapäeviku teadmise heuristika annab loetava teksti", "Selgus, kas" not in learn_tail and "Tailscale" in learn_tail and "https://" not in learn_gnome)
plain = A._plain_day_summary_from_texts(["aitrack uuenda globaalseid agent juhiseid", "aitrack work start ja tick käsuahel", "server login kasutajatele"], ["aitrack"])
check("praktikapäeviku fallback kasutab projekti nime ja ei kuva toorprompti", "aitrack uuenda" not in plain["objekt"] and plain["objekt"].startswith("aitrack -"))
long_summary = "Rebase tehtud. " + "Haru oli ajakohane ja kontrollitud. " * 20
long_plain = A._plain_day_summary_from_texts(["AI agenti töö", long_summary], ["Puhastusproff - Finar"])
check("praktikapäeviku kopeeritav saavutus ei lõppe varase kolme punktiga", "..." not in long_plain["saavutus"] and "…" not in long_plain["saavutus"])
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
allowdb = CFG / "server-allowlist-test.db"
allowdb.unlink(missing_ok=True)
allowtok = A._db_add_user(allowdb, "allowuser")
blocked = False
try:
    A._db_work_start(allowdb, allowtok, {"project": {"project_key": "github.com/example/blocked", "name": "blocked", "local_path": "/tmp/blocked"}, "work": {"title": "blocked"}, "session": {"client_id": "c", "tool": "pi", "local_path": "/tmp/blocked", "cwd": "/tmp/blocked"}})
except PermissionError:
    blocked = True
allow_server_project(allowdb, allowtok, "/tmp/allowed-root", "local:allowed-root", "allowed-root")
allowed_start = A._db_work_start(allowdb, allowtok, {"project": {"project_key": "github.com/example/child", "name": "child", "local_path": "/tmp/allowed-root/child"}, "work": {"title": "allowed"}, "session": {"client_id": "c", "tool": "pi", "local_path": "/tmp/allowed-root/child", "cwd": "/tmp/allowed-root/child"}})
rejected_events = A._db_ingest_events(allowdb, allowtok, [{"event_key": "blocked-event", "tool": "Pi", "project": "/tmp/blocked", "prompt_text": "ei tohi", "started_at": HFL(10).isoformat(), "ended_at": HFL(10).isoformat(), "duration_seconds": 60}])
old_min_client = os.environ.get("MIN_CLIENT_VERSION")
os.environ["MIN_CLIENT_VERSION"] = "3"
old_client = A._db_check_client_version(allowdb, allowtok, {"client_version": 2, "client": {"client_id": "client-old", "name": "oldbox", "platform": "linux"}})
ok_client = A._db_check_client_version(allowdb, allowtok, {"client_version": 3, "client": {"client_id": "client-new", "name": "newbox", "platform": "linux"}})
if old_min_client is None:
    os.environ.pop("MIN_CLIENT_VERSION", None)
else:
    os.environ["MIN_CLIENT_VERSION"] = old_min_client
with A._db_connect(allowdb) as conn:
    old_device = conn.execute("SELECT client_version FROM devices WHERE client_id = 'client-old'").fetchone()
check("server blokeerib work/start projekti, mida pole allowlisti lisatud", blocked is True)
check("server lubab allowlisti juure all oleva projekti", str(allowed_start.get("work_session_uid", "")).startswith("ws_"))
check("server ei salvesta evente mitte-allowlist projektist", rejected_events["raw_events"]["rejected"] == 1 and rejected_events["prompt_events"]["rejected"] == 1)
check("server nõuab vana kliendi korral uuendust", old_client.get("upgrade_required") is True and old_client.get("min_client_version") == 3 and ok_client.get("ok") is True)
check("server salvestab kliendi versiooni seadme külge", old_device is not None and old_device["client_version"] == 2)

# ============ TEST 43: 3NF work_session + invoice/practice vaated ============
print("TEST 43: serveri normaliseeritud work_session'id toidavad arve- ja praktikavaadet")
wdb = CFG / "server-work-test.db"
wdb.unlink(missing_ok=True)
wtok = A._db_add_user(wdb, "karl")
allow_server_project(wdb, wtok, "/tmp/pp-finar-pi", "github.com/puhastusproff/pp-finar", "pp-finar")
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
check("serveri päevavaade asendab automaatkokkuvõtte placeholderi tunni prompt-event transkriptiga", "tee issue 662" in day_rows_with_work[0][3] and "parandus valmis" not in day_rows_with_work[0][3])
check("serveri päevavaade tuletab prompt-event transkriptist uued teadmised", day_rows_with_work[0][5] != A._NA)

# ============ TEST 43B: projekti kestus ei summeeri paralleelseid agente ============
print("TEST 43B: Activity projekti kestus ühendab sama projekti kattuvad agendiminutid")
pddb = CFG / "server-project-duration-test.db"
pddb.unlink(missing_ok=True)
pdtok = A._db_add_user(pddb, "durationuser")
allow_server_project(pddb, pdtok, "/tmp/project-a", "github.com/example/project-a", "project-a")
allow_server_project(pddb, pdtok, "/tmp/project-b", "github.com/example/project-b", "project-b")
def duration_start(project_key, name, path, client, tool):
    return A._db_work_start(pddb, pdtok, {
        "project": {"project_key": project_key, "name": name, "local_path": path},
        "work": {"title": "paralleelne töö", "summary": "paralleelne töö"},
        "session": {"client_id": client, "device_name": "testbox", "platform": "linux", "tool": tool, "local_path": path, "cwd": path},
        "started_at": HFL(10).isoformat(),
    })
pda1 = duration_start("github.com/example/project-a", "project-a", "/tmp/project-a", "duration-a1", "pi")
pda2 = duration_start("github.com/example/project-a", "project-a", "/tmp/project-a", "duration-a2", "claude")
pdb1 = duration_start("github.com/example/project-b", "project-b", "/tmp/project-b", "duration-b1", "codex")
for minute in (0, 1, 2):
    A._db_work_tick(pddb, pdtok, {"work_session_uid": pda1["work_session_uid"], "tick_at": (HFL(10) + dt.timedelta(minutes=minute)).isoformat()})
for minute in (1, 2, 3):
    A._db_work_tick(pddb, pdtok, {"work_session_uid": pda2["work_session_uid"], "tick_at": (HFL(10) + dt.timedelta(minutes=minute)).isoformat()})
for minute in (2, 3):
    A._db_work_tick(pddb, pdtok, {"work_session_uid": pdb1["work_session_uid"], "tick_at": (HFL(10) + dt.timedelta(minutes=minute)).isoformat()})
project_duration_activity = A._db_activity_log(pddb, pdtok, {"date": ["2026-06-16"], "timezone": ["UTC"], "stale_minutes": ["999999"]})
durations_by_key = {x["project_key"]: x["minutes"] for x in project_duration_activity.get("project_durations", [])}
check("sama projekti paralleelsed agendid loetakse ühe korra", durations_by_key.get("github.com/example/project-a") == 4)
check("eri projektide kestused summeeritakse projektipõhiselt", durations_by_key.get("github.com/example/project-b") == 2 and project_duration_activity.get("project_minutes") == 6)
check("projekti kestus erineb agentide minutite lihtsummast", sum(x.get("minutes", 0) for x in project_duration_activity.get("sessions", [])) == 8 and project_duration_activity.get("project_minutes") == 6)

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
check("activity kuvab agentide summa asemel iga projekti kestust", "data.project_durations" in activity_page and "Projekti kestus" in activity_page and "durationCards" in activity_page and "formatDuration" in activity_page and "Agendi min" in activity_page and "reduce((a, s) => a + Number(s.minutes" not in activity_page)
check("activity näitab sündmuste millisekundeid ja metadata prompti selgitust", "fmtTime(x.at, true)" in activity_page and "fractionalSecondDigits:3" in activity_page and "Teksti ei saadetud" in activity_page)
check("activity kuvab prompti lõpetamisel tehtu kokkuvõtet ja tokenite arvu", "x.work_summary" in activity_page and "Tehtu kokkuvõte" in activity_page and "x.context_tokens" in activity_page and "tokenit" in activity_page and "promptContextLine" not in activity_page)
check("activity filtrid jõustuvad automaatselt ja status on nupud", "setSessionStatusFilter('active')" in activity_page and "scheduleActivityLoad" in activity_page and ">Ava</button>" not in activity_page)
check("activity kuupäevafilter toetab kalendri vahemikku", "datePicker" in activity_page and "selectDateRangeDay" in activity_page and "params.set('from', dateRangeStart)" in activity_page and "params.set('to', dateRangeEnd)" in activity_page)
server_start_page = A._start_page_html(server_mode=True)
check("serveri päevavaade kasutab cookie authi, mitte tokenivälja", "Server token" not in server_start_page and "credentials:'same-origin'" in server_start_page and "const SERVER_MODE = true" in server_start_page)
check("serveri päevavaate Abi asemel on kasutaja nupp", "Kasutaja" in server_start_page and ">Abi<" not in server_start_page and "/account" in server_start_page)
check("serveri päevavaates saab admin kasutajat valida", "userSelect" in server_start_page and "/api/activity/filters" in server_start_page and "selectedUserParam" in server_start_page)
check("serveri ülevaade laeb päevad /api/diary kaudu", "/api/diary" in server_start_page and "diaryBody" in server_start_page)
check("ülevaade näitab päevade ja tundide koondstatistikat", "renderStats" in server_start_page and "tundi kokku" in server_start_page and "≥8h" in server_start_page)
account_page = A._account_page_html()
check("kasutaja lehel saab parooli muuta", "/api/me/password" in account_page and "current-password" in account_page and "new-password" in account_page)
check("kasutaja lehel on kolme OS-i installikäsk", "/api/install-code" in account_page and "install-client.sh" in account_page and "install-client.ps1" in account_page and "Linux" in account_page and "macOS" in account_page and "Windows" in account_page)
check("Linux/macOS installikäsk sobib ka fish shellile", "bash <(" not in account_page and "| bash -s -- --code" in account_page)
check("installiskriptid ühendavad serveriga ja paigaldavad Pi extensioni", "install --minute-tracking --pi-extension" in A._install_client_sh("https://aitrack.example.com") and "Install-AitrackClient" in A._install_client_ps1("https://aitrack.example.com"))
check("login leht postitab /api/login endpointi", "/api/login" in A._login_page_html() and "password" in A._login_page_html())
check("päevavaates on link serveri tegevustele", "Server tegevused" in start_page and "location.href='/activity'" in start_page)
for theme_page in [start_page, server_start_page, account_page, A._login_page_html(), A._admin_page_html(), activity_page]:
    check("veebilehtedel on light/dark mode nupp", "themeToggle" in theme_page and "toggleTheme" in theme_page and "data-theme" in theme_page)

# ============ TEST 46: server client saadab explicit User-Agent ============
print("TEST 46: server client kasutab explicit User-Agent headerit")
headers = A._server_headers({"Content-Type": "application/json"})
check("server headerites on User-Agent", headers.get("User-Agent", "").startswith("aitrack/"))
check("server headerid säilitavad Content-Type", headers.get("Content-Type") == "application/json")

# ============ TEST 46B: prompt-eventide privaatsusrežiim ============
print("TEST 46B: klient saadab prompt-eventid vaikimisi metadatana, mitte tekstina")
priv_records = [A.Record("Pi", "/proj", HFL(10), "salajane prompti tekst")]
meta_events = A._prompt_events_payload(priv_records, ["/proj"], HFL(10), HFL(11), mode="metadata")
full_events = A._prompt_events_payload(priv_records, ["/proj"], HFL(10), HFL(11), mode="full")
off_events = A._prompt_events_payload(priv_records, ["/proj"], HFL(10), HFL(11), mode="off")
check("metadata prompt-event ei sisalda prompti teksti", len(meta_events) == 1 and meta_events[0]["prompt_text"] == "" and meta_events[0]["prompt_chars"] == len("salajane prompti tekst"))
check("full prompt-event on opt-in", full_events[0]["prompt_text"] == "salajane prompti tekst" and off_events == [])
hook_args = types.SimpleNamespace(prompt="salajane hook prompt", summary="", tool="pi", tool_name="", tool_call_id="", agent_uid="", parent_agent_uid="")
hook_meta = A._hook_event(hook_args, "prompt_started", {"work_session_uid": "ws_priv"}, {"cwd": "/proj", "project_key": "local:proj"}, {"server_prompt_events": "metadata"})
hook_full = A._hook_event(hook_args, "prompt_started", {"work_session_uid": "ws_priv"}, {"cwd": "/proj", "project_key": "local:proj"}, {"server_prompt_events": "full"})
hook_done_args = types.SimpleNamespace(prompt="", summary="Parandasin Activity vaate ja kontrollisin testidega.", tool="pi", tool_name="", tool_call_id="", agent_uid="", parent_agent_uid="", context_tokens=3210)
hook_done_meta = A._hook_event(hook_done_args, "prompt_finished", {"work_session_uid": "ws_priv"}, {"cwd": "/proj", "project_key": "local:proj"}, {"server_prompt_events": "metadata"})
check("hook prompt-start peidab teksti metadata režiimis", hook_meta["prompt_text"] == "" and hook_meta["summary"] == "" and hook_meta["prompt_chars"] == len("salajane hook prompt"))
check("hook prompt-start full režiim on opt-in", hook_full["prompt_text"] == "salajane hook prompt")
check("metadata režiim saadab prompti lõpus tehtu kokkuvõtte ja tokenite arvu", hook_done_meta["prompt_text"] == "" and hook_done_meta["work_summary"] == "Parandasin Activity vaate ja kontrollisin testidega." and hook_done_meta["payload"]["work_summary"] == hook_done_meta["work_summary"] and hook_done_meta["context_tokens"] == 3210)

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
login_profile = A._rate_limit_profile("/api/login")
agent_profile = A._rate_limit_profile("/api/events")
project_sync_profile = A._rate_limit_profile("/api/projects/allow")
normal_profile = A._rate_limit_profile("/activity")
check("rate-limit on ainult login endpointil", login_profile is not None and login_profile[0] == "login" and agent_profile is None and project_sync_profile is None and normal_profile is None)
check("login rate-limit throttleb, aga ei pane IP-banni", login_profile is not None and login_profile[2] is False)
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
allow_server_project(rdb, rtok)
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
check("raw event activity tekst kasutab sündmuse enda kirjeldust, mitte sessioni kokkuvõtet", raw_activity.get("raw_events", [{}])[0].get("summary") == "Käivitasin tööriista read")
check("activity filter otsib projekti ja kasutajat osalise tekstiga", len(raw_activity_filtered.get("raw_events", [])) == 1)
check("activity issue filter oskab combobox labelit kasutada", len(raw_activity_issue_filtered.get("raw_events", [])) == 1)
check("activity autocomplete valikud sisaldavad projekti, kasutajat ja issue pealkirja", any("parkkarl" in p.get("project_key", "") for p in raw_filter_options.get("projects", [])) and any(u.get("name") == "rawuser" for u in raw_filter_options.get("users", [])) and any(i.get("issue_key") == "662" and "Asendaja" in i.get("title", "") for i in raw_filter_options.get("issues", [])))
check("raw payload on piiratud ja saladus redigeeritud", "SALA" not in exported_raw.get("payload_json", "") and len(exported_raw.get("payload_json", "")) <= A.RAW_EVENT_PAYLOAD_MAX_BYTES)
safe_token_meta = A._raw_event_payload_value({"context_tokens": 321, "token": "SALA"})
check("tokenite arv säilib, kuid päris token redigeeritakse", safe_token_meta["context_tokens"] == 321 and safe_token_meta["token"] == "[redacted]")
check("raw export filter töötab", A._db_export_raw_events(rdb, rtok, {"period": ["2026-06"], "event_type": ["after_tool_call"]})["count"] == 0)

# ============ TEST 50: active interval rollup käsitleb sleep-gap'i ==========
print("TEST 50: active interval rollup tihendab tickid ja jätab sleep-gap'i auguks")
idb = CFG / "server-intervals-test.db"
idb.unlink(missing_ok=True)
itok = A._db_add_user(idb, "intervaluser")
allow_server_project(idb, itok)
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
allow_server_project(stdb, stok)
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
allow_server_project(adb, atok)
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
allow_server_project(edb, etok)
epayload = {
    "project": {"project_key": "github.com/parkkarl/aitrack", "name": "aitrack", "local_path": "/tmp/aitrack"},
    "work": {"title": "event endpoint test", "summary": "events"},
    "session": {"client_id": "client-event", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/aitrack", "cwd": "/tmp/aitrack", "agent_uid": "agent-a"},
    "started_at": HFL(9).isoformat(),
}
estart = A._db_work_start(edb, etok, epayload)
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-a", "tool": "pi", "prompt": "palun tee test", "occurred_at_utc": HFL(9).isoformat()}, "prompt_started")
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-b", "tool": "pi", "prompt": "teise agendi prompt", "occurred_at_utc": (HFL(9) + dt.timedelta(minutes=1)).isoformat()}, "prompt_started")
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-a", "tool_name": "read", "tool_call_id": "tc-1", "occurred_at_utc": (HFL(9) + dt.timedelta(minutes=3)).isoformat()}, "before_tool_call")
activity_agent = A._db_activity_log(edb, etok, {"period": ["2026-06"], "agent_uid": ["agent-a"]})
with A._db_connect(edb) as conn:
    erow = conn.execute("SELECT current_tool_name, current_tool_call_id, agent_uid FROM work_sessions WHERE session_uid = ?", (estart["work_session_uid"],)).fetchone()
    prompt_link = conn.execute("SELECT work_session_id FROM prompt_events WHERE event_key != ''").fetchone()
check("prompt/start endpoint tekitab prompt_eventi ja seob sessiooniga", prompt_link is not None and prompt_link["work_session_id"] == estart["work_session_id"])
check("tool-start endpoint uuendab jooksva tooli välja", erow["current_tool_name"] == "read" and erow["current_tool_call_id"] == "tc-1" and erow["agent_uid"] == "agent-a")
check("activity agent filter leiab raw eventid", len(activity_agent.get("raw_events", [])) >= 2 and all(x.get("agent_uid") == "agent-a" for x in activity_agent.get("raw_events", [])))
check("activity agent filter piirab ka prompt-evente", activity_agent.get("prompt_events") and all(x.get("agent_uid") == "agent-a" for x in activity_agent.get("prompt_events", [])))
check("activity koondvaade ei kuva prompt-starti raw ja prompt duplikaadina", len([x for x in activity_agent.get("activity", []) if x.get("event_type") == "prompt_started"]) == 1 and all(x.get("type") == "prompt_event" for x in activity_agent.get("activity", []) if x.get("event_type") == "prompt_started"))
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-a", "tool_name": "read", "tool_call_id": "tc-1", "occurred_at_utc": (HFL(9) + dt.timedelta(minutes=4)).isoformat()}, "after_tool_call")
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-a", "summary": "muudatused valmis", "occurred_at_utc": (HFL(9) + dt.timedelta(minutes=5)).isoformat()}, "agent_finished")
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-a", "summary": "prompti kokkuvõte", "context_tokens": 900, "occurred_at_utc": (HFL(9) + dt.timedelta(minutes=5, seconds=1)).isoformat()}, "prompt_finished")
activity_done = A._db_activity_log(edb, etok, {"period": ["2026-06"], "agent_uid": ["agent-a"]})
with A._db_connect(edb) as conn:
    done_row = conn.execute("SELECT current_tool_name, summary, status_detail FROM work_sessions WHERE session_uid = ?", (estart["work_session_uid"],)).fetchone()
    done_prompt = conn.execute("SELECT prompt_text FROM prompt_events WHERE prompt_text = 'prompti kokkuvõte'").fetchone()
check("tool-end endpoint puhastab jooksva tooli", done_row["current_tool_name"] == "")
check("done event uuendab sessiooni kokkuvõtte", done_row["summary"] == "prompti kokkuvõte" and str(done_row["status_detail"]).startswith("done:"))
check("prompt-done event salvestab tehtu kokkuvõtte prompt-eventina", done_prompt is not None)
check("activity prompt-done sisaldab tehtu kokkuvõtet ja tokenite arvu", any(x.get("event_type") == "prompt_finished" and x.get("work_summary") == "prompti kokkuvõte" and x.get("context_tokens") == 900 for x in activity_done.get("prompt_events", [])))
check("activity raw done event näitab sündmuse liiki ja oma kokkuvõtet", any(x.get("event_type") == "agent_finished" and x.get("summary") == "AI-agent lõpetas tööülesande: muudatused valmis" for x in activity_done.get("raw_events", [])))
check("activity varasem tool-event ei päri sessioni lõppkokkuvõtet", any(x.get("event_type") == "before_tool_call" and x.get("summary") == "Käivitasin tööriista read" for x in activity_done.get("raw_events", [])))
completion_activity = [x for x in activity_done.get("activity", []) if x.get("event_type") in {"agent_finished", "prompt_finished"}]
check("activity koondab agent- ja prompt-finished üheks lõpetamiseks", len(completion_activity) == 1 and completion_activity[0].get("type") == "prompt_event")
check("raw audit säilitab mõlemad lõpetamise sündmused", {x.get("event_type") for x in activity_done.get("raw_events", [])} >= {"agent_finished", "prompt_finished"})
activity_prompt_finished = A._db_activity_log(edb, etok, {"period": ["2026-06"], "event_type": ["prompt_finished"]})
check("activity event_type filter piirab nii prompt- kui raw-evente", activity_prompt_finished.get("prompt_events") and all(x.get("event_type") == "prompt_finished" for x in activity_prompt_finished.get("prompt_events", [])) and all(x.get("event_type") == "prompt_finished" for x in activity_prompt_finished.get("raw_events", [])))
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-limit", "summary": "limit prompt", "occurred_at_utc": (HFL(9) + dt.timedelta(minutes=7)).isoformat()}, "prompt_finished")
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-limit", "summary": "limit agent", "occurred_at_utc": (HFL(9) + dt.timedelta(minutes=7, seconds=1)).isoformat()}, "agent_finished")
activity_limit = A._db_activity_log(edb, etok, {"period": ["2026-06"], "limit": ["1"]})
check("activity lõpetamise koondamine ei sõltu raw tabeli limiidist", not any(x.get("event_type") == "agent_finished" for x in activity_limit.get("activity", [])) and any(x.get("event_type") == "prompt_finished" for x in activity_limit.get("activity", [])))
A._db_ingest_event_endpoint(edb, etok, {"work_session_uid": estart["work_session_uid"], "agent_uid": "agent-a", "summary": "hilisem eraldi lõpetamine", "occurred_at_utc": (HFL(9) + dt.timedelta(minutes=20)).isoformat()}, "agent_finished")
activity_reused_agent = A._db_activity_log(edb, etok, {"period": ["2026-06"], "agent_uid": ["agent-a"]})
check("korduv agent_uid ei peida ajaliselt eraldi lõpetamist", any(x.get("event_type") == "agent_finished" and "hilisem eraldi lõpetamine" in x.get("summary", "") for x in activity_reused_agent.get("activity", [])))

# ============ TEST 53: watchdog tuvastab stuck tool-call'i ==========
print("TEST 53: watchdog märgib pika poolelioleva tool-call'i stuck olekusse")
sdb2 = CFG / "server-stuck-test.db"
sdb2.unlink(missing_ok=True)
stok2 = A._db_add_user(sdb2, "stuckuser")
allow_server_project(sdb2, stok2)
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
allow_server_project(cdb, ctok)
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
allow_server_project(clipdb, cltok)
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
allow_server_project(wdb, wtok, "/tmp/acme", "github.com/example/acme", "acme")
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
allow_server_project(treedb, treetok)
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
allow_server_project(detdb, dettok)
detstart = A._db_work_start(detdb, dettok, {
    "project": {"project_key": "github.com/parkkarl/aitrack", "name": "aitrack", "local_path": "/tmp/aitrack"},
    "work": {"title": "detail", "summary": "detail"},
    "session": {"client_id": "client-detail", "device_name": "testbox", "platform": "linux", "tool": "pi", "local_path": "/tmp/aitrack", "cwd": "/tmp/aitrack"},
    "started_at": HFL(4).isoformat(),
})
A._db_ingest_events(detdb, dettok, [{"event_key": "detail-raw", "event_type": "before_tool_call", "work_session_uid": detstart["work_session_uid"], "agent_uid": "agent-detail", "tool_name": "bash", "tool_call_id": "tc-detail", "occurred_at_utc": HFL(4).isoformat(), "payload": {"command": "echo detail"}}])
filtered_activity = A._db_activity_log(detdb, dettok, {"period": ["2026-06"], "work_session_uid": [detstart["work_session_uid"]]})
miss_activity = A._db_activity_log(detdb, dettok, {"period": ["2026-06"], "worksessionid": ["ws_missing"]})
with A._db_connect(detdb) as conn:
    raw_id = conn.execute("SELECT id FROM raw_events WHERE event_key = 'detail-raw'").fetchone()["id"]
raw_flat_detail = A._db_event_detail(detdb, dettok, {"type": ["raw_event"], "id": [str(raw_id)]})
raw_detail = A._db_event_detail(detdb, dettok, {"type": ["raw_event"], "id": [str(raw_id)], "structured": ["1"]})
session_detail = A._db_event_detail(detdb, dettok, {"type": ["work_session"], "work_session_uid": [detstart["work_session_uid"]], "structured": ["1"]})
activity_html = A._activity_page_html()
deep_payload = {"leaf": "ok"}
for _ in range(20):
    deep_payload = {"child": deep_payload}
deep_detail = A._structured_event_detail("raw_event", {"id": 1, "payload_json": json.dumps(deep_payload)})
oversized_detail = A._structured_event_detail("raw_event", {"id": 2, "payload_json": json.dumps({"text": "x" * (A.RAW_EVENT_PAYLOAD_MAX_BYTES + 1)})})
check("event-detail eraldab DB-kirje parsitud payloadist", raw_detail["detail"]["record"]["event_key"] == "detail-raw" and raw_detail["detail"]["payload"]["payload"]["command"] == "echo detail")
check("event-detail ei kuva struktureeritud payloadis topelt event_key välja", "payload_json" not in raw_detail["detail"]["record"] and "event_key" not in raw_detail["detail"]["payload"])
check("event-detail säilitab vaikimisi vana flat API kuju", raw_flat_detail["detail"]["event_key"] == "detail-raw" and "payload_json" in raw_flat_detail["detail"] and raw_flat_detail["detail_format"] == "flat")
check("event-detail piirab liiga sügava ja suure payloadi", "truncated-depth" in json.dumps(deep_detail) and oversized_detail.get("payload", {}).get("_truncated") is True)
check("event-detail tagastab work_session kirje", session_detail["detail"]["record"]["session_uid"] == detstart["work_session_uid"])
check("activity HTML sisaldab detail modalit ja nuppe", "detailModal" in activity_html and "showDetail" in activity_html and "Toorandmed" in activity_html)
check("detail modal värvib JSON-i süntaksit ja küsib struktureeritud kuju", "syntaxHighlightJson" in activity_html and "json-key" in activity_html and "json-string" in activity_html and "params.set('structured', '1')" in activity_html)
check("activity filtreerib worksessionid järgi", len(filtered_activity["sessions"]) == 1 and len(filtered_activity["raw_events"]) == 1 and not miss_activity["sessions"] and not miss_activity["raw_events"])
check("activity HTML sisaldab worksessionid filtrit", "workSessionInput" in activity_html and "work_session_uid" in activity_html)

# ============ TEST 60B: Pi extension ei lõika valmis-kokkuvõtet 260 märgi pealt ==========
print("TEST 60B: Pi extension jätab valmis-kokkuvõtte kopeerimiseks alles")
extension_text = A._pi_extension_text()
check("Pi extension ei tee 260 märgi '...' lõiget", "s.length > 260" not in extension_text and "slice(0, 257)" not in extension_text)
check("Pi extension lubab pikema summary serverisse", "summary.slice(0, 4000)" in extension_text)
check("Pi extension saadab prompti lõpus tehtu kokkuvõtte ja tokenite arvu", '"prompt-done"' in extension_text and "doneSummary(event)" in extension_text and '"--context-tokens"' in extension_text and '"--context-json"' not in extension_text)

# ============ TEST 61: aitrack add vajab päris terminali ==========
print("TEST 61: aitrack add vajab päris terminali")
class _TTY:
    def __init__(self, value):
        self.value = value
    def isatty(self):
        return self.value

add_target = CFG / "human-add-project"
add_target.mkdir(parents=True, exist_ok=True)
A.save_projects([])
_orig_stdin = A.sys.stdin
_orig_sync_allowed = A._sync_allowed_projects
_orig_env = {k: os.environ.get(k) for k in A._AGENT_ENV_KEYS}
A._sync_allowed_projects = lambda *a, **kw: True
blocked_add = False
try:
    A.sys.stdin = _TTY(False)
    try:
        A.cmd_add(types.SimpleNamespace(path=str(add_target)), A.load_config())
    except SystemExit as e:
        blocked_add = "päris terminalist" in str(e)
    for k in A._AGENT_ENV_KEYS:
        os.environ.pop(k, None)
    A.sys.stdin = _TTY(True)
    A.cmd_add(types.SimpleNamespace(path=str(add_target)), A.load_config())
finally:
    A.sys.stdin = _orig_stdin
    A._sync_allowed_projects = _orig_sync_allowed
    for k, v in _orig_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
check("non-TTY/agent ei saa projekti lisada", blocked_add)
check("päris terminalist lisamine töötab", str(add_target.resolve()) in A.load_projects())

# ============ TEST 62: allowlist sync ei spämmmi serverit iga hook/tickiga ==========
print("TEST 62: allowlist sync kasutab lühikest cache'i")
sync_calls = []
_orig_server_post = A._server_post
try:
    A._server_post = lambda endpoint, payload, cfg: sync_calls.append((endpoint, payload)) or {"ok": True}
    A.save_projects([str(add_target)])
    A._allowlist_sync_cache_clear()
    server_cfg = {"sink": {"type": "server", "server_url": "https://aitrack.example", "token": "tok"}}
    A._sync_allowed_projects(server_cfg, quiet=True)
    A._sync_allowed_projects(server_cfg, quiet=True)
    A._sync_allowed_projects(server_cfg, quiet=True, force=True)
finally:
    A._server_post = _orig_server_post
check("muutumatu täis-allowlist saadetakse cache'i ajal ainult korra", [c[0] for c in sync_calls].count("projects/allow") == 2)

# ============ TEST 63: monthly report grupeerib issue ja annab evidence ==========
print("TEST 63: monthly report koondab issue-põhise aja, töö ja põhjenduse")
rdb = CFG / "server-monthly-report.db"
rdb.unlink(missing_ok=True)
rtok = A._db_add_user(rdb, "reporter")
allow_server_project(rdb, rtok, "/tmp/pp-finar-report", "github.com/puhastusproff/pp-finar", "pp-finar")
base_payload = {
    "project": {"project_key": "github.com/puhastusproff/pp-finar", "repo_url": "git@github.com:Puhastusproff/pp-finar.git", "name": "pp-finar", "local_path": "/tmp/pp-finar-report", "checkout_id": "co-report", "branch": "main"},
    "session": {"client_id": "client-report", "device_name": "reportbox", "platform": "linux", "checkout_id": "co-report", "tool": "pi", "local_path": "/tmp/pp-finar-report", "cwd": "/tmp/pp-finar-report", "branch": "main"},
    "started_at": HFL(12).isoformat(),
}
explicit = A._db_work_start(rdb, rtok, {
    **base_payload,
    "issue": {"provider": "github", "issue_key": "701", "title": "Kasutajana tahan kuuraporti eksporti", "url": "https://github.com/example/issues/701", "body": "Põhjendus issue body põhjal"},
    "work": {"title": "kuuraporti eksport", "summary": "alustasin #701", "billable": True},
})
A._db_work_tick(rdb, rtok, {"work_session_uid": explicit["work_session_uid"], "tick_at": HFL(12).isoformat()})
A._db_work_tick(rdb, rtok, {"work_session_uid": explicit["work_session_uid"], "tick_at": (HFL(12) + dt.timedelta(minutes=1)).isoformat()})
A._db_ingest_events(rdb, rtok, [{"event_key": "report-raw-1", "event_type": "agent_finished", "work_session_uid": explicit["work_session_uid"], "summary": "lisatud monthly report API", "occurred_at_utc": (HFL(12) + dt.timedelta(minutes=1)).isoformat()}])
A._db_work_finish(rdb, rtok, {"work_session_uid": explicit["work_session_uid"], "summary": "explicit #701 valmis", "ended_at": (HFL(12) + dt.timedelta(minutes=2)).isoformat(), "result": "kept"})
inferred_payload = {
    **base_payload,
    "session": {**base_payload["session"], "checkout_id": "co-report-2"},
    "work": {"title": "paranda issue 701 järelkontroll", "summary": "teen GH-701 järelkontrolli", "billable": True},
    "started_at": HFL(13).isoformat(),
}
inferred = A._db_work_start(rdb, rtok, inferred_payload)
for offset in range(3):
    A._db_work_tick(rdb, rtok, {"work_session_uid": inferred["work_session_uid"], "tick_at": (HFL(13) + dt.timedelta(minutes=offset)).isoformat()})
A._db_work_finish(rdb, rtok, {"work_session_uid": inferred["work_session_uid"], "summary": "järelkontroll #701 valmis", "ended_at": (HFL(13) + dt.timedelta(minutes=3)).isoformat(), "result": "kept"})
other_tok = A._db_add_user(rdb, "other-reporter")
allow_server_project(rdb, other_tok, "/tmp/pp-finar-report", "github.com/puhastusproff/pp-finar", "pp-finar")
other = A._db_work_start(rdb, other_tok, {
    **base_payload,
    "session": {**base_payload["session"], "client_id": "client-other", "checkout_id": "co-other"},
    "issue": {"provider": "github", "issue_key": "702", "title": "Teise kasutaja töö"},
    "work": {"title": "teise kasutaja töö", "summary": "teise kasutaja #702", "billable": True},
})
A._db_work_tick(rdb, other_tok, {"work_session_uid": other["work_session_uid"], "tick_at": HFL(14).isoformat()})
A._db_work_finish(rdb, other_tok, {"work_session_uid": other["work_session_uid"], "summary": "teise kasutaja töö valmis", "ended_at": (HFL(14) + dt.timedelta(minutes=1)).isoformat(), "result": "kept"})
admin_tok = A._db_add_user(rdb, "report-admin", role="admin")
report = A._db_monthly_report(rdb, rtok, {"period": ["2026-06"], "project_key": ["github.com/puhastusproff/pp-finar"], "hourly_rate": ["82"]})
admin_report = A._db_monthly_report(rdb, admin_tok, {"period": ["2026-06"], "project_key": ["github.com/puhastusproff/pp-finar"], "hourly_rate": ["82"]})
line = report["lines"][0] if report.get("lines") else {}
check("monthly report tagastab ühe issue rea", report.get("count") == 1 and line.get("issue") == "#701")
check("monthly report summeerib explicit ja tekstist tuvastatud issue minutid", line.get("minutes") == 5 and line.get("time") == "00:05")
check("monthly report arvutab summa", line.get("amount") == 6.83 and report.get("amount") == 6.83)
check("monthly report sisaldab issue põhjenduse body't", line.get("issue_body") == "Põhjendus issue body põhjal" and line.get("problem_text") == "Põhjendus issue body põhjal")
check("monthly report sisaldab tehtud töö kokkuvõtteid", "explicit #701 valmis" in line.get("work_done", []) and "järelkontroll #701 valmis" in line.get("work_done", []))
check("monthly report sisaldab sessiooni ja raw-event evidence'it", len(line.get("evidence", {}).get("work_session_uids", [])) == 2 and len(line.get("evidence", {}).get("raw_event_ids", [])) == 1)
check("monthly report tavakasutaja näeb ainult enda ridu ja admin kõiki", report.get("count") == 1 and admin_report.get("count") == 2)

# ============ TEST 64: päringukeha lugemine (Content-Length ja chunked) ============
# Regressioon: proxy (Cloudflare tunnel) edastas keha chunked-kujul ilma Content-Length
# päiseta, server luges tühja keha ja vastas "token puudub", kuigi token oli päringus.
print("TEST 64: _read_json loeb nii Content-Length kui ka chunked keha")
import io  # noqa: E402

def read_json_with(headers, raw):
    h = object.__new__(A._AitrackHandler)
    h.headers = headers
    h.rfile = io.BytesIO(raw)
    return h._read_json()

body = b'{"token": "abc", "date": "2026-07-17"}'
check("Content-Length keha loetakse",
      read_json_with({"Content-Length": str(len(body))}, body) == {"token": "abc", "date": "2026-07-17"})
chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
check("chunked keha loetakse",
      read_json_with({"Transfer-Encoding": "chunked"}, chunked) == {"token": "abc", "date": "2026-07-17"})
split = b"%x\r\n%s\r\n%x\r\n%s\r\n0\r\n\r\n" % (10, body[:10], len(body) - 10, body[10:])
check("mitmes tükis chunked keha liidetakse kokku",
      read_json_with({"Transfer-Encoding": "chunked"}, split) == {"token": "abc", "date": "2026-07-17"})
ext = b"%x;ext=1\r\n%s\r\n0\r\nX-Trailer: v\r\n\r\n" % (len(body), body)
check("chunk-laiendid ja trailerid ei sega lugemist",
      read_json_with({"Transfer-Encoding": "chunked", "Content-Length": "0"}, ext) == {"token": "abc", "date": "2026-07-17"})
check("Transfer-Encoding suurtähtedega tuvastatakse",
      read_json_with({"Transfer-Encoding": "CHUNKED"}, chunked) == {"token": "abc", "date": "2026-07-17"})
check("kehata päring annab tühja dicti", read_json_with({}, b"") == {})
check("vigane chunk-suurus annab tühja dicti",
      read_json_with({"Transfer-Encoding": "chunked"}, b"zz\r\n" + body) == {})
check("katkine chunked-ühendus annab tühja dicti",
      read_json_with({"Transfer-Encoding": "chunked"}, b"%x\r\n%s" % (len(body) + 50, body)) == {})
check("liiga suur Content-Length annab tühja dicti",
      read_json_with({"Content-Length": str(A.REQUEST_BODY_MAX_BYTES + 1)}, body) == {})
big = b"%x\r\n%s\r\n0\r\n\r\n" % (A.REQUEST_BODY_MAX_BYTES + 1, b"x" * 10)
check("liiga suur chunked keha annab tühja dicti",
      read_json_with({"Transfer-Encoding": "chunked"}, big) == {})
check("mitte-JSON keha annab tühja dicti",
      read_json_with({"Content-Length": "4"}, b"nope") == {})
check("JSON-massiiv (mitte objekt) annab tühja dicti",
      read_json_with({"Content-Length": "2"}, b"[]") == {})

# ============ TEST 65: mudelipõhine token-kulu (parse → aggregate → server) ============
print("TEST 65: token-kulu — normaliseerimine, transkripti-parse, agregatsioon, serveri upsert")

# --- normaliseerimine: Claude ja Pi usage-kujud ühisele kujule ---
claude_u = A._norm_usage("Claude", {"input_tokens": 10, "output_tokens": 200,
                                     "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 300})
check("Claude usage normaliseerub",
      claude_u == {"input": 10, "output": 200, "cache_read": 5000, "cache_write": 300, "reasoning": 0})
pi_u = A._norm_usage("Pi", {"input": 14000, "output": 600, "cacheRead": 0, "cacheWrite": 0, "reasoning": 60})
check("Pi usage normaliseerub (reasoning eraldi)",
      pi_u == {"input": 14000, "output": 600, "cache_read": 0, "cache_write": 0, "reasoning": 60})

# --- transkripti-parse: kirjuta ajutine session-jsonl ja loe kulu ---
since = D(9)
cl_file = CFG / "usage-claude.jsonl"
cl_file.write_text(
    json.dumps({"cwd": "/proj", "effort": "xhigh", "timestamp": "2026-06-16T10:30:00+00:00", "message": {
        "role": "assistant", "model": "claude-opus-4-8",
        "usage": {"input_tokens": 10, "output_tokens": 200, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 300},
        "content": [{"type": "thinking", "text": "..."}, {"type": "text", "text": "hi"}]}}) + "\n" +
    json.dumps({"cwd": "/proj", "timestamp": "2026-06-16T08:00:00+00:00", "message": {  # enne 'since' → välja
        "role": "assistant", "model": "claude-opus-4-8", "usage": {"input_tokens": 99, "output_tokens": 99}}}) + "\n",
    encoding="utf-8")
cl = A._usage_from_jsonl(cl_file, "Claude", since)
check("Claude transkriptist loetakse ainult 'since' järel", len(cl) == 1)
check("Claude .effort → thinking_level", cl and cl[0].thinking_level == "xhigh" and cl[0].thinking is True)
check("Claude tokenid loetud", cl and cl[0].output == 200 and cl[0].cache_read == 5000)

pi_file = CFG / "usage-pi.jsonl"
pi_file.write_text(  # päris Pi-formaat: cwd 'session'-real, tase eraldi 'thinking_level_change'-real
    json.dumps({"type": "session", "cwd": "/proj"}) + "\n" +
    json.dumps({"type": "thinking_level_change", "timestamp": "2026-06-16T10:10:00+00:00", "thinkingLevel": "high"}) + "\n" +
    json.dumps({"type": "message", "timestamp": "2026-06-16T10:15:00+00:00", "message": {
        "role": "assistant", "model": "gpt-5.6-sol",
        "usage": {"input": 14000, "output": 600, "cacheRead": 0, "cacheWrite": 0, "reasoning": 60}}}) + "\n",
    encoding="utf-8")
pi = A._usage_from_jsonl(pi_file, "Pi", since)
check("Pi session-realt cwd päritud", pi and pi[0].project == "/proj")
check("Pi eraldi thinking_level_change → tase kehtib edaspidi", pi and pi[0].thinking_level == "high" and pi[0].reasoning == 60)

# thinking ilma tasemeta → level 'on'; ilma thinking'uta → level ''
nolevel = CFG / "usage-nolevel.jsonl"
nolevel.write_text(
    json.dumps({"cwd": "/proj", "timestamp": "2026-06-16T10:20:00+00:00", "message": {
        "role": "assistant", "model": "m",
        "usage": {"input_tokens": 5, "output_tokens": 5, "reasoning": 7}}}) + "\n" +
    json.dumps({"cwd": "/proj", "timestamp": "2026-06-16T10:21:00+00:00", "message": {
        "role": "assistant", "model": "m", "usage": {"input_tokens": 5, "output_tokens": 5}}}) + "\n",
    encoding="utf-8")
nl = A._usage_from_jsonl(nolevel, "Pi", since)
check("thinking ilma tasemeta → level 'on'", nl[0].thinking_level == "on")
check("ilma thinking'uta → level ''", nl[1].thinking_level == "" and nl[1].thinking is False)

# --- agregatsioon: sama (tund × mudel × tase) liidetakse, erinev tase eraldub ---
UTC = dt.timezone.utc
def UR(model, level, out_tok, tool="Claude"):  # noqa: E306
    return A.TokenUsageRecord(tool, "/proj", D(10), model, bool(level), level, 10, out_tok, 5000, 300, 0)
recs = [UR("claude-opus-4-8", "xhigh", 200), UR("claude-opus-4-8", "xhigh", 100),
        UR("claude-opus-4-8", "high", 50), UR("claude-sonnet-5", "xhigh", 40)]
urows = A._token_usage_rows(recs, ["/proj"], group_by="project",
                            start=A.hour_floor(D(9)), end=A.hour_floor(D(12)), tz=UTC)
check("agregatsioon eristab tasemed sama mudeli sees (opus xhigh + opus high = 2 rida)", len(urows) == 3)
opus_xhigh = [r for r in urows if r["model"] == "claude-opus-4-8" and r["thinking_level"] == "xhigh"]
check("sama mudel+tase liidetakse", len(opus_xhigh) == 1 and opus_xhigh[0]["output"] == 300 and opus_xhigh[0]["messages"] == 2)
check("tase kandub ritta", {r["thinking_level"] for r in urows} == {"xhigh", "high"})
check("kõik read sama tunni hour_key all", len({r["hour_key"] for r in urows}) == 1 and urows[0]["hour_key"].startswith("k:"))
check("jälgimata projekt jäetakse välja",
      A._token_usage_rows([A.TokenUsageRecord("Claude", "/muu", D(10), "m", False, "", 1, 1, 0, 0, 0)],
                          ["/proj"], group_by="project", start=A.hour_floor(D(9)), end=A.hour_floor(D(12)), tz=UTC) == [])

# --- serveri salvestus: upsert + dedup ---
tudb = CFG / "server-token-usage-test.db"
tudb.unlink(missing_ok=True)
tutok = A._db_add_user(tudb, "tokenuser")
res = A._db_ingest_token_usage(tudb, tutok, urows)
check("serverisse salvestatud 3 rida", res["ok"] and res["stored"] == 3)
with A._db_connect(tudb) as conn:
    n = conn.execute("SELECT COUNT(*) FROM token_usage WHERE user_id=?", (1,)).fetchone()[0]
    check("token_usage tabelis 3 rida", n == 3)
# kordus-ingest sama hour_key+mudel+tase → upsert (mitte duplikaat), värske väärtus peale
bumped = [dict(r, output=99999) for r in urows if r["model"] == "claude-opus-4-8" and r["thinking_level"] == "xhigh"]
A._db_ingest_token_usage(tudb, tutok, bumped)
with A._db_connect(tudb) as conn:
    n2 = conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]
    val = conn.execute("SELECT output_tokens FROM token_usage WHERE model='claude-opus-4-8' AND thinking_level='xhigh'").fetchone()[0]
    check("kordus-ingest ei tekita duplikaati", n2 == 3)
    check("kordus-ingest kirjutab värske summa peale", val == 99999)
check("vigane rida (ilma hour_key'ta) jäetakse vahele",
      A._db_ingest_token_usage(tudb, tutok, [{"date": "2026-06-16", "hour": "10:00–11:00", "model": "x"}])["stored"] == 0)

# --- perioodi report: koondamine + kogusummad + filter ---
rep = A._db_token_usage_report(tudb, tutok, {"from": "2026-06-01", "to": "2026-06-30"})
check("report annab 3 rida perioodis", rep["ok"] and len(rep["rows"]) == 3)
check("report read järjestatud kokku-summa järgi kahanevalt",
      rep["rows"][0]["total"] >= rep["rows"][-1]["total"])
opus_row = [r for r in rep["rows"] if r["model"] == "claude-opus-4-8" and r["thinking_level"] == "xhigh"][0]
check("report tagastab thinking_level", opus_row["thinking_level"] == "xhigh")
check("report Kokku = input+output+cache (reasoning ei liideta topelt)",
      opus_row["total"] == opus_row["input"] + opus_row["output"] + opus_row["cache_read"] + opus_row["cache_write"])
check("report kogusummad liidetud", rep["totals"]["total"] == sum(r["total"] for r in rep["rows"]))
check("report mudelifilter kitsendab",
      len(A._db_token_usage_report(tudb, tutok, {"from": "2026-06-01", "to": "2026-06-30", "model": "sonnet"})["rows"]) == 1)
check("report periood väljaspool andmeid → tühi",
      A._db_token_usage_report(tudb, tutok, {"from": "2026-01-01", "to": "2026-01-31"})["rows"] == [])

# --- migratsioon: vana token_usage skeem (thinking, ilma thinking_level) → uus ---
migdb = CFG / "server-token-usage-migrate.db"
migdb.unlink(missing_ok=True)
A._db_init(migdb)  # loob uue skeemi
with A._db_connect(migdb) as conn:
    conn.execute("DROP TABLE token_usage")  # jäljenda vana klienti: loo VANA skeem ilma thinking_level'ita
    conn.execute("CREATE TABLE token_usage (id INTEGER PRIMARY KEY, user_id INTEGER, date TEXT, hour TEXT, "
                 "hour_key TEXT, tool TEXT, model TEXT, thinking INTEGER, created_at TEXT, updated_at TEXT, "
                 "UNIQUE(user_id, hour_key, tool, model, thinking))")
    conn.execute("INSERT INTO token_usage (user_id, date, hour, hour_key, tool, model, thinking, created_at, updated_at) "
                 "VALUES (1,'2026-06-16','10:00–11:00','k:x','Pi','m',1,'t','t')")
check("vana skeemis pole thinking_level veergu", "thinking_level" not in A._db_columns(A._db_connect(migdb).__enter__(), "token_usage"))
A._db_init(migdb)  # peaks vana tabeli maha viskama ja uue looma
with A._db_connect(migdb) as conn:
    cols = A._db_columns(conn, "token_usage")
    check("migratsioon lisas thinking_level veeru", "thinking_level" in cols)
    check("migratsioon viskas vana andmestiku maha (taastatav backfilliga)",
          conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0] == 0)

print(f"\n==== TULEMUS: {PASS} läbitud, {FAIL} ebaõnnestunud ====")
sys.exit(1 if FAIL else 0)
