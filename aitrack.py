#!/usr/bin/env python3
"""aitrack — AI-tööriistade tunnipõhine tööpäevik (macOS / Windows / Linux).

Vaatab iga tund läbi sinu Claude Code / Codex / Antigravity sessioonid,
filtreerib ainult lubatud projektid ning kirjutab Google Sheetsi ühe rea iga
(tund × projekt) kohta — lühikese eestikeelse kokkuvõttega.

Sõltub ainult Pythoni standardteegist + vähemalt ühest AI-CLI-st (claude/codex/gemini)
kokkuvõtete tegemiseks. Töötab Python 3.9+.
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import hashlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
THIS = Path(__file__).resolve()

# --- failiteed (platvormiülesed) -------------------------------------------
CONFIG_DIR = Path(os.environ.get("AITRACK_CONFIG_DIR", HOME / ".config" / "aitrack"))
CONFIG_FILE = CONFIG_DIR / "config.json"
PROJECTS_FILE = CONFIG_DIR / "projects.json"
STATE_FILE = CONFIG_DIR / "state.json"
LOG_FILE = CONFIG_DIR / "aitrack.log"
LOCK_FILE = CONFIG_DIR / "aitrack.lock"
NOTES_FILE = CONFIG_DIR / "notes.jsonl"  # käsitsi lisatud tunnimärkmed (aitrack note)
DEFAULT_CSV_PATH = HOME / "aitrack-log.csv"  # lokaalse sink'i vaiketee (masinapõhine, ei lähe git'i)

# AI-tööriistade logiasukohad — samad kõigil OS-idel (~ = kasutaja kodukaust)
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CODEX_HISTORY = HOME / ".codex" / "history.jsonl"
ANTIGRAVITY_HISTORY = HOME / ".gemini" / "antigravity-cli" / "history.jsonl"


@dataclass
class Record:
    tool: str          # "Claude" | "Codex" | "Antigravity"
    project: str       # absoluutne tee, kust AI-d kasutati (cwd / workspace)
    ts: dt.datetime    # UTC, timezone-aware
    text: str          # kasutaja prompt


# --- failisüsteemi abifunktsioonid ------------------------------------------
def _mkconfdir() -> None:
    """Loo CONFIG_DIR ja piira õigused 0o700 (token elab seal)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def _atomic_write_json(path: Path, obj, mode: int | None = None) -> None:
    """Atomaarne JSON-kirjutus (temp + os.replace), valikuline failirežiim."""
    _mkconfdir()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)


_LOG_MAX = 5 * 1024 * 1024  # 5 MB


# --- logimine ---------------------------------------------------------------
def log(msg: str) -> None:
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        _mkconfdir()
        try:  # rotatsioon: ära lase logil piiramatult kasvada
            if LOG_FILE.stat().st_size > _LOG_MAX:
                os.replace(LOG_FILE, LOG_FILE.with_suffix(".log.1"))
        except OSError:
            pass
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --- konfiguratsioon (JSON, ilma sõltuvusteta) -----------------------------
DEFAULT_CONFIG = {
    "timezone": "auto",
    "language": "et",
    "max_prompts_per_bucket": 40,
    # rea tase: "project" = üks rida iga (tund × projekt) kohta (vaikimisi, jagatav);
    #           "hour"    = üks rida tunni kohta, KÕIK kaustad koos (Projekt-veerus loetelu)
    "group_by": "project",
    # type: "local" (CSV-fail, ilma Google'ita — VAIKIMISI) või "google_sheets" (Apps Script)
    # path tühi + type "local" → _local_path annab DEFAULT_CSV_PATH (~/aitrack-log.csv)
    "sink": {"type": "local", "webapp_url": "", "token": "", "path": ""},
    # exe = paigaldusajal lahendatud absoluuttee (kindlustab ajasti vastu, kus PATH puudub)
    "summarizer": {"engine": "auto", "model": "", "timeout": 120, "exe": ""},
    "max_catchup_hours": 48,
}


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            user = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log(f"config: viga config.json lugemisel ({e}) — kasutan vaikeväärtusi")
            return cfg
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            elif isinstance(cfg.get(k), dict) and not isinstance(v, dict):
                # ära asenda dict-vaikeväärtust vale tüübiga (käsitsi rikutud config)
                log(f"config: '{k}' peaks olema objekt, eiran vigast väärtust")
            else:
                cfg[k] = v
    return cfg


def save_config(cfg: dict) -> None:
    # 0o600 — config sisaldab Sheetsi tokenit
    _atomic_write_json(CONFIG_FILE, cfg, mode=0o600)


def _detect_iana() -> str | None:
    """Leiab IANA ajavööndinime (nt 'Europe/Tallinn') ilma lisasõltuvusteta."""
    tz = os.environ.get("TZ")
    if tz and "/" in tz:
        return tz
    p = Path("/etc/localtime")  # Linux + macOS: sümlink zoneinfo-faili
    try:
        if p.is_symlink():
            target = os.readlink(str(p))
            if "zoneinfo/" in target:
                return target.split("zoneinfo/")[-1]
    except OSError:
        pass
    et = Path("/etc/timezone")  # paljud Linuxid
    try:
        if et.exists():
            return et.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


def get_tz(cfg: dict):
    """Ainult KUVAMISEKS (sildid/kuupäev) — kogu ämbritamine toimub UTC-s.

    Eelista configis selgelt määratud 'timezone'; muidu proovi süsteemi IANA-tsooni;
    viimase variandina kasuta hetke fikseeritud nihet (kuvamise jaoks piisav)."""
    name = cfg.get("timezone", "auto")
    if name and name != "auto":
        try:
            return ZoneInfo(name)
        except Exception:
            log(f"tz: tundmatu ajavöönd '{name}', proovin süsteemi oma")
    iana = _detect_iana()
    if iana:
        try:
            return ZoneInfo(iana)
        except Exception:
            log(f"tz: ei suutnud laadida IANA-tsooni '{iana}' (paigalda 'tzdata'?)")
    return dt.datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


# --- projektide allowlist ---------------------------------------------------
def load_projects() -> list[str]:
    if PROJECTS_FILE.exists():
        try:
            data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
            return [str(Path(p).expanduser().resolve()) for p in data.get("allow", [])]
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_projects(paths: list[str]) -> None:
    _atomic_write_json(PROJECTS_FILE, {"allow": sorted(set(paths))})


def match_project(record_project: str, allow: list[str]) -> str | None:
    """Tagastab lubatud projekti tee, kui kirje sinna alla kuulub (pikim vaste).

    Võrdleb tee-komponente (mitte stringi-prefiksit) ja normaliseerib tõstu
    (`normcase`) — väldib Windowsi tõstutundlikkuse ja segasseparaatorite vigu
    ning sibling-vasteid (nt /a/proj vs /a/proj2)."""
    try:
        rp = os.path.normcase(str(Path(record_project).expanduser().resolve()))
    except (OSError, ValueError, RuntimeError):
        rp = os.path.normcase(record_project)
    rparts = Path(rp).parts
    best = None
    best_len = -1
    for p in allow:
        pparts = Path(os.path.normcase(p)).parts
        if rparts[: len(pparts)] == pparts and len(pparts) > best_len:
            best, best_len = p, len(pparts)
    return best


# --- state ------------------------------------------------------------------
def load_state() -> dict | None:
    """Tagastab state-dict'i; {} kui faili pole (uus paigaldus); None kui RIKUTUD."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("state: RIKUTUD state.json — katkestan, et watermarki mitte nullida")
            return None
        except OSError:
            return None
    return {}


def save_state(state: dict) -> None:
    _atomic_write_json(STATE_FILE, state)


# --- platvormiülene lukk (väldib paralleelseid run'e) -----------------------
LOCK_STALE_SECONDS = 7200  # peab ületama halvima järelejõudmis-puhangu


def acquire_lock(stale_seconds: int = LOCK_STALE_SECONDS) -> Path | None:
    _mkconfdir()
    for attempt in range(2):  # piiratud: max üks aegunud-luku ülevõtmine, ei rekurseeru
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return LOCK_FILE
        except FileExistsError:
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
            except OSError:
                age = 0.0
            if age > stale_seconds and attempt == 0:
                log(f"lock: aegunud lukk ({int(age)}s) — võtan üle")
                try:
                    LOCK_FILE.unlink()
                except OSError:
                    pass
                continue
            return None
    return None


def heartbeat_lock(lock: Path | None) -> None:
    """Värskenda luku mtime'i, et pikk run ei näiks teisele protsessile aegunud."""
    if lock:
        try:
            os.utime(lock, None)
        except OSError:
            pass


def release_lock(lock: Path | None) -> None:
    if lock:
        try:
            Path(lock).unlink(missing_ok=True)
        except OSError:
            pass


def bucket_key(hstart_utc: dt.datetime, project_path: str | None) -> str:
    """Stabiilne dedup-võti UTC-tunnist + TÄISTEE hashist.

    - UTC-tund → ei sõltu kuvasildist/ajavööndist.
    - Täistee hash → kaks samanimelist projekti (nt /a/web ja /b/web) EI põrku.
    - project_path=None → tunni-tasandi võti ('hour'-režiim): sõltub AINULT tunnist,
      nii et kõik kaustad koonduvad ühte ritta ega tekita duplikaate.
    - 'k:' prefiks → võti pole kuupäeva-kujuline, nii et Google Sheets ei coerci seda."""
    iso = hstart_utc.astimezone(dt.timezone.utc).isoformat()
    if project_path is None:
        return f"k:{iso}|__hour__"
    h = hashlib.sha1(str(project_path).encode("utf-8")).hexdigest()[:8]
    return f"k:{iso}|{Path(project_path).name}|{h}"


# --- ajatempli abifunktsioonid ----------------------------------------------
def parse_iso(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def from_epoch(value: float, unit: str = "s") -> dt.datetime:
    secs = value / 1000.0 if unit == "ms" else float(value)
    return dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc)


def is_user_prompt(text: str) -> bool:
    """Filtreerib välja süsteemi/injekteeritud sõnumid."""
    t = (text or "").strip()
    if not t:
        return False
    if t.startswith("<"):  # <command-name>, <environment_context>, <local-command...>
        return False
    if t.startswith("[Request interrupted"):  # Claude'i katkestusmarker, mitte päris prompt
        return False
    return True


# --- parserid ---------------------------------------------------------------
def claude_records(since: dt.datetime) -> list[Record]:
    out: list[Record] = []
    if not CLAUDE_PROJECTS.is_dir():
        return out
    cutoff = since.timestamp()
    for jsonl in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        try:
            if jsonl.stat().st_mtime < cutoff - 3600:  # vana fail — jäta vahele
                continue
        except OSError:
            continue
        try:
            with jsonl.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    # odav eelfilter (tolerantne tühikutele); tegelik kontroll allpool
                    if not line or '"user"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") != "user" or d.get("isMeta"):
                        continue
                    ts = parse_iso(d.get("timestamp", ""))
                    if ts is None or ts <= since:
                        continue
                    msg = d.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    else:
                        text = content if isinstance(content, str) else ""
                    if not is_user_prompt(text):
                        continue
                    project = d.get("cwd") or ""
                    if project:
                        out.append(Record("Claude", project, ts, text.strip()))
        except OSError:
            continue
    return out


def _codex_cwd_map(needed_ids: set[str]) -> dict[str, str]:
    """session_id -> cwd, AINULT vajalike sessioonide rollout-failidest.

    Rollout-faili nimi sisaldab session-id'd, seega avame vaid need failid, mille
    id esineb hiljutises history-aknas — bounded kulu (mitte O(kogu ajalugu)) ja
    samas ei lähe ükski hiljutine kirje projektita kaduma."""
    mapping: dict[str, str] = {}
    if not needed_ids or not CODEX_SESSIONS.is_dir():
        return mapping
    for roll in CODEX_SESSIONS.rglob("rollout-*.jsonl"):
        name = roll.name
        if not any(sid in name for sid in needed_ids):
            continue
        try:
            with roll.open(encoding="utf-8", errors="replace") as f:
                for _ in range(3):  # session_meta on faili esimestel ridadel
                    line = f.readline()
                    if not line:
                        break
                    if '"session_meta"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        break
                    if d.get("type") == "session_meta":
                        p = d.get("payload") or {}
                        sid, cwd = p.get("id"), p.get("cwd")
                        if sid and cwd:
                            mapping[sid] = cwd
                        break
        except OSError:
            continue
    return mapping


def codex_records(since: dt.datetime) -> list[Record]:
    if not CODEX_HISTORY.exists():
        return []
    # 1. samm: loe history-aknas olevad promptid + vajalikud session-id'd
    window: list[tuple] = []  # (session_id, ts, text)
    try:
        with CODEX_HISTORY.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = d.get("ts")
                if ts_raw is None:
                    continue
                ts = from_epoch(ts_raw, "s")
                if ts <= since:
                    continue
                text = d.get("text", "")
                if not is_user_prompt(text):
                    continue
                window.append((d.get("session_id", ""), ts, text.strip()))
    except OSError:
        return []
    if not window:
        return []
    # 2. samm: lahenda cwd ainult vajalikele sessioonidele
    cwd_map = _codex_cwd_map({sid for sid, _, _ in window if sid})
    out = [Record("Codex", cwd_map[sid], ts, text)
           for sid, ts, text in window if sid in cwd_map]
    dropped = len(window) - len(out)
    if dropped:
        log(f"codex: {dropped}/{len(window)} prompti jäi cwd-ta (rollout puudub?) — neid ei kaasata")
    return out


def antigravity_records(since: dt.datetime) -> list[Record]:
    out: list[Record] = []
    if not ANTIGRAVITY_HISTORY.exists():
        return out
    try:
        with ANTIGRAVITY_HISTORY.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = d.get("timestamp")
                if ts_raw is None:
                    continue
                ts = from_epoch(ts_raw, "ms")
                if ts <= since:
                    continue
                text = d.get("display", "")
                if not is_user_prompt(text):
                    continue
                project = d.get("workspace", "")
                if project:
                    out.append(Record("Antigravity", project, ts, text.strip()))
    except OSError:
        pass
    return out


def collect_records(since: dt.datetime) -> list[Record]:
    recs = claude_records(since) + codex_records(since) + antigravity_records(since)
    recs.sort(key=lambda r: r.ts)
    return recs


# --- kokkuvõtja (mitme mootori automaattuvastus) ----------------------------
SUMMARIZER_ENGINES = ["claude", "codex", "gemini"]  # eelistuse järjekord


def _engine_cmd(engine: str, exe: str, prompt: str, model: str) -> tuple[list[str], str | None]:
    """Tagastab (cmd, stdin_tekst). Claude saab prompti STDIN-i kaudu → väldib
    argv pikkuse-limiiti (Windowsil ~32KB) ja prompti nähtavust 'ps'-is."""
    if engine == "claude":
        cmd = [exe, "-p"]
        if model:
            cmd += ["--model", model]
        return cmd, prompt
    if engine == "codex":
        return [exe, "exec", prompt], None
    if engine == "gemini":
        cmd = [exe, "-p", prompt]
        if model:
            cmd += ["-m", model]
        return cmd, None
    return [exe, prompt], None


def resolve_engine(cfg: dict) -> tuple[str, str | None]:
    """Tagastab (engine_nimi, exe_tee). 'none' kui ühtegi pole.

    Eelistab paigaldusajal salvestatud absoluutteed (`summarizer.exe`) — nii leiab
    mootori ka ajastis, kus PATH on minimaalne (Windows Task Scheduler) või on
    triivinud (nvm/asdf)."""
    s = cfg.get("summarizer", {})
    want = s.get("engine", "auto")
    if want == "none":
        return "none", None
    stored = (s.get("exe") or "").strip()
    if stored and Path(stored).exists():
        base = Path(stored).name.lower()
        for eng in SUMMARIZER_ENGINES:
            if eng in base and want in ("auto", eng):
                return eng, stored
        if want != "auto":
            return want, stored
    candidates = SUMMARIZER_ENGINES if want == "auto" else [want]
    for eng in candidates:
        exe = shutil.which(eng)
        if exe:
            return eng, exe
    return "none", None


def summarize(prompts: list[str], project_label: str, hour_label: str, cfg: dict) -> str:
    engine, exe = resolve_engine(cfg)
    if engine == "none" or exe is None:
        return _fallback_summary(prompts)

    joined = "\n".join(f"- {p[:300]}" for p in prompts)
    if len(joined) > 8000:  # piira koondprompti suurust
        joined = joined[:8000] + "\n…(kärbitud)"
    # 'hour'-režiimis on silt mitu kausta ("web, api") → õige kääne ja katab kõik kaustad
    multi = "," in project_label
    where = (f"projektides {project_label} (kata kõik)" if multi
             else f"projektis {project_label}")
    prompt = (
        "Sa teed lühikesi eestikeelseid kokkuvõtteid arendustööst. "
        f"Allpool on kasutaja AI-promptid ühe tunni ({hour_label}) jooksul "
        f"{where}. "
        "Vasta TÄPSELT ühe lühikese eestikeelse lausega (kuni ~18 sõna), "
        "mis võtab kokku mida selle tunni jooksul tehti. "
        "Ära lisa midagi muud peale selle lause.\n\n"
        f"Promptid:\n{joined}"
    )
    model = cfg.get("summarizer", {}).get("model", "")
    cmd, stdin_text = _engine_cmd(engine, exe, prompt, model)
    try:
        res = subprocess.run(
            cmd, input=stdin_text, capture_output=True, text=True,
            timeout=int(cfg.get("summarizer", {}).get("timeout", 120)),
            cwd=tempfile.gettempdir(),
        )
        text = (res.stdout or "").strip()
        if res.returncode == 0 and text:
            line = text.replace("\n", " ").strip()
            return line if len(line) <= 400 else line[:397] + "..."
        log(f"summarize: {engine} rc={res.returncode} err={(res.stderr or '')[:200]}")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(f"summarize: {engine} ebaõnnestus ({e}) — kasutan varvarianti")
    return _fallback_summary(prompts)


def _fallback_summary(prompts: list[str]) -> str:
    """Geneeriline — EI pane toorest promptisisu lehele (privaatsus)."""
    n = len(prompts)
    return f"({n} prompti, automaatkokkuvõte puudub)" if n else "(tegevus tuvastatud)"


# --- sink: Google Sheets VÕI lokaalne CSV -----------------------------------
CSV_HEADER = ["Kuupäev", "Tund", "Projekt", "Tööriist", "Töö kokkuvõte", "_key"]
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _cell_safe(v) -> str:
    """Neutraliseeri tabeliarvutuse valemisüst: prefiksib ohumärgiga algava lahtri '-ga.

    Rakendatakse kasutajast tuletatud lahtritele (kokkuvõte, projekt) ENNE kirjutamist,
    nii Sheetsi kui lokaalse CSV jaoks."""
    s = "" if v is None else str(v)
    return "'" + s if s and s[0] in _FORMULA_LEAD else s


def _local_path(cfg: dict) -> Path | None:
    sink = cfg.get("sink", {})
    p = (sink.get("path") or "").strip()
    if p:
        return Path(p).expanduser()
    # tee seadmata: lokaalse sink'i puhul vaikimisi ~/aitrack-log.csv (nii töötab ka ilma setup'ita)
    return DEFAULT_CSV_PATH if sink.get("type") == "local" else None


def _local_keys(path: Path) -> set[str]:
    """Loe olemasolevad dedup-võtmed. Ankurda 'k:' prefiksile + veerguarvule, et
    käsitsi muudetud/lühike rida ei tooks dedup-hulka fantoomvõtit (vaikne andmekadu)."""
    if not path.exists():
        return set()
    out: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= len(CSV_HEADER) and row[-1].startswith("k:"):
                    out.add(row[-1])
    except OSError:
        pass
    return out


def _local_append(rows: list[list], keys: list[str], cfg: dict) -> bool:
    path = _local_path(cfg)
    if path is None:
        log("sink: lokaalse faili tee puudub configis")
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _local_keys(path)
        is_new = not path.exists() or path.stat().st_size == 0
        if is_new:  # loo fail 0o600-ga (sisaldab tundlikke kokkuvõtteid)
            try:
                os.close(os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600))
            except OSError:
                pass
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(CSV_HEADER)
            for row, k in zip(rows, keys):
                if k in existing:
                    continue  # dedup, nagu Apps Scriptis
                w.writerow(list(row) + [k])  # read on juba _cell_safe'iga ehitatud
                existing.add(k)
        return True
    except OSError as e:
        log(f"sink: lokaalse faili kirjutus ebaõnnestus: {e}")
        return False


# --- Google Sheets sink -----------------------------------------------------
def _post(payload: dict, cfg: dict) -> str | None:
    """POST Apps Scripti veebirakendusele; tagastab vastuse keha või None."""
    url = cfg.get("sink", {}).get("webapp_url", "").strip()
    if not url:
        log("sink: webapp_url puudub configis — ei saa Sheetsiga suhelda")
        return None
    payload = {"token": cfg.get("sink", {}).get("token", ""), **payload}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace").strip()
    except Exception as e:  # noqa: BLE001 — võrgu/HTTP vead
        log(f"sink: POST ebaõnnestus: {e}")
        return None


def append_rows(rows: list[list], keys: list[str], cfg: dict) -> bool:
    if cfg.get("sink", {}).get("type") == "local":
        return _local_append(rows, keys, cfg)
    # 'keys' = deterministlikud rea-võtmed; Apps Script jätab juba olemasolevad vahele
    # (idempotentsus → katkestus/kordussaatmine ei tekita duplikaate)
    body = _post({"rows": rows, "keys": keys}, cfg)
    if body is None:
        return False
    low = body.lower()
    if low.startswith("ok"):
        return True
    if low.startswith("forbidden"):
        log("sink: TOKEN TAGASI LÜKATUD — paranda config.json token "
            "(kordamine ei aita, kuni token vale)")
        return False
    log(f"sink: ootamatu vastus: {body[:200]}")
    return False


def fetch_existing_keys(cfg: dict) -> set[str] | None:
    """Küsib lehelt juba olemasolevad rea-võtmed (backfill ei summeeri neid uuesti).

    Tagastab None, kui päring ebaõnnestub või Apps Script on vana (ilma 'keys' režiimita)
    → kutsuja summeerib siis kõik (dedup hoiab duplikaadid niikuinii ära)."""
    if cfg.get("sink", {}).get("type") == "local":
        path = _local_path(cfg)
        return _local_keys(path) if path else set()
    body = _post({"op": "keys"}, cfg)
    if body is None:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log("sink: võtmete päring andis ootamatu vastuse (vana Apps Script?) — summeerin kõik")
        return None
    if isinstance(data, list):
        return {str(x) for x in data}
    return None


# --- tundide grupeerimine ja töötlemine -------------------------------------
def hour_floor(d: dt.datetime) -> dt.datetime:
    return d.replace(minute=0, second=0, microsecond=0)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --- käsitsi tunnimärkmed (aitrack note) ------------------------------------
def _hour_iso(d: dt.datetime) -> str:
    """Kanooniline UTC-tunni võti — normaliseeri ENNE floorimist UTC-sse,
    et märkme salvestus ja väljundi-lugemine viitaks alati samale füüsilisele tunnile."""
    return hour_floor(d.astimezone(dt.timezone.utc)).isoformat()


def append_note(hour_utc: dt.datetime, text: str) -> None:
    """Lisa käsitsi-märge ühe tunni juurde (append-only JSONL, kraşi-kindel)."""
    _mkconfdir()
    rec = {"hour": _hour_iso(hour_utc), "text": text, "added": _now_utc().isoformat()}
    with NOTES_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_notes() -> dict[str, list[str]]:
    """Loe kõik märkmed → {UTC-tunni-iso: [tekst, …]}. Rikutud rida jäetakse vahele."""
    out: dict[str, list[str]] = {}
    if not NOTES_FILE.exists():
        return out
    try:
        lines = NOTES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        h, t = rec.get("hour"), (rec.get("text") or "").strip()
        if h and t:
            out.setdefault(h, []).append(t)
    return out


def run_once(cfg: dict, allow: list[str], backfill_hours: int | None = None) -> None:
    if not allow:
        log("run: ühtegi lubatud projekti pole (lisa: aitrack add <tee>). Ei tee midagi.")
        return

    lock = acquire_lock()
    if lock is None:
        log("run: teine aitrack-protsess juba töötab (lukk hõivatud) — väljun")
        return
    try:
        _run_once_locked(cfg, allow, backfill_hours, lock)
    finally:
        release_lock(lock)


def _run_once_locked(cfg: dict, allow: list[str], backfill_hours: int | None,
                     lock: Path | None = None) -> None:
    tz = get_tz(cfg)  # ainult kuvamiseks
    # Kogu ämbritamine ja watermark on UTC-s → DST-st sõltumatu, üks füüsiline tund = üks ämber.
    now_utc = _now_utc()
    current_hour = hour_floor(now_utc)

    state = load_state()
    if state is None:  # rikutud state — ära nulli watermarki
        return

    if backfill_hours is not None:
        last_hour = current_hour - dt.timedelta(hours=backfill_hours)
    elif "last_processed_hour" in state:
        parsed = parse_iso(state["last_processed_hour"])
        last_hour = parsed if parsed else current_hour - dt.timedelta(hours=1)
    else:
        last_hour = current_hour - dt.timedelta(hours=1)  # esimene käivitus: eelmine tund
    last_hour = hour_floor(last_hour.astimezone(dt.timezone.utc))  # joonda (käsitsi/legacy state)

    if last_hour >= current_hour:
        log(f"run: pole uusi lõpetatud tunde (viimane={last_hour.isoformat()})")
        return

    # Piira järelejõudmise mahtu ühe käivituse kohta — pikk seisak ei tekita
    # ühe run'i sees hiidpuhangut (mis võiks luku-akna ületada). Ülejäänu järgmisel korral.
    process_until = current_hour
    max_catchup = int(cfg.get("max_catchup_hours", 48))
    if backfill_hours is None and max_catchup > 0 and \
            current_hour - last_hour > dt.timedelta(hours=max_catchup):
        process_until = last_hour + dt.timedelta(hours=max_catchup)
        log(f"run: järelejõudmine piiratud {max_catchup}h-le, ülejäänu järgmisel korral")

    if resolve_engine(cfg)[0] == "none":
        log("run: HOIATUS — AI-CLI mootorit ei leitud, read saavad varukokkuvõtte")

    # võta veidi varem, et täistunni-piiril olev kirje ei kaoks; täpse valiku teeb ämbrifilter
    records = collect_records(last_hour - dt.timedelta(seconds=1))
    log(f"run: kogutud {len(records)} kirjet alates {last_hour.strftime('%Y-%m-%d %H:%M')} UTC")

    # group_by="hour" → kõik kaustad koonduvad ühte ämbrisse tunni kohta (üks rida/tund);
    # group_by="project" (vaikimisi) → eraldi ämber iga (tund × projekt) kohta.
    group_by = cfg.get("group_by", "project")
    buckets: dict[tuple, list[tuple]] = {}
    for r in records:
        hstart = hour_floor(r.ts)  # UTC tunni-piir
        # Pool-avatud vahemik [last_hour, process_until): iga lõpetatud tund täpselt korra.
        if not (last_hour <= hstart < process_until):
            continue
        proj = match_project(r.project, allow)
        if proj is None:
            continue
        bkey = (hstart, None) if group_by == "hour" else (hstart, proj)
        buckets.setdefault(bkey, []).append((r, proj))

    # Backfill: küsi lehelt olemasolevad võtmed ja jäta need tunnid kokku VÕTMATA
    # (väldib raisatud LLM-kõnesid). Tavakäivitus seda ei tee — uued tunnid pole veel lehel.
    existing_keys: set[str] | None = None
    if backfill_hours is not None:
        existing_keys = fetch_existing_keys(cfg)
        if existing_keys is not None:
            log(f"backfill: {len(existing_keys)} võtit juba lehel — neid ei summeerita uuesti")

    max_p = int(cfg.get("max_prompts_per_bucket", 40))
    notes_by_hour = load_notes()  # käsitsi lisatud tunnimärkmed → liidetakse kokkuvõttesse
    rows: list[list] = []
    keys: list[str] = []
    skipped = 0
    for (hstart, proj_key), items in sorted(buckets.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        recs = sorted((it[0] for it in items), key=lambda r: r.ts)
        tools = sorted({r.tool for r in recs})
        prompts = [r.text for r in recs][:max_p]
        # 'hour'-režiimis võib ämbris olla mitu kausta → Projekt-veergu loetelu (nt "api, web")
        proj_label = ", ".join(sorted({Path(it[1]).name for it in items}))
        local = hstart.astimezone(tz)            # kuvamine kohalikus ajas
        local_end = (hstart + dt.timedelta(hours=1)).astimezone(tz)
        hour_label = f"{local.strftime('%H:%M')}–{local_end.strftime('%H:%M')}"
        date_str = local.strftime("%Y-%m-%d")
        key = bucket_key(hstart, proj_key)       # proj_key=None ('hour') → tunni-tasandi võti
        if existing_keys is not None and key in existing_keys:
            skipped += 1
            continue                             # juba lehel → ära kuluta LLM-kõnet
        heartbeat_lock(lock)  # pikk run ei tohi teisele protsessile aegunud näida
        summary = summarize(prompts, proj_label, hour_label, cfg)
        notes = notes_by_hour.get(_hour_iso(hstart), [])  # käsitsi-märkmed selle tunni kohta
        if notes:
            summary = f"{summary} · Märge: {' · '.join(notes)}"
        # _cell_safe kasutajast tuletatud lahtritel → ei käivitu valemina (CSV/Sheets injection)
        rows.append([date_str, hour_label, _cell_safe(proj_label), ", ".join(tools), _cell_safe(summary)])
        keys.append(key)
        log(f"  → {date_str} {hour_label} | {proj_label} | {', '.join(tools)} | {summary[:80]}")

    # Märkmed tundidel ILMA jälgitava AI-tegevuseta → eraldi "(märge)"-rida (ei kao kaotsi)
    activity_hours = {_hour_iso(hstart) for (hstart, _) in buckets}
    for hiso, texts in sorted(notes_by_hour.items()):
        if hiso in activity_hours:
            continue                             # juba tegevuse-kokkuvõttesse liidetud
        hstart = parse_iso(hiso)
        if hstart is None or not (last_hour <= hstart < process_until):
            continue                             # väljaspool töödeldavat vahemikku
        key = f"k:{hiso}|__note__"
        if existing_keys is not None and key in existing_keys:
            skipped += 1
            continue
        local = hstart.astimezone(tz)
        local_end = (hstart + dt.timedelta(hours=1)).astimezone(tz)
        hour_label = f"{local.strftime('%H:%M')}–{local_end.strftime('%H:%M')}"
        date_str = local.strftime("%Y-%m-%d")
        summary = "Märge: " + " · ".join(texts)
        rows.append([date_str, hour_label, _cell_safe("(märge)"), "", _cell_safe(summary)])
        keys.append(key)
        log(f"  → {date_str} {hour_label} | (märge) | — | {summary[:80]}")

    if skipped:
        log(f"backfill: {skipped} juba-olemas tundi jäeti vahele (LLM-kõnet ei tehtud)")

    if rows:
        if append_rows(rows, keys, cfg):
            dest = str(_local_path(cfg)) if cfg.get("sink", {}).get("type") == "local" else "Google Sheetsi"
            log(f"run: {len(rows)} rida saadetud → {dest}")
        else:
            log(f"run: {len(rows)} rida EI saadud kirjutada — state'i ei uuendata, proovin uuesti")
            return  # ära uuenda state'i, et read ei kaoks
    else:
        log("run: lõpetatud tundides polnud (uusi) jälgitavate projektide tegevust")

    # Backfill on käsitsi taastamine/test — see EI tohi watermarki muuta (väldib hüppeid/vahelejätmist)
    if backfill_hours is None:
        state["last_processed_hour"] = process_until.isoformat()
        save_state(state)


# --- platvormiülene ajasti seadistus ---------------------------------------
def _platform() -> str:
    s = platform.system().lower()
    if "darwin" in s:
        return "macos"
    if "windows" in s:
        return "windows"
    return "linux"


def install_scheduler() -> None:
    plat = _platform()
    py = sys.executable
    script = str(THIS)
    log(f"install: platvorm={plat}, python={py}")
    if plat == "linux":
        _install_systemd(py, script)
    elif plat == "macos":
        _install_launchd(py, script)
    elif plat == "windows":
        _install_schtasks(py, script)
    else:
        log("install: tundmatu platvorm — seadista ajasti käsitsi käsuga 'aitrack run'")


def _run_logged(cmd: list[str]) -> None:
    """Käivita teardown-käsk ja logi tõrge (mitte vaikselt neela)."""
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        log(f"uninstall: '{' '.join(cmd[:3])}…' rc={res.returncode}: "
            f"{(res.stderr or res.stdout).strip()[:160]}")


def uninstall_scheduler() -> None:
    plat = _platform()
    if plat == "linux":
        for t in ("aitrack.timer", "aitrack-digest.timer"):
            _run_logged(["systemctl", "--user", "disable", "--now", t])
        _run_logged(["systemctl", "--user", "stop", "aitrack.service"])  # peata jooksev oneshot
        for f in ("aitrack.timer", "aitrack.service",
                  "aitrack-digest.timer", "aitrack-digest.service"):
            (HOME / ".config" / "systemd" / "user" / f).unlink(missing_ok=True)
        _run_logged(["systemctl", "--user", "daemon-reload"])
        log("uninstall: systemd timer(id) eemaldatud")
    elif plat == "macos":
        domain = f"gui/{os.getuid()}"
        for name in ("com.aitrack.agent", "com.aitrack.digest"):
            plist = HOME / "Library" / "LaunchAgents" / f"{name}.plist"
            _run_logged(["launchctl", "bootout", domain, str(plist)])
            _run_logged(["launchctl", "unload", "-w", str(plist)])  # vanade macOS-ide jaoks
            plist.unlink(missing_ok=True)
        log("uninstall: launchd agent(id) eemaldatud")
    elif plat == "windows":
        for tn in ("aitrack", "aitrack-digest"):
            _run_logged(["schtasks", "/Delete", "/TN", tn, "/F"])
        log("uninstall: Task Scheduler ülesanne(d) eemaldatud")


def _install_systemd(py: str, script: str) -> None:
    unit_dir = HOME / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    path_env = os.environ.get("PATH", "")
    (unit_dir / "aitrack.service").write_text(
        "[Unit]\n"
        "Description=aitrack — tunnipohine AI-toopaevik\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=oneshot\n"
        f"ExecStart={py} {script} run\n"
        f"Environment=PATH={path_env}\n",
        encoding="utf-8",
    )
    (unit_dir / "aitrack.timer").write_text(
        "[Unit]\nDescription=aitrack kaivitus iga tund (:05)\n\n"
        "[Timer]\nOnCalendar=*-*-* *:05:00\nPersistent=true\nAccuracySec=1min\n\n"
        "[Install]\nWantedBy=timers.target\n",
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "aitrack.timer"], check=False)
    log("install: systemd user-timer lubatud (iga tund :05)")
    subprocess.run(["systemctl", "--user", "list-timers", "aitrack.timer", "--no-pager"], check=False)
    print("\nSoovitus, et timer jookseks ka väljalogituna:")
    print(f"  sudo loginctl enable-linger {os.environ.get('USER', '$USER')}")


def _install_launchd(py: str, script: str) -> None:
    la_dir = HOME / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True, exist_ok=True)
    plist = la_dir / "com.aitrack.agent.plist"
    path_env = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    err_log = str(CONFIG_DIR / "launchd.err.log")
    out_log = str(CONFIG_DIR / "launchd.out.log")
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        "  <key>Label</key><string>com.aitrack.agent</string>\n"
        f"  <key>ProgramArguments</key><array><string>{py}</string>"
        f"<string>{script}</string><string>run</string></array>\n"
        "  <key>StartCalendarInterval</key><dict><key>Minute</key><integer>5</integer></dict>\n"
        f"  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path_env}</string></dict>\n"
        f"  <key>StandardErrorPath</key><string>{err_log}</string>\n"
        f"  <key>StandardOutPath</key><string>{out_log}</string>\n"
        "  <key>RunAtLoad</key><false/>\n"
        "</dict></plist>\n",
        encoding="utf-8",
    )
    # Moderne macOS: bootstrap/kickstart; vanematel langeb tagasi load -w peale.
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)],
                   check=False, capture_output=True)
    res = subprocess.run(["launchctl", "bootstrap", domain, str(plist)],
                         capture_output=True, text=True)
    if res.returncode != 0:
        subprocess.run(["launchctl", "load", "-w", str(plist)], check=False)
    # paigaldusjärgne suitsutest — käivita kohe, et katki plist/PATH avastada nüüd, mitte tunni pärast
    subprocess.run(["launchctl", "kickstart", f"{domain}/com.aitrack.agent"],
                   check=False, capture_output=True)
    log(f"install: launchd agent laetud ({plist}) — iga tund :05")


def _next_hh05() -> str:
    """Järgmine :05 kohaliku aja järgi 'HH:05' vormingus (schtasks /ST jaoks)."""
    now = dt.datetime.now()
    nxt = now.replace(minute=5, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(hours=1)
    return nxt.strftime("%H:%M")


def _install_schtasks(py: str, script: str) -> None:
    # Iga tund minutil :05 (sama nihe kui systemd/launchd). Vahelejäänud tunnid püüab run järele.
    cmd = f'"{py}" "{script}" run'
    res = subprocess.run(
        ["schtasks", "/Create", "/TN", "aitrack", "/TR", cmd,
         "/SC", "HOURLY", "/MO", "1", "/ST", _next_hh05(), "/F"],
        capture_output=True, text=True,
    )
    if res.returncode == 0:
        log("install: Windows Task Scheduler ülesanne 'aitrack' loodud (iga tund :05)")
    else:
        log(f"install: schtasks ebaõnnestus: {(res.stderr or res.stdout).strip()[:300]}")


# --- soovitused / digest / teavitused ---------------------------------------
def suggest_projects(days: int = 30) -> list[tuple[str, int, list[str]]]:
    """Logidest leitud aktiivsed kaustad: [(tee, promptide_arv, tööriistad)], top 12."""
    since = _now_utc() - dt.timedelta(days=days)
    cnt: Counter = Counter()
    tools: dict[str, set] = {}
    for r in collect_records(since):
        cnt[r.project] += 1
        tools.setdefault(r.project, set()).add(r.tool)
    return [(p, n, sorted(tools[p])) for p, n in cnt.most_common(12)]


def compute_digest(cfg: dict, allow: list[str], days: int):
    """Tagastab (aktiivseid_tunde_kokku, per_projekt_tunnid Counter, promptide_arv)."""
    since = _now_utc() - dt.timedelta(days=days)
    bucket_hours: set = set()
    prompts = 0
    for r in collect_records(since):
        proj = match_project(r.project, allow) if allow else None
        if proj is None:
            continue
        prompts += 1
        bucket_hours.add((hour_floor(r.ts), proj))
    per_proj = Counter(proj for (_, proj) in bucket_hours)
    total_hours = len({h for (h, _) in bucket_hours})
    return total_hours, per_proj, prompts


def _notify(title: str, body: str) -> None:
    plat = _platform()
    body = body.replace("\n", " · ")  # teavitused on üherealised
    try:
        if plat == "linux":
            # '--' → body, mis algab '-'-ga, ei tõlgendata lipuna
            subprocess.run(["notify-send", "--", title, body], check=False, capture_output=True)
        elif plat == "macos":
            # ensure_ascii=False → AppleScript kuvab '—' jms õigesti (mitte —)
            esc = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731
            subprocess.run(["osascript", "-e",
                            f"display notification {esc(body)} with title {esc(title)}"],
                           check=False, capture_output=True)
        # Windows: toast on keeruline — print katab
    except OSError:
        pass


def write_ready_apps_script(token: str) -> Path | None:
    """Kirjutab apps-script.gs koopia, kus SECRET on juba täidetud (kasutaja vaid kopeerib).

    Asendab AINULT omistamise rea, mitte vaikesaladuse-kontrolli (muidu jääks check katki)."""
    src = THIS.parent / "apps-script.gs"
    dst = CONFIG_DIR / "apps-script-ready.gs"
    try:
        text = src.read_text(encoding="utf-8").replace(
            'var SECRET = "MUUDA-SEE-ARA";', f"var SECRET = {json.dumps(token)};", 1)
        _mkconfdir()
        dst.write_text(text, encoding="utf-8")
        return dst
    except OSError:
        return None


def interactive_pick_projects() -> None:
    print("\nSkännin logisid…")
    sugg = suggest_projects(30)
    if not sugg:
        print("Logidest ei leitud hiljutist AI-tegevust. Lisa käsitsi: aitrack add <tee>")
        return
    tracked = set(load_projects())
    print("Hiljutine AI-tegevus (vali, mida jälgida):")
    for i, (p, n, tools) in enumerate(sugg, 1):
        mark = "  [jälgitav]" if p in tracked else ""
        print(f"  {i}) {p}  — {n} prompti, {', '.join(tools)}{mark}")
    try:
        sel = input("Numbrid (nt 1,3,4), 'k' = kõik, Enter = jäta vahele: ").strip().lower()
    except EOFError:
        return
    if not sel:
        return
    if sel == "k":
        chosen = [p for p, _, _ in sugg]
    else:
        chosen = [sugg[int(t) - 1][0] for t in sel.replace(" ", "").split(",")
                  if t.isdigit() and 1 <= int(t) <= len(sugg)]
    chosen = [c for c in chosen if c not in tracked]  # ära lisa juba-jälgitavaid uuesti
    if chosen:
        save_projects(load_projects() + chosen)
        print("Lisatud: " + ", ".join(Path(c).name for c in chosen))


def install_digest_scheduler(at: str = "18:00") -> None:
    """Päevane digest-teavitus (eraldi ajasti)."""
    plat, py, script = _platform(), sys.executable, str(THIS)
    if plat == "linux":
        unit_dir = HOME / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "aitrack-digest.service").write_text(
            "[Unit]\nDescription=aitrack paeva-digest\n\n[Service]\nType=oneshot\n"
            f"ExecStart={py} {script} digest --days 1 --notify\n"
            f"Environment=PATH={os.environ.get('PATH', '')}\n", encoding="utf-8")
        (unit_dir / "aitrack-digest.timer").write_text(
            f"[Unit]\nDescription=aitrack digest iga paev {at}\n\n"
            f"[Timer]\nOnCalendar=*-*-* {at}:00\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", "aitrack-digest.timer"], check=False)
        log(f"digest: päevataimer lubatud ({at})")
    elif plat == "macos":
        la = HOME / "Library" / "LaunchAgents" / "com.aitrack.digest.plist"
        la.parent.mkdir(parents=True, exist_ok=True)
        hh, mm = (at.split(":") + ["0"])[:2]
        la.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            "  <key>Label</key><string>com.aitrack.digest</string>\n"
            f"  <key>ProgramArguments</key><array><string>{py}</string><string>{script}</string>"
            "<string>digest</string><string>--days</string><string>1</string><string>--notify</string></array>\n"
            f"  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>{int(hh)}</integer>"
            f"<key>Minute</key><integer>{int(mm)}</integer></dict>\n"
            f"  <key>EnvironmentVariables</key><dict><key>PATH</key><string>"
            f"{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}</string></dict>\n"
            "</dict></plist>\n", encoding="utf-8")
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(la)], check=False, capture_output=True)
        if subprocess.run(["launchctl", "bootstrap", domain, str(la)],
                          capture_output=True).returncode != 0:
            subprocess.run(["launchctl", "load", "-w", str(la)], check=False)
        log(f"digest: launchd päevaagent ({at})")
    elif plat == "windows":
        cmd = f'"{py}" "{script}" digest --days 1 --notify'
        subprocess.run(["schtasks", "/Create", "/TN", "aitrack-digest", "/TR", cmd,
                        "/SC", "DAILY", "/ST", at, "/F"], capture_output=True, text=True)
        log(f"digest: Windows päevaülesanne ({at})")


# --- CLI käsud --------------------------------------------------------------
def cmd_run(args, cfg):
    run_once(cfg, load_projects())


def cmd_backfill(args, cfg):
    run_once(cfg, load_projects(), backfill_hours=args.hours)


def cmd_add(args, cfg):
    paths = load_projects()
    new = str(Path(args.path).expanduser().resolve())
    if not Path(new).is_dir():
        log(f"add: hoiatus — tee ei ole olemasolev kaust: {new}")
    paths.append(new)
    save_projects(paths)
    log(f"add: lisatud {new}")
    cmd_list(args, cfg)


def cmd_remove(args, cfg):
    paths = load_projects()
    target = str(Path(args.path).expanduser().resolve())
    paths = [p for p in paths if p != target]
    save_projects(paths)
    log(f"remove: eemaldatud {target}")
    cmd_list(args, cfg)


def cmd_list(args, cfg):
    paths = load_projects()
    if not paths:
        print("Jälgitavaid projekte pole. Lisa: aitrack add <projekti-tee>")
    else:
        print("Jälgitavad projektid:")
        for p in paths:
            print(f"  • {p}")


def _do_test_sink(cfg: dict) -> bool:
    """Kontrolli väljundit. Lokaalne: kirjutatavus ILMA päris-logisse rida lisamata.
    Sheets: päris testrida (round-trip kontroll)."""
    if cfg.get("sink", {}).get("type") == "local":
        path = _local_path(cfg)
        if path is None:
            print("Lokaalse faili tee puudub. Jooksuta: aitrack setup")
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():  # loo 0o600-ga (ära lisa rida — run lisab hiljem päise)
                os.close(os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600))
            else:
                with path.open("a", encoding="utf-8"):
                    pass
            print(f"OK! Lokaalne fail on kirjutatav: {path}")
            return True
        except OSError as e:
            print(f"EBAÕNNESTUS: {e}")
            return False
    tz = get_tz(cfg)
    now = dt.datetime.now(tz)
    row = [now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), "TEST", "aitrack",
           "Testrida — kui näed seda väljundis, ühendus töötab."]
    key = bucket_key(_now_utc(), "TEST-" + str(int(time.time())))  # uniikne, et test ei dedupiks
    print("Saadan testrea: Google Sheets")
    ok = append_rows([row], [key], cfg)
    print("OK!" if ok else "EBAÕNNESTUS — vaata logi.")
    return ok


def cmd_test_sink(args, cfg):
    _do_test_sink(cfg)


def cmd_preview(args, cfg):
    """Kuiv töötlus: näitab, mida KIRJUTATAKS, ilma Sheetsi saatmata ja state'i muutmata."""
    allow = load_projects()
    if not allow:
        print("Lisa kõigepealt projekt: aitrack add <tee>")
        return
    tz = get_tz(cfg)
    group_by = cfg.get("group_by", "project")  # peegelda run_once grupeerimist
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.hours)
    records = collect_records(since)
    buckets: dict[tuple, list[tuple]] = {}
    for r in records:
        proj = match_project(r.project, allow)
        if proj is None:
            continue
        hstart = hour_floor(r.ts)  # UTC, nagu run_once-is
        bkey = (hstart, None) if group_by == "hour" else (hstart, proj)
        buckets.setdefault(bkey, []).append((r, proj))
    notes_by_hour = load_notes()  # käsitsi-märkmed → kuva vastava tunni all
    unit = "tund" if group_by == "hour" else "tund × projekt"
    print(f"Viimase {args.hours}h jooksul {len(buckets)} ({unit}) bucketit:\n")
    for (hstart, _), items in sorted(buckets.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        recs = [it[0] for it in items]
        local = hstart.astimezone(tz)
        tools = sorted({r.tool for r in recs})
        proj_label = ", ".join(sorted({Path(it[1]).name for it in items}))
        print(f"  {local.strftime('%Y-%m-%d %H:%M')} | {proj_label} | "
              f"{', '.join(tools)} | {len(recs)} prompti")
        for r in recs[:3]:
            print(f"      - [{r.tool}] {r.text[:80]}")
        for note in notes_by_hour.get(_hour_iso(hstart), []):
            print(f"      · Märge: {note}")
    # Märkmed tundidel ILMA tegevuseta (vaatevahemikus) — et ükski märge ei jääks nähtamatuks
    shown = {_hour_iso(hstart) for (hstart, _) in buckets}
    orphans = [(h, txts) for h, txts in sorted(notes_by_hour.items())
               if h not in shown and (parse_iso(h) or since) >= hour_floor(since)]
    if orphans:
        print("\n  Märkmed (tundidel ilma jälgitava tegevuseta):")
        for h, txts in orphans:
            local = parse_iso(h).astimezone(tz)
            print(f"      {local.strftime('%Y-%m-%d %H:%M')} · Märge: {' · '.join(txts)}")


def cmd_note(args, cfg):
    """Lisa käsitsi-märge praegusele tunnile; ilma tekstita → kuva olemasolevad märkmed."""
    text = " ".join(args.text).strip()
    tz = get_tz(cfg)
    if not text:
        notes = load_notes()
        if not notes:
            print('Märkmeid pole. Lisa: aitrack note "mida õppisid või tegid"')
            return
        print("Märkmed:")
        for hiso in sorted(notes):
            local = (parse_iso(hiso) or _now_utc()).astimezone(tz)
            for t in notes[hiso]:
                print(f"  {local.strftime('%Y-%m-%d %H:%M')} · {t}")
        return
    now = _now_utc()
    append_note(now, text)
    start = hour_floor(now).astimezone(tz)
    end = (hour_floor(now) + dt.timedelta(hours=1)).astimezone(tz)
    log(f"note: lisatud tundi {start.strftime('%Y-%m-%d %H:%M')} — {text[:80]}")
    print(f"Lisatud tundi {start.strftime('%Y-%m-%d %H:%M')}–{end.strftime('%H:%M')}: {text}")


def cmd_init(args, cfg):
    """Loob config.json ja küsib Sheetsi seaded."""
    existing = load_config()
    url = args.url
    token = args.token
    # Mitte-interaktiivne (nt 'curl | bash', CI): nõua lippe, ära salvesta vaikselt tühja
    if (url is None or token is None) and not sys.stdin.isatty():
        print("init: mitte-interaktiivne käivitus — anna --url ja --token, nt:")
        print("  aitrack init --url https://script.google.com/.../exec --token SINU-SALADUS")
        sys.exit(2)
    if url is None:
        cur = existing["sink"].get("webapp_url", "")
        try:
            url = input(f"Google Sheets webapp URL [{cur or 'tühi'}]: ").strip() or cur
        except EOFError:
            url = cur
    if token is None:
        cur = existing["sink"].get("token", "")
        try:  # ÄRA kuva token-väärtust (satuks scrollbacki/ajalukku)
            token = input(f"Sheetsi token (apps-script.gs SECRET) [{'(seatud)' if cur else 'tühi'}]: ").strip() or cur
        except EOFError:
            token = cur
    cfg2 = existing
    cfg2["sink"]["webapp_url"] = url
    cfg2["sink"]["token"] = token
    if args.engine:
        cfg2["summarizer"]["engine"] = args.engine
    if args.timezone:
        cfg2["timezone"] = args.timezone
    save_config(cfg2)
    if not PROJECTS_FILE.exists():
        save_projects([])
    eng, exe = resolve_engine(cfg2)
    log(f"init: config salvestatud → {CONFIG_FILE}")
    print(f"Kokkuvõtja mootor: {eng}" + (f" ({exe})" if exe else " — ÜHTEGI AI-CLI-d ei leitud!"))
    print("Järgmised sammud:")
    print("  1. aitrack test-sink           # kontrolli Sheetsi ühendust")
    print("  2. aitrack add <projekti-tee>  # lisa jälgitav projekt")
    print("  3. aitrack install             # seadista tunniajasti")


def cmd_install(args, cfg):
    if not CONFIG_FILE.exists():
        print("Hoiatus: config.json puudub. Jooksuta enne: aitrack init")
    # Lahenda mootori absoluuttee ja salvesta — ajasti võib joosta minimaalse/triiviva PATH-iga
    eng, exe = resolve_engine(cfg)
    if exe and Path(exe).is_absolute():
        cfg.setdefault("summarizer", {})["exe"] = exe
        save_config(cfg)
        log(f"install: mootor '{eng}' lukustatud teele: {exe}")
    elif eng == "none":
        log("install: HOIATUS — AI-CLI mootorit ei leitud; kokkuvõtted tulevad varurežiimis")
    install_scheduler()


def cmd_uninstall(args, cfg):
    uninstall_scheduler()


def cmd_status(args, cfg):
    plat = _platform()
    eng, exe = resolve_engine(cfg)
    print(f"Platvorm:        {plat}")
    print(f"Python:          {sys.executable}")
    print(f"Config:          {CONFIG_FILE} ({'olemas' if CONFIG_FILE.exists() else 'PUUDUB'})")
    sink = cfg.get("sink", {})
    if sink.get("type") == "local":
        path = _local_path(cfg)  # lahendab ka vaiketee (~/aitrack-log.csv), kui path on tühi
        exists = "olemas" if path and path.exists() else "puudub veel"
        print(f"Väljund:         lokaalne CSV → {path} ({exists})")
    else:
        print(f"Väljund:         Google Sheets ({'seadistatud' if sink.get('webapp_url') else 'URL PUUDUB'})")
    print(f"Kokkuvõtja:      {eng}" + (f" ({exe})" if exe else " — AI-CLI puudub"))
    print(f"Projekte:        {len(load_projects())}")
    raw = load_state()  # None = rikutud (ära kuku kokku diagnoosikäsus)
    if raw is None:
        print("Viimati töödeldud: RIKUTUD state.json — kustuta ~/.config/aitrack/state.json")
    else:
        print(f"Viimati töödeldud: {raw.get('last_processed_hour', '(pole veel)')}")
    print("Tuvastatud logiallikad:")
    for name, p in [("Claude", CLAUDE_PROJECTS), ("Codex", CODEX_HISTORY),
                    ("Antigravity", ANTIGRAVITY_HISTORY)]:
        print(f"  {name:12} {'✓' if p.exists() else '–'}  {p}")


def cmd_suggest(args, cfg):
    sugg = suggest_projects(args.days)
    if not sugg:
        print(f"Viimase {args.days} päeva logidest ei leitud AI-tegevust.")
        return
    allow = set(load_projects())
    print(f"Aktiivsed AI-kaustad (viimased {args.days}p):")
    for p, n, tools in sugg:
        mark = "  [jälgitav]" if p in allow else ""
        print(f"  {n:5} prompti  {p}  ({', '.join(tools)}){mark}")
    print("\nLisa jälgimisse: aitrack add <tee>")


def cmd_digest(args, cfg):
    allow = load_projects()
    if not allow:
        print("Pole jälgitavaid projekte (aitrack add <tee>).")
        return
    total_hours, per_proj, prompts = compute_digest(cfg, allow, args.days)
    if not per_proj:
        msg = f"Viimased {args.days}p: AI-tegevust polnud."
    else:
        parts = ", ".join(f"{Path(p).name} {n}h" for p, n in per_proj.most_common())
        msg = f"AI-töö {args.days}p: {total_hours} aktiivset tundi, {prompts} prompti — {parts}"
    print(msg)
    if getattr(args, "notify", False):
        _notify("aitrack", msg)


def cmd_setup(args, cfg):
    """Interaktiivne seadistus: sink → mootor → projektid → ajasti → test (üks voog)."""
    if not sys.stdin.isatty():
        print("setup vajab interaktiivset terminali. Kasuta: aitrack init / add / install.")
        return
    try:
        _setup_flow()
    except EOFError:
        print("\nSeadistus katkestatud (EOF). Jätka hiljem: aitrack setup")


def _setup_flow() -> None:
    print("=== aitrack seadistus ===\n")
    cfg = load_config()
    print("Kuhu tulemused salvestada?")
    print("  1) Google Sheets — jagatav, pilves (~5 min Apps Scripti seadistust)")
    print("  2) Lokaalne CSV-fail — kohe, ilma Google'ita")
    ch = input("Vali [1/2] (vaikimisi 2): ").strip() or "2"
    if ch == "1":
        token = secrets.token_urlsafe(12)
        ready = write_ready_apps_script(token)
        print("\n--- Google Sheetsi seadistus ---")
        print("1) Ava uus leht:  https://sheets.new")
        print("2) Laiendused → Apps Script → kustuta näidiskood")
        if ready:
            print(f"3) Kleebi sinna kogu fail:  {ready}")
            print("   (token on juba sees — midagi muuta pole vaja)")
        else:
            print(f"3) Kleebi apps-script.gs ja sea SECRET = {token}")
        print("4) Deploy → New deployment → Web app (Execute: Me, Access: Anyone)")
        url = ""
        while not url:
            url = input("5) Kleebi Web app URL (.../exec): ").strip()
        cfg["sink"] = {"type": "google_sheets", "webapp_url": url, "token": token, "path": ""}
    else:
        default_path = str(DEFAULT_CSV_PATH)
        path = input(f"CSV-faili tee [{default_path}]: ").strip() or default_path
        cfg["sink"] = {"type": "local", "path": path, "webapp_url": "", "token": ""}
        print(f"→ tulemused lähevad faili: {path}")

    eng, exe = resolve_engine(cfg)
    if exe and Path(exe).is_absolute():
        cfg.setdefault("summarizer", {})["exe"] = exe
    save_config(cfg)
    print(f"\nKokkuvõtja mootor: {eng}" + (f" ({exe})" if exe else " — HOIATUS: AI-CLI-d ei leitud"))
    if not PROJECTS_FILE.exists():
        save_projects([])

    interactive_pick_projects()

    print("\nSeadistan tunniajasti...")
    install_scheduler()

    if input("\nKas tahad iga õhtu kokkuvõtte-teavitust (18:00)? [j/N]: ").strip().lower().startswith("j"):
        install_digest_scheduler("18:00")

    print("\nTestin väljundit...")
    _do_test_sink(load_config())

    if not load_projects():
        print("\n(Projekte pole veel — lisa hiljem: aitrack add <tee>)")
    elif input("Täita viimased 12h kohe? [J/n]: ").strip().lower() != "n":
        run_once(load_config(), load_projects(), backfill_hours=12)

    print("\n✓ Valmis! Tööriist jookseb edaspidi iga tund. Olek: aitrack status")


def main():
    cfg = load_config()
    p = argparse.ArgumentParser(prog="aitrack", description="AI-tööriistade tunnipõhine tööpäevik")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="interaktiivne seadistus algusest lõpuni (soovitatav)").set_defaults(fn=cmd_setup)

    ini = sub.add_parser("init", help="seadista config (Sheetsi URL + token)")
    ini.add_argument("--url"); ini.add_argument("--token")
    ini.add_argument("--engine", choices=["auto", "claude", "codex", "gemini", "none"])
    ini.add_argument("--timezone")
    ini.set_defaults(fn=cmd_init)

    sg = sub.add_parser("suggest", help="näita logidest aktiivseid projektikaustu")
    sg.add_argument("--days", type=int, default=30)
    sg.set_defaults(fn=cmd_suggest)

    dg = sub.add_parser("digest", help="päeva/nädala kokkuvõte (valikuliselt teavitus)")
    dg.add_argument("--days", type=int, default=1)
    dg.add_argument("--notify", action="store_true")
    dg.set_defaults(fn=cmd_digest)

    sub.add_parser("install", help="seadista OS-i tunniajasti (systemd/launchd/Task Scheduler)").set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="eemalda ajasti").set_defaults(fn=cmd_uninstall)
    sub.add_parser("status", help="näita seadistust ja tuvastatud logiallikaid").set_defaults(fn=cmd_status)
    sub.add_parser("run", help="töötle lõpetatud tunnid (ajasti kutsub seda)").set_defaults(fn=cmd_run)

    bf = sub.add_parser("backfill", help="töötle viimased N tundi tagasiulatuvalt")
    bf.add_argument("--hours", type=int, default=6)
    bf.set_defaults(fn=cmd_backfill)

    pv = sub.add_parser("preview", help="kuiv vaade — mida kirjutataks (ei saada Sheetsi)")
    pv.add_argument("--hours", type=int, default=6)
    pv.set_defaults(fn=cmd_preview)

    ad = sub.add_parser("add", help="lisa projekt jälgimisse")
    ad.add_argument("path"); ad.set_defaults(fn=cmd_add)

    rm = sub.add_parser("remove", help="eemalda projekt jälgimisest")
    rm.add_argument("path"); rm.set_defaults(fn=cmd_remove)

    sub.add_parser("list", help="näita jälgitavaid projekte").set_defaults(fn=cmd_list)
    sub.add_parser("test-sink", help="saada testrida Google Sheetsi").set_defaults(fn=cmd_test_sink)

    nt = sub.add_parser("note", help='lisa käsitsi-märge praegusele tunnile (nt: aitrack note "õppisin X")')
    nt.add_argument("text", nargs="*", help="märkme tekst; tühjalt = kuva olemasolevad märkmed")
    nt.set_defaults(fn=cmd_note)

    args = p.parse_args()
    args.fn(args, cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
