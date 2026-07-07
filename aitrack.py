#!/usr/bin/env python3
"""aitrack — AI-tööriistade tunnipõhine tööpäevik (macOS / Windows / Linux).

Vaatab iga tund läbi sinu Claude Code / Codex / Antigravity / Pi / OpenCode sessioonid,
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
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
CLIENT_FILE = CONFIG_DIR / "client.json"  # selle arvuti püsiv client_id serveri jaoks
WORK_STATE_FILE = CONFIG_DIR / "work-state.json"  # aktiivsed serveri work_session'id sellel kliendil
SERVER_DB = CONFIG_DIR / "server.db"  # keskserveri SQLite andmebaas (aitrack serve)
DEFAULT_CSV_PATH = HOME / "aitrack-log.csv"  # lokaalse sink'i vaiketee (masinapõhine, ei lähe git'i)
HOURS_CSV = CONFIG_DIR / "hours.csv"  # sisemine tunnipõhine algandmestik (dedup + 4 välja); päevavaade renderdatakse siit

# AI-tööriistade logiasukohad — samad kõigil OS-idel (~ = kasutaja kodukaust)
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CODEX_HISTORY = HOME / ".codex" / "history.jsonl"
ANTIGRAVITY_HISTORY = HOME / ".gemini" / "antigravity-cli" / "history.jsonl"
PI_SESSIONS = HOME / ".pi" / "agent" / "sessions"
OPENCODE_DB = HOME / ".local" / "share" / "opencode" / "opencode.db"


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
    # rea tase: "hour" = üks tunnipunkt, KÕIK kaustad koos (vaikimisi, sobib päevavaatega);
    #           "project" = eraldi tunnipunkt iga (tund × projekt) kohta
    "group_by": "hour",
    # type: "local" (CSV-fail, ilma Google'ita — VAIKIMISI) või "google_sheets" (Apps Script)
    # path tühi + type "local" → _local_path annab DEFAULT_CSV_PATH (~/aitrack-log.csv)
    "sink": {"type": "local", "webapp_url": "", "token": "", "path": ""},
    # kaust → ärinimi päevavaates (nt "pp-finar": "Puhastusproff – Finar"), et väljundis poleks kaustanime
    "object_names": {},
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
_WORKTREE_MAIN_CACHE: dict[str, str | None] = {}


def _git_worktree_main(record_project: str) -> str | None:
    """Kui tee on git worktree, tagasta põhitööpuu juur.

    See laseb lubatud projektil `/repo` katta ka sibling-worktree'd nagu
    `/repo-662`, mille `.git` fail viitab `/repo/.git/worktrees/...` alla.
    """
    try:
        start = Path(record_project).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        start = Path(record_project).expanduser()
    cache_key = str(start)
    if cache_key in _WORKTREE_MAIN_CACHE:
        return _WORKTREE_MAIN_CACHE[cache_key]

    result: str | None = None
    for root in (start, *start.parents):
        gitp = root / ".git"
        try:
            if gitp.is_file():
                first = gitp.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
                if not first.lower().startswith("gitdir:"):
                    continue
                raw = first.split(":", 1)[1].strip()
                gitdir = Path(raw)
                if not gitdir.is_absolute():
                    gitdir = (root / gitdir)
                parts = gitdir.resolve().parts
                if ".git" in parts:
                    idx = parts.index(".git")
                    if idx > 0:
                        result = str(Path(*parts[:idx]).resolve())
                        break
            elif gitp.is_dir():
                result = str(root.resolve())
                break
        except (OSError, ValueError, RuntimeError, IndexError):
            continue

    _WORKTREE_MAIN_CACHE[cache_key] = result
    return result


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
    ning sibling-vasteid (nt /a/proj vs /a/proj2). Git worktree puhul kontrollib
    lisaks põhitööpuu juurt, et `/repo-662` läheks lubatud `/repo` alla.
    """
    try:
        rp = os.path.normcase(str(Path(record_project).expanduser().resolve()))
    except (OSError, ValueError, RuntimeError):
        rp = os.path.normcase(record_project)

    def _best_for(parts) -> str | None:
        best = None
        best_len = -1
        for p in allow:
            pparts = Path(os.path.normcase(p)).parts
            if parts[: len(pparts)] == pparts and len(pparts) > best_len:
                best, best_len = p, len(pparts)
        return best

    direct = _best_for(Path(rp).parts)
    if direct is not None:
        return direct

    main = _git_worktree_main(record_project)
    if main:
        return _best_for(Path(os.path.normcase(main)).parts)
    return None


# --- ühise projekti tuvastus (serveri work tracking) ------------------------
def _git_capture(args: list[str], cwd: Path) -> str:
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=3)
    except Exception:  # noqa: BLE001 - git võib puududa või cwd olla kustunud
        return ""
    if p.returncode != 0:
        return ""
    return p.stdout.strip()


def _normalise_repo_url(url: str) -> str:
    """Muuda git remote eri kujud samaks projektivõtmeks.

    Näited:
      git@github.com:Org/Repo.git -> github.com/org/repo
      https://github.com/Org/Repo.git -> github.com/org/repo
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    raw = raw.removesuffix("/")
    parsed = urllib.parse.urlparse(raw)
    host = parsed.hostname or ""
    path = parsed.path.strip("/")
    if host and path:
        path = path[:-4] if path.lower().endswith(".git") else path
        return f"{host.lower()}/{path.lower()}"
    m = re.match(r"^(?:[^@\s]+@)?([^:\s]+):(.+)$", raw)
    if m:
        host, path = m.group(1), m.group(2).strip("/")
        path = path[:-4] if path.lower().endswith(".git") else path
        return f"{host.lower()}/{path.lower()}"
    p = raw[:-4] if raw.lower().endswith(".git") else raw
    return p.lower()


def _issue_provider_for_project_key(project_key: str) -> str:
    key = (project_key or "").lower()
    if key.startswith("github.com/"):
        return "github"
    if key.startswith("bitbucket.org/"):
        return "bitbucket"
    if key.startswith("gitlab.com/"):
        return "gitlab"
    return "local"


def _normalise_issue_key(value: str | None) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    v = re.sub(r"^issue[:#\s-]*", "", v, flags=re.I)
    v = v.lstrip("#")
    return v.strip()


def _issue_from_branch(branch: str) -> str:
    b = branch or ""
    # Eelista haru alguses või kaldkriipsu järel olevat issue numbrit: 662-x, gh-662-x, fix/662-x.
    m = re.search(r"(?:^|/)(?:gh-|bb-)?#?(\d{2,7})(?=$|[-_/])", b, flags=re.I)
    return m.group(1) if m else ""


def _normalise_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _project_context(path: str | Path = ".", issue: str | None = None) -> dict:
    """Tagasta kliendi/serveri ühine projektifingerprint.

    Git repo korral on põhinimi normaliseeritud remote URL; muidu fallback on local:<kaustanimi>.
    Local path jääb alles ainult checkout'i tõendiks, mitte projekti ühise identifikaatorina.
    """
    cwd = Path(path).expanduser()
    try:
        cwd = cwd.resolve()
    except (OSError, RuntimeError):
        pass
    root_s = _git_capture(["rev-parse", "--show-toplevel"], cwd)
    root = Path(root_s) if root_s else cwd
    remote = _git_capture(["config", "--get", "remote.origin.url"], root)
    branch = _git_capture(["branch", "--show-current"], root) or _git_capture(["rev-parse", "--abbrev-ref", "HEAD"], root)
    project_key = _normalise_repo_url(remote)
    if not project_key:
        project_key = f"local:{root.name.lower()}"
    name = project_key.rstrip("/").split("/")[-1] if "/" in project_key else root.name
    issue_key = _normalise_issue_key(issue) or _issue_from_branch(branch)
    checkout_seed = f"{project_key}|{str(root)}".encode("utf-8", errors="replace")
    return {
        "project_key": project_key,
        "repo_url": remote,
        "name": name,
        "local_path": str(root),
        "cwd": str(cwd),
        "branch": branch or "",
        "issue_key": issue_key,
        "issue_provider": _issue_provider_for_project_key(project_key),
        "checkout_id": hashlib.sha1(checkout_seed).hexdigest()[:16],
    }


def _client_info() -> dict:
    """Püsiv, mitte-salajane client_id selle seadme eristamiseks serveris."""
    _mkconfdir()
    data = {}
    if CLIENT_FILE.exists():
        try:
            data = json.loads(CLIENT_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    if not data.get("client_id"):
        data = {
            "client_id": f"client-{secrets.token_hex(12)}",
            "name": socket.gethostname() or platform.node() or "unknown",
            "platform": _platform(),
            "created_at": _now_utc().isoformat(),
        }
        _atomic_write_json(CLIENT_FILE, data, 0o600)
    data.setdefault("name", socket.gethostname() or platform.node() or "unknown")
    data.setdefault("platform", _platform())
    return data


def _detect_cli(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip().lower()
    for key in ("AITRACK_TOOL", "AGENT_TASK_CLI", "AGENT_CLI", "AI_TOOL"):
        val = os.environ.get(key, "").strip()
        if val:
            return val.lower()
    return "manual"


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


def _load_work_state() -> dict:
    if WORK_STATE_FILE.exists():
        try:
            data = json.loads(WORK_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("sessions", [])
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"sessions": []}


def _save_work_state(state: dict) -> None:
    state.setdefault("sessions", [])
    _atomic_write_json(WORK_STATE_FILE, state, 0o600)


def _work_state_key(client_id: str, checkout_id: str, tool: str) -> str:
    raw = f"{client_id}|{checkout_id}|{tool.lower()}".encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def _active_work_sessions(*, tool: str | None = None, checkout_id: str | None = None) -> list[dict]:
    out = []
    for s in _load_work_state().get("sessions", []):
        if s.get("status") != "active":
            continue
        if tool and s.get("tool") != tool:
            continue
        if checkout_id and s.get("checkout_id") != checkout_id:
            continue
        out.append(s)
    return out


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


def _content_text(content) -> str:
    """Võta AI-logide erinevatest content-kujudest kokku kasutaja tekst."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif isinstance(b, str):
                parts.append(b)
        return " ".join(parts)
    return ""


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
                    text = _content_text(msg.get("content"))
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


def pi_records(since: dt.datetime) -> list[Record]:
    """Loe Pi agenti JSONL-sessioonidest kasutaja promptid.

    Pi hoiab iga sessiooni alguses `cwd`-d ning sõnumid on kujul
    `{type:"message", message:{role:"user", content:[...]}}`.
    """
    out: list[Record] = []
    if not PI_SESSIONS.is_dir():
        return out
    cutoff = since.timestamp()
    for jsonl in PI_SESSIONS.rglob("*.jsonl"):
        try:
            if jsonl.stat().st_mtime < cutoff - 3600:
                continue
        except OSError:
            continue
        cwd = ""
        try:
            with jsonl.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if '"session"' not in line and '"user"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") == "session":
                        cwd = d.get("cwd") or cwd
                        continue
                    if d.get("type") != "message":
                        continue
                    msg = d.get("message") or {}
                    if msg.get("role") != "user":
                        continue
                    ts = parse_iso(d.get("timestamp", ""))
                    if ts is None:
                        ts_raw = msg.get("timestamp")
                        if isinstance(ts_raw, (int, float)):
                            ts = from_epoch(ts_raw, "ms" if ts_raw > 10_000_000_000 else "s")
                    if ts is None or ts <= since:
                        continue
                    text = _content_text(msg.get("content"))
                    if not is_user_prompt(text):
                        continue
                    if cwd:
                        out.append(Record("Pi", cwd, ts, text.strip()))
        except OSError:
            continue
    return out


def opencode_records(since: dt.datetime) -> list[Record]:
    """Loe OpenCode'i SQLite-andmebaasist kasutaja tekstiosad.

    Loeme ainult `session`/`message`/`part` tabeleid; konto- ja credential-tabeleid
    ei puudutata. Ajad on OpenCode'is millisekundites.
    """
    out: list[Record] = []
    if not OPENCODE_DB.exists():
        return out
    since_ms = int(since.timestamp() * 1000)
    try:
        db = OPENCODE_DB.resolve()
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return out
    try:
        cur = conn.execute(
            "SELECT p.time_created, s.directory, p.data, m.data "
            "FROM part p "
            "JOIN message m ON p.message_id = m.id "
            "JOIN session s ON p.session_id = s.id "
            "WHERE p.time_created > ? "
            "ORDER BY p.time_created",
            (since_ms,),
        )
        for ts_raw, directory, part_data, msg_data in cur:
            try:
                msg = json.loads(msg_data or "{}")
                part = json.loads(part_data or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if msg.get("role") != "user" or part.get("type") != "text":
                continue
            ts = from_epoch(float(ts_raw), "ms")
            if ts <= since:
                continue
            text = str(part.get("text", ""))
            if not is_user_prompt(text):
                continue
            if directory:
                out.append(Record("OpenCode", str(directory), ts, text.strip()))
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def collect_records(since: dt.datetime) -> list[Record]:
    recs = (claude_records(since) + codex_records(since) + antigravity_records(since) +
            pi_records(since) + opencode_records(since))
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


# Päevavaate 4 analüütilist välja (praktikapäeviku vorm). Järjekord = veergude järjekord.
ITEM_FIELDS = ("objekt", "saavutus", "takistus", "teadmine")
_NA = "Ei olnud"  # tühja Takistus/Teadmine vaikeväärtus (hoiab numbrid veergudes kohakuti)


def _object_context(project_label: str, cfg: dict) -> str:
    """Ehita LLM-ile objekti-vihje ärinimede kaardist. Kaardistamata kausta EI lekita
    (kasutaja ei taha kaustanimesid väljundis) — siis tugineb LLM promptide sisule."""
    names = cfg.get("object_names") or {}
    mapped = []
    for name in [p.strip() for p in project_label.split(",") if p.strip()]:
        disp = names.get(name)
        if disp and disp not in mapped:
            mapped.append(disp)
    return f" Seotud objekt(id): {', '.join(mapped)}." if mapped else ""


def _parse_item(text: str, prompts: list[str]) -> dict:
    """Parsi LLM-i 4-realine vastus (OBJEKT:/SAAVUTUS:/TAKISTUS:/TEADMINE:) dict-iks.
    Robustne: võtmed suur/väiketäht ükskõik, järjekord vaba, puuduvad väljad täidetakse."""
    got = {k: "" for k in ITEM_FIELDS}
    aliases = {"objekt": "objekt", "saavutus": "saavutus", "saavutused": "saavutus",
               "takistus": "takistus", "takistused": "takistus",
               "teadmine": "teadmine", "teadmised": "teadmine", "uued teadmised": "teadmine"}
    for raw in text.splitlines():
        line = raw.strip().lstrip("-•* ").strip()
        if ":" not in line:
            continue
        head, _, rest = line.partition(":")
        key = aliases.get(head.strip().lower())
        if key and not got[key]:
            got[key] = rest.strip()
    if not got["objekt"]:  # LLM ei pidanud vormist kinni → pane kogu tekst objekti
        got["objekt"] = text.replace("\n", " ").strip()[:400] or _fallback_item(prompts)["objekt"]
    if not got["saavutus"]:
        got["saavutus"] = got["objekt"]
    got["takistus"] = got["takistus"] or _NA
    got["teadmine"] = got["teadmine"] or _NA
    return {k: v[:400] for k, v in got.items()}


def summarize(prompts: list[str], project_label: str, hour_label: str, cfg: dict) -> dict:
    """Tagasta ühe tunni kohta 4-väljaline dict (objekt/saavutus/takistus/teadmine)."""
    engine, exe = resolve_engine(cfg)
    if engine == "none" or exe is None:
        return _fallback_item(prompts)

    joined = "\n".join(f"- {p[:300]}" for p in prompts)
    if len(joined) > 8000:  # piira koondprompti suurust
        joined = joined[:8000] + "\n…(kärbitud)"
    prompt = (
        "Sa teed eestikeelseid kokkuvõtteid arendustööst praktikapäeviku jaoks. "
        f"Allpool on kasutaja AI-promptid ühe tunni ({hour_label}) jooksul."
        f"{_object_context(project_label, cfg)} "
        "Kirjelda TÖÖ SISU põhjal (ÄRA maini kaustanimesid ega failiteid). "
        "Vasta TÄPSELT neljal real, iga rida algab märksõnaga:\n"
        "OBJEKT: <objekt/klient ja ülesanne, üks lause, kuni ~15 sõna>\n"
        "SAAVUTUS: <mis sai tehtud või valmis, üks lause>\n"
        "TAKISTUS: <mis takistas; kui takistust polnud, kirjuta täpselt: Ei olnud>\n"
        "TEADMINE: <mida uut õpiti; kui uut polnud, kirjuta täpselt: Ei olnud>\n"
        "Ära lisa midagi peale nende nelja rea.\n\n"
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
            return _parse_item(text, prompts)
        log(f"summarize: {engine} rc={res.returncode} err={(res.stderr or '')[:200]}")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(f"summarize: {engine} ebaõnnestus ({e}) — kasutan varvarianti")
    return _fallback_item(prompts)


def _fallback_item(prompts: list[str]) -> dict:
    """Geneeriline — EI pane toorest promptisisu lehele (privaatsus)."""
    n = len(prompts)
    objekt = f"({n} prompti, automaatkokkuvõte puudub)" if n else "(tegevus tuvastatud)"
    return {"objekt": objekt, "saavutus": objekt, "takistus": _NA, "teadmine": _NA}


# --- sink: Google Sheets VÕI lokaalne CSV -----------------------------------
# Sisemine tunnipõhine algandmestik (allikas dedup'iks ja päevavaate renderdamiseks).
RAW_HEADER = ["Kuupäev", "Tund", "Objekt ja ülesanne", "Saavutused",
              "Takistused", "Uued teadmised", "Tööriist", "_key"]
# Kasutaja kleebitav päevavaade (üks rida päevas). Veergude järjekord = Google Sheeti A–G.
DAY_HEADER = ["Kuupäev", "Punkte", "Nädalapäev", "Objekt ja ülesanne",
              "Saavutused", "Takistused", "Uued teadmised"]
WEEKDAY_ET = ["E", "T", "K", "N", "R", "L", "P"]  # weekday() 0=E(smaspäev) … 6=P(ühapäev)
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _weekday_letter(date_str: str) -> str:
    """YYYY-MM-DD → eesti nädalapäeva algustäht (E/T/K/N/R/L/P)."""
    try:
        return WEEKDAY_ET[dt.date.fromisoformat(date_str).weekday()]
    except ValueError:
        return "?"


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
                if len(row) >= len(RAW_HEADER) and row[-1].startswith("k:"):
                    out.add(row[-1])
    except OSError:
        pass
    return out


def _create_private(path: Path) -> None:
    """Loo fail 0o600-ga (sisaldab tundlikke kokkuvõtteid), kui puudub."""
    try:
        os.close(os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600))
    except OSError:
        pass


def _raw_append(rows: list[list], keys: list[str]) -> bool:
    """Lisa tunnipõhised read sisemisse algandmestikku (HOURS_CSV), dedup võtme järgi."""
    path = HOURS_CSV
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _local_keys(path)
        is_new = not path.exists() or path.stat().st_size == 0
        if is_new:
            _create_private(path)
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(RAW_HEADER)
            for row, k in zip(rows, keys):
                if k in existing:
                    continue  # dedup, nagu Apps Scriptis
                w.writerow(list(row) + [k])  # read on juba _cell_safe'iga ehitatud
                existing.add(k)
        return True
    except OSError as e:
        log(f"sink: algandmestiku kirjutus ebaõnnestus: {e}")
        return False


def _read_raw_days() -> "dict[str, list[list]]":
    """Loe HOURS_CSV → {kuupäev: [tunnirida, …]} tunni järgi sorteeritult.
    Tunnirida = [Kuupäev, Tund, Objekt, Saavutused, Takistused, Uued teadmised, Tööriist, _key]."""
    days: dict[str, list[list]] = {}
    if not HOURS_CSV.exists():
        return days
    try:
        with HOURS_CSV.open(newline="", encoding="utf-8") as f:
            rdr = csv.reader(f)
            header = next(rdr, None)  # jäta päis vahele
            for row in rdr:
                if len(row) < len(RAW_HEADER) or row == header:
                    continue
                days.setdefault(row[0], []).append(row)
    except OSError as e:
        log(f"render: algandmestiku lugemine ebaõnnestus: {e}")
    for date in days:
        days[date].sort(key=lambda r: r[1])  # Tund (nt "10:00–11:00") stringina → kronoloogiline
    return days


def _day_row(date: str, hour_rows: list[list]) -> list:
    """Kokku üks päevarida: iga tund = nummerdatud punkt, numbrid kõigis 4 veerus kohakuti."""
    # eemalda välja SEEST reavahetused → ainsad reavahetused on punktide vahel (join),
    # nii ei teki kleepimisel valeridu ega peitunud valemisüsti (=… uue rea alguses)
    clean = lambda s: str(s).replace("\r", " ").replace("\n", " ").strip()  # noqa: E731
    cols = {k: [] for k in ("objekt", "saavutus", "takistus", "teadmine")}
    for i, r in enumerate(hour_rows, 1):
        cols["objekt"].append(f"{i}. {clean(r[2])}")
        cols["saavutus"].append(f"{i}. {clean(r[3])}")
        cols["takistus"].append(f"{i}. {clean(r[4])}")
        cols["teadmine"].append(f"{i}. {clean(r[5])}")
    join = lambda xs: "\n".join(xs)  # noqa: E731
    return [date, len(hour_rows), _weekday_letter(date),
            join(cols["objekt"]), join(cols["saavutus"]),
            join(cols["takistus"]), join(cols["teadmine"])]


def _migrate_old_log(cfg: dict) -> None:
    """Ühekordne: teisenda vana 5-veeru päevalog (~/aitrack-log.csv) uude tunnipõhisesse
    algandmestikku, et vana andmestik ei kaoks päevavaate ümberrenderdamisel.
    Käivitub AINULT kui HOURS_CSV veel puudub JA sink-fail on vanas formaadis."""
    if HOURS_CSV.exists():
        return  # juba migreeritud / uus paigaldus
    path = _local_path(cfg)
    if path is None or not path.exists():
        return
    rows: list[list] = []
    keys: list[str] = []
    try:
        with path.open(newline="", encoding="utf-8") as f:
            rdr = csv.reader(f)
            header = next(rdr, None)
            if not header or "Töö kokkuvõte" not in header:
                return  # pole vana formaat (võib juba olla päevavaade) — ära puutu
            for row in rdr:
                if len(row) < 6 or not row[-1].startswith("k:"):
                    continue
                date, hour, proj, tool, summary, key = (
                    row[0], row[1], row[2], row[3], row[4], row[-1])
                objekt, teadmine = summary, _NA
                if proj == "(märge)":       # vana märkme-rida → objekt "(märge)", tekst teadmisse
                    objekt, teadmine = "(märge)", summary
                elif "· Märge:" in summary:  # tegevus + märge → tõsta märge teadmiste veergu
                    objekt, _, note = summary.partition("· Märge:")
                    objekt, teadmine = objekt.strip(), "Märge: " + note.strip()
                rows.append([date, hour, _cell_safe(objekt), _cell_safe(_NA),
                             _cell_safe(_NA), _cell_safe(teadmine), tool])
                keys.append(key)
    except OSError as e:
        log(f"migrate: vana logi lugemine ebaõnnestus: {e}")
        return
    if rows and _raw_append(rows, keys):
        log(f"migrate: {len(rows)} vana tunnirida → {HOURS_CSV} (Objekt-veergu; täpsusta käsitsi)")


def _render_day_view(cfg: dict) -> bool:
    """Renderda kogu päevavaade (üks rida päevas) HOURS_CSV põhjal sink-faili. Idempotentne."""
    path = _local_path(cfg)
    if path is None:
        log("sink: lokaalse faili tee puudub configis")
        return False
    days = _read_raw_days()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _create_private(tmp)
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(DAY_HEADER)
            for date in sorted(days):
                w.writerow(_day_row(date, days[date]))
        os.replace(str(tmp), str(path))  # aatomiline vahetus → pooleli fail ei jää
        return True
    except OSError as e:
        log(f"sink: päevavaate kirjutus ebaõnnestus: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


# --- SQLite server / client-server sink ------------------------------------
def _server_db_path(path: str | None = None) -> Path:
    return Path(path).expanduser() if path else SERVER_DB


def _db_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _db_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _db_add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _db_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _db_init(path: Path) -> None:
    with _db_connect(path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          token TEXT NOT NULL UNIQUE,
          role TEXT NOT NULL DEFAULT 'user',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS devices (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          client_id TEXT NOT NULL,
          name TEXT NOT NULL,
          platform TEXT NOT NULL,
          created_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          UNIQUE(user_id, client_id)
        );
        CREATE TABLE IF NOT EXISTS customers (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          external_key TEXT UNIQUE,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_key TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_remotes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          remote_url TEXT NOT NULL,
          normalized_url TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(project_id, normalized_url)
        );
        CREATE TABLE IF NOT EXISTS project_checkouts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          checkout_id TEXT NOT NULL,
          local_path TEXT NOT NULL,
          branch TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          UNIQUE(user_id, device_id, checkout_id)
        );
        CREATE TABLE IF NOT EXISTS issues (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          provider TEXT NOT NULL,
          issue_key TEXT NOT NULL,
          title TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(project_id, provider, issue_key)
        );
        CREATE TABLE IF NOT EXISTS work_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
          title TEXT NOT NULL,
          normalized_title TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          billable_default INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(project_id, issue_id)
        );
        CREATE INDEX IF NOT EXISTS work_items_project_title_idx ON work_items(project_id, normalized_title);
        CREATE TABLE IF NOT EXISTS work_sessions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          work_item_id INTEGER NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          project_checkout_id INTEGER REFERENCES project_checkouts(id) ON DELETE SET NULL,
          checkout_id TEXT NOT NULL,
          tool TEXT NOT NULL,
          local_path TEXT NOT NULL,
          cwd TEXT NOT NULL,
          branch TEXT NOT NULL DEFAULT '',
          started_at TEXT NOT NULL,
          ended_at TEXT,
          last_seen_at TEXT,
          status TEXT NOT NULL DEFAULT 'active',
          result TEXT NOT NULL DEFAULT '',
          billable INTEGER NOT NULL DEFAULT 1,
          summary TEXT NOT NULL DEFAULT '',
          minutes_final INTEGER,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS work_sessions_work_item_idx ON work_sessions(work_item_id, started_at);
        CREATE INDEX IF NOT EXISTS work_sessions_user_status_idx ON work_sessions(user_id, status, started_at);
        CREATE TABLE IF NOT EXISTS minute_ticks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          work_session_id INTEGER NOT NULL REFERENCES work_sessions(id) ON DELETE CASCADE,
          work_item_id INTEGER NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          minute_start_utc TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'heartbeat',
          created_at TEXT NOT NULL,
          UNIQUE(work_session_id, minute_start_utc)
        );
        CREATE INDEX IF NOT EXISTS minute_ticks_item_minute_idx ON minute_ticks(work_item_id, minute_start_utc);
        CREATE TABLE IF NOT EXISTS contracts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          currency TEXT NOT NULL DEFAULT 'EUR',
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contract_rates (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
          valid_from TEXT NOT NULL,
          valid_to TEXT,
          hourly_rate REAL NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hour_rows (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          date TEXT NOT NULL,
          hour TEXT NOT NULL,
          objekt TEXT NOT NULL,
          saavutus TEXT NOT NULL,
          takistus TEXT NOT NULL,
          teadmine TEXT NOT NULL,
          tool TEXT NOT NULL,
          row_key TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(user_id, row_key)
        );
        CREATE INDEX IF NOT EXISTS hour_rows_user_date_idx ON hour_rows(user_id, date, hour);
        CREATE TABLE IF NOT EXISTS prompt_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          event_key TEXT NOT NULL,
          tool TEXT NOT NULL,
          project TEXT NOT NULL,
          prompt_text TEXT NOT NULL,
          started_at TEXT NOT NULL,
          ended_at TEXT NOT NULL,
          duration_seconds INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(user_id, event_key)
        );
        CREATE INDEX IF NOT EXISTS prompt_events_user_started_idx ON prompt_events(user_id, started_at);
        """)
        # Vanade server.db failide kerge migratsioon: prompt_events jäi alles, aga saab nüüd
        # viidata normaliseeritud projekti/töö/sessiooni ridadele.
        _db_add_column_if_missing(conn, "prompt_events", "project_id", "INTEGER REFERENCES projects(id) ON DELETE SET NULL")
        _db_add_column_if_missing(conn, "prompt_events", "issue_id", "INTEGER REFERENCES issues(id) ON DELETE SET NULL")
        _db_add_column_if_missing(conn, "prompt_events", "work_item_id", "INTEGER REFERENCES work_items(id) ON DELETE SET NULL")
        _db_add_column_if_missing(conn, "prompt_events", "work_session_id", "INTEGER REFERENCES work_sessions(id) ON DELETE SET NULL")
        _db_add_column_if_missing(conn, "prompt_events", "confidence", "REAL")
        _db_add_column_if_missing(conn, "prompt_events", "payload_json", "TEXT")


def _db_user_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    if not token:
        raise PermissionError("token puudub")
    row = conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise PermissionError("vale token")
    return row


def _db_add_user(path: Path, name: str, role: str = "user") -> str:
    _db_init(path)
    token = secrets.token_urlsafe(32)
    now = _now_utc().isoformat()
    with _db_connect(path) as conn:
        conn.execute("INSERT INTO users(name, token, role, created_at) VALUES (?, ?, ?, ?)",
                     (name, token, role, now))
    return token


def _db_one_id(conn: sqlite3.Connection, sql: str, args: tuple) -> int:
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise RuntimeError("andmebaasi upsert ei tagastanud id-d")
    return int(row["id"])


def _db_upsert_device(conn: sqlite3.Connection, user_id: int, client_id: str, name: str, plat: str, now: str) -> int:
    client_id = client_id or "unknown"
    conn.execute(
        "INSERT OR IGNORE INTO devices(user_id, client_id, name, platform, created_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, client_id, name or "unknown", plat or "unknown", now, now),
    )
    conn.execute(
        "UPDATE devices SET name = ?, platform = ?, last_seen_at = ? WHERE user_id = ? AND client_id = ?",
        (name or "unknown", plat or "unknown", now, user_id, client_id),
    )
    return _db_one_id(conn, "SELECT id FROM devices WHERE user_id = ? AND client_id = ?", (user_id, client_id))


def _db_upsert_project(conn: sqlite3.Connection, project: dict, now: str) -> int:
    repo_url = str(project.get("repo_url") or "")
    project_key = str(project.get("project_key") or project.get("key") or _normalise_repo_url(repo_url))
    local_path = str(project.get("local_path") or "")
    if not project_key:
        project_key = f"local:{Path(local_path or '.').name.lower()}"
    name = str(project.get("name") or project_key.rstrip("/").split("/")[-1] or "project")
    conn.execute(
        "INSERT OR IGNORE INTO projects(project_key, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (project_key, name, now, now),
    )
    conn.execute("UPDATE projects SET name = ?, updated_at = ? WHERE project_key = ?", (name, now, project_key))
    project_id = _db_one_id(conn, "SELECT id FROM projects WHERE project_key = ?", (project_key,))
    norm = _normalise_repo_url(repo_url)
    if repo_url and norm:
        conn.execute(
            "INSERT OR IGNORE INTO project_remotes(project_id, remote_url, normalized_url, created_at) "
            "VALUES (?, ?, ?, ?)",
            (project_id, repo_url, norm, now),
        )
    return project_id


def _db_upsert_issue(conn: sqlite3.Connection, project_id: int, issue: dict, default_provider: str, now: str) -> int | None:
    issue_key = _normalise_issue_key(str(issue.get("issue_key") or issue.get("key") or issue.get("id") or ""))
    if not issue_key:
        return None
    provider = str(issue.get("provider") or default_provider or "local")
    title = str(issue.get("title") or "")
    url = str(issue.get("url") or "")
    conn.execute(
        "INSERT OR IGNORE INTO issues(project_id, provider, issue_key, title, url, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, provider, issue_key, title, url, now, now),
    )
    conn.execute(
        "UPDATE issues SET title = COALESCE(NULLIF(?, ''), title), url = COALESCE(NULLIF(?, ''), url), "
        "updated_at = ? WHERE project_id = ? AND provider = ? AND issue_key = ?",
        (title, url, now, project_id, provider, issue_key),
    )
    return _db_one_id(
        conn,
        "SELECT id FROM issues WHERE project_id = ? AND provider = ? AND issue_key = ?",
        (project_id, provider, issue_key),
    )


def _db_upsert_work_item(conn: sqlite3.Connection, project_id: int, issue_id: int | None,
                         title: str, billable_default: bool, now: str) -> int:
    title = (title or "").strip() or "Nimetu töö"
    norm = _normalise_title(title)
    if issue_id is not None:
        row = conn.execute(
            "SELECT id FROM work_items WHERE project_id = ? AND issue_id = ?",
            (project_id, issue_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO work_items(project_id, issue_id, title, normalized_title, status, billable_default, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                (project_id, issue_id, title, norm, 1 if billable_default else 0, now, now),
            )
        else:
            conn.execute("UPDATE work_items SET updated_at = ? WHERE id = ?", (now, row["id"]))
        return _db_one_id(conn, "SELECT id FROM work_items WHERE project_id = ? AND issue_id = ?", (project_id, issue_id))
    row = conn.execute(
        "SELECT id FROM work_items WHERE project_id = ? AND issue_id IS NULL AND normalized_title = ?",
        (project_id, norm),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO work_items(project_id, issue_id, title, normalized_title, status, billable_default, created_at, updated_at) "
            "VALUES (?, NULL, ?, ?, 'active', ?, ?, ?)",
            (project_id, title, norm, 1 if billable_default else 0, now, now),
        )
    else:
        conn.execute("UPDATE work_items SET updated_at = ? WHERE id = ?", (now, row["id"]))
    return _db_one_id(
        conn,
        "SELECT id FROM work_items WHERE project_id = ? AND issue_id IS NULL AND normalized_title = ?",
        (project_id, norm),
    )


def _db_upsert_checkout(conn: sqlite3.Connection, project_id: int, user_id: int, device_id: int,
                        session: dict, project: dict, now: str) -> int:
    checkout_id = str(session.get("checkout_id") or project.get("checkout_id") or "")
    local_path = str(project.get("local_path") or session.get("local_path") or session.get("cwd") or "")
    if not checkout_id:
        checkout_id = hashlib.sha1(f"{project_id}|{local_path}".encode("utf-8", errors="replace")).hexdigest()[:16]
    branch = str(session.get("branch") or project.get("branch") or "")
    conn.execute(
        "INSERT OR IGNORE INTO project_checkouts(project_id, user_id, device_id, checkout_id, local_path, branch, created_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, user_id, device_id, checkout_id, local_path, branch, now, now),
    )
    conn.execute(
        "UPDATE project_checkouts SET local_path = ?, branch = ?, last_seen_at = ? "
        "WHERE user_id = ? AND device_id = ? AND checkout_id = ?",
        (local_path, branch, now, user_id, device_id, checkout_id),
    )
    return _db_one_id(
        conn,
        "SELECT id FROM project_checkouts WHERE user_id = ? AND device_id = ? AND checkout_id = ?",
        (user_id, device_id, checkout_id),
    )


def _db_work_graph(conn: sqlite3.Connection, user: sqlite3.Row, payload: dict, now: str) -> dict:
    project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    work = payload.get("work") if isinstance(payload.get("work"), dict) else {}

    client_id = str(session.get("client_id") or payload.get("client_id") or "unknown")
    device_id = _db_upsert_device(
        conn, int(user["id"]), client_id,
        str(session.get("device_name") or session.get("name") or "unknown"),
        str(session.get("platform") or "unknown"), now,
    )
    project_id = _db_upsert_project(conn, project, now)
    default_provider = str(issue.get("provider") or _issue_provider_for_project_key(str(project.get("project_key") or project.get("key") or "")))
    if not issue.get("issue_key") and not issue.get("key") and session.get("branch"):
        issue = {**issue, "issue_key": _issue_from_branch(str(session.get("branch")))}
    issue_id = _db_upsert_issue(conn, project_id, issue, default_provider, now)
    title = str(work.get("title") or payload.get("title") or issue.get("title") or "").strip()
    billable_default = bool(work.get("billable", payload.get("billable", True)))
    work_item_id = _db_upsert_work_item(conn, project_id, issue_id, title, billable_default, now)
    checkout_id = _db_upsert_checkout(conn, project_id, int(user["id"]), device_id, session, project, now)
    return {"device_id": device_id, "project_id": project_id, "issue_id": issue_id,
            "work_item_id": work_item_id, "checkout_row_id": checkout_id}


def _db_work_start(path: Path, token: str, payload: dict) -> dict:
    _db_init(path)
    now = _now_utc().isoformat()
    with _db_connect(path) as conn:
        user = _db_user_by_token(conn, token)
        graph = _db_work_graph(conn, user, payload, now)
        project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
        session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
        work = payload.get("work") if isinstance(payload.get("work"), dict) else {}
        started_at = str(payload.get("started_at") or session.get("started_at") or now)
        tool = str(session.get("tool") or work.get("tool") or "manual")
        local_path = str(project.get("local_path") or session.get("local_path") or "")
        cwd = str(session.get("cwd") or project.get("cwd") or local_path)
        branch = str(session.get("branch") or project.get("branch") or "")
        checkout_id = str(session.get("checkout_id") or project.get("checkout_id") or "")
        if not checkout_id:
            checkout_id = hashlib.sha1(f"{graph['project_id']}|{local_path}".encode("utf-8", errors="replace")).hexdigest()[:16]
        summary = str(work.get("summary") or work.get("title") or payload.get("title") or "")
        billable = 1 if bool(work.get("billable", payload.get("billable", True))) else 0
        cur = conn.execute(
            "INSERT INTO work_sessions(work_item_id, user_id, device_id, project_checkout_id, checkout_id, tool, "
            "local_path, cwd, branch, started_at, last_seen_at, status, billable, summary, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
            (graph["work_item_id"], int(user["id"]), graph["device_id"], graph["checkout_row_id"], checkout_id,
             tool, local_path, cwd, branch, started_at, started_at, billable, summary, now, now),
        )
        session_id = int(cur.lastrowid)
        return {"ok": True, "work_session_id": session_id, "work_item_id": graph["work_item_id"],
                "project_id": graph["project_id"], "issue_id": graph["issue_id"]}


def _db_session_for_user(conn: sqlite3.Connection, user_id: int, session_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM work_sessions WHERE id = ? AND user_id = ?", (session_id, user_id)).fetchone()
    if row is None:
        raise PermissionError("work_session puudub või ei kuulu sellele kasutajale")
    return row


def _minute_floor_iso(value: str | None = None) -> str:
    d = parse_iso(value or "") or _now_utc()
    return d.astimezone(dt.timezone.utc).replace(second=0, microsecond=0).isoformat()


def _db_work_tick(path: Path, token: str, payload: dict) -> dict:
    _db_init(path)
    now = _now_utc().isoformat()
    with _db_connect(path) as conn:
        user = _db_user_by_token(conn, token)
        session_id = int(payload.get("work_session_id") or payload.get("session_id") or 0)
        row = _db_session_for_user(conn, int(user["id"]), session_id)
        if row["status"] != "active":
            return {"ok": True, "ignored": True, "reason": f"session status is {row['status']}"}
        minute = _minute_floor_iso(str(payload.get("minute_start_utc") or payload.get("tick_at") or ""))
        conn.execute(
            "INSERT OR IGNORE INTO minute_ticks(work_session_id, work_item_id, user_id, minute_start_utc, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, int(row["work_item_id"]), int(user["id"]), minute,
             str(payload.get("source") or "heartbeat"), now),
        )
        conn.execute("UPDATE work_sessions SET last_seen_at = ?, updated_at = ? WHERE id = ?", (minute, now, session_id))
        return {"ok": True, "work_session_id": session_id, "minute_start_utc": minute}


def _work_session_minutes(row: sqlite3.Row | dict, tick_count: int | None = None) -> int:
    final = row["minutes_final"] if isinstance(row, sqlite3.Row) else row.get("minutes_final")
    if final is not None:
        try:
            return max(0, int(final))
        except (TypeError, ValueError):
            pass
    if tick_count is not None and tick_count > 0:
        return int(tick_count)
    start = parse_iso(row["started_at"] if isinstance(row, sqlite3.Row) else row.get("started_at", ""))
    end = parse_iso((row["ended_at"] if isinstance(row, sqlite3.Row) else row.get("ended_at")) or
                    (row["last_seen_at"] if isinstance(row, sqlite3.Row) else row.get("last_seen_at")) or "")
    if not start or not end or end <= start:
        return 0
    return max(1, int((end - start).total_seconds() + 59) // 60)


def _db_work_finish(path: Path, token: str, payload: dict, *, status: str = "done") -> dict:
    _db_init(path)
    now = _now_utc().isoformat()
    with _db_connect(path) as conn:
        user = _db_user_by_token(conn, token)
        session_id = int(payload.get("work_session_id") or payload.get("session_id") or 0)
        row = _db_session_for_user(conn, int(user["id"]), session_id)
        ended_at = str(payload.get("ended_at") or now)
        manual_minutes = payload.get("minutes")
        if manual_minutes not in (None, ""):
            minutes = max(1, int(manual_minutes))
        else:
            tick_count = int(conn.execute("SELECT COUNT(*) AS c FROM minute_ticks WHERE work_session_id = ?", (session_id,)).fetchone()["c"])
            minutes = _work_session_minutes(row, tick_count)
            if minutes <= 0:
                start = parse_iso(row["started_at"]) or parse_iso(ended_at) or _now_utc()
                end = parse_iso(ended_at) or _now_utc()
                minutes = max(1, int((end - start).total_seconds() + 59) // 60)
        summary = str(payload.get("summary") or row["summary"] or "")
        result = str(payload.get("result") or ("discarded" if status == "discarded" else row["result"] or ""))
        billable = row["billable"]
        if "billable" in payload:
            billable = 1 if bool(payload.get("billable")) else 0
        conn.execute(
            "UPDATE work_sessions SET ended_at = ?, last_seen_at = ?, status = ?, result = ?, billable = ?, "
            "summary = ?, minutes_final = ?, updated_at = ? WHERE id = ?",
            (ended_at, ended_at, status, result, billable, summary, minutes, now, session_id),
        )
        conn.execute(
            "UPDATE work_items SET status = CASE WHEN ? = 'discarded' THEN status ELSE 'active' END, updated_at = ? "
            "WHERE id = ?",
            (status, now, int(row["work_item_id"])),
        )
        return {"ok": True, "work_session_id": session_id, "minutes": minutes, "status": status}


def _db_work_status(path: Path, token: str) -> list[dict]:
    _db_init(path)
    with _db_connect(path) as conn:
        user = _db_user_by_token(conn, token)
        rows = conn.execute(
            "SELECT ws.id, ws.tool, ws.started_at, ws.last_seen_at, ws.summary, ws.status, ws.branch, "
            "wi.id AS work_item_id, wi.title AS work_title, p.project_key, p.name AS project_name, "
            "i.provider, i.issue_key, u.name AS user_name "
            "FROM work_sessions ws "
            "JOIN work_items wi ON wi.id = ws.work_item_id "
            "JOIN projects p ON p.id = wi.project_id "
            "LEFT JOIN issues i ON i.id = wi.issue_id "
            "JOIN users u ON u.id = ws.user_id "
            "WHERE ws.user_id = ? AND ws.status = 'active' ORDER BY ws.started_at",
            (int(user["id"]),),
        ).fetchall()
    return [dict(r) for r in rows]


def _qval(q: dict, key: str, default: str = "") -> str:
    val = q.get(key, default)
    if isinstance(val, list):
        return str(val[0]) if val else default
    return str(val) if val is not None else default


def _period_bounds(q: dict) -> tuple[str, str, str]:
    period = _qval(q, "period")
    if re.fullmatch(r"\d{4}-\d{2}", period):
        y, m = map(int, period.split("-"))
        start = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
        end = dt.datetime(y + (m // 12), 1 if m == 12 else m + 1, 1, tzinfo=dt.timezone.utc)
        return start.isoformat(), end.isoformat(), period
    from_s = _qval(q, "from") or _qval(q, "start")
    to_s = _qval(q, "to") or _qval(q, "end")
    now = _now_utc()
    start = parse_iso(from_s) if from_s else dt.datetime(now.year, now.month, 1, tzinfo=dt.timezone.utc)
    if to_s:
        end = parse_iso(to_s)
        if end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", to_s):
            end = end + dt.timedelta(days=1)
    else:
        end = dt.datetime(now.year + (now.month // 12), 1 if now.month == 12 else now.month + 1, 1,
                          tzinfo=dt.timezone.utc)
    start = start or dt.datetime(now.year, now.month, 1, tzinfo=dt.timezone.utc)
    end = end or now
    label = f"{start.date()}..{end.date()}"
    return start.isoformat(), end.isoformat(), label


def _db_invoice_lines(path: Path, token: str, q: dict) -> dict:
    _db_init(path)
    start_iso, end_iso, label = _period_bounds(q)
    rate_s = _qval(q, "hourly_rate") or _qval(q, "rate")
    hourly_rate = float(rate_s) if rate_s else None
    project_filter = _qval(q, "project_key")
    customer_filter = _qval(q, "customer_id")
    issue_filter = _normalise_issue_key(_qval(q, "issue"))
    with _db_connect(path) as conn:
        user = _db_user_by_token(conn, token)
        where = ["ws.billable = 1", "ws.status != 'discarded'",
                 "ws.started_at < ?", "COALESCE(ws.ended_at, ws.last_seen_at, ws.started_at) >= ?"]
        args: list = [end_iso, start_iso]
        if user["role"] != "admin":
            where.append("ws.user_id = ?")
            args.append(int(user["id"]))
        if project_filter:
            where.append("p.project_key = ?")
            args.append(project_filter)
        if customer_filter:
            where.append("p.customer_id = ?")
            args.append(int(customer_filter))
        if issue_filter:
            where.append("i.issue_key = ?")
            args.append(issue_filter)
        sql = f"""
            SELECT ws.*, u.name AS user_name, d.client_id, d.name AS device_name,
                   wi.id AS work_item_id, wi.title AS work_title, p.project_key, p.name AS project_name,
                   c.name AS customer_name, i.provider, i.issue_key, i.title AS issue_title,
                   (SELECT COUNT(*) FROM minute_ticks mt WHERE mt.work_session_id = ws.id) AS tick_count
            FROM work_sessions ws
            JOIN users u ON u.id = ws.user_id
            JOIN devices d ON d.id = ws.device_id
            JOIN work_items wi ON wi.id = ws.work_item_id
            JOIN projects p ON p.id = wi.project_id
            LEFT JOIN customers c ON c.id = p.customer_id
            LEFT JOIN issues i ON i.id = wi.issue_id
            WHERE {' AND '.join(where)}
            ORDER BY p.project_key, i.issue_key, wi.title, ws.started_at
        """
        rows = conn.execute(sql, tuple(args)).fetchall()
    groups: dict[int, dict] = {}
    for r in rows:
        minutes = _work_session_minutes(r, int(r["tick_count"] or 0))
        if minutes <= 0:
            continue
        gid = int(r["work_item_id"])
        line = groups.setdefault(gid, {
            "work_item_id": gid,
            "customer": r["customer_name"] or "",
            "project": r["project_name"],
            "project_key": r["project_key"],
            "issue": (f"#{r['issue_key']}" if r["issue_key"] else ""),
            "issue_provider": r["provider"] or "",
            "title": r["issue_title"] or r["work_title"],
            "problem_text": r["issue_title"] or r["work_title"],
            "work_done": [],
            "minutes": 0,
            "time": "00:00",
            "hourly_rate": hourly_rate,
            "amount": None,
            "sessions": [],
            "evidence": {"work_item_id": gid, "session_ids": []},
        })
        if r["summary"] and r["summary"] not in line["work_done"]:
            line["work_done"].append(r["summary"])
        line["minutes"] += minutes
        line["sessions"].append({
            "session_id": int(r["id"]), "user": r["user_name"], "tool": r["tool"],
            "device": r["device_name"], "branch": r["branch"], "started_at": r["started_at"],
            "ended_at": r["ended_at"], "minutes": minutes, "result": r["result"],
        })
        line["evidence"]["session_ids"].append(int(r["id"]))
    lines = []
    for line in groups.values():
        mins = int(line["minutes"])
        line["time"] = f"{mins // 60:02d}:{mins % 60:02d}"
        if hourly_rate is not None:
            line["amount"] = round((mins / 60.0) * hourly_rate, 2)
        lines.append(line)
    return {"ok": True, "period": label, "from": start_iso, "to": end_iso,
            "currency": "EUR", "hourly_rate": hourly_rate, "lines": lines}


def _db_practice_summary(path: Path, token: str, q: dict) -> dict:
    _db_init(path)
    start_iso, end_iso, label = _period_bounds(q)
    with _db_connect(path) as conn:
        user = _db_user_by_token(conn, token)
        target_user = int(_qval(q, "user_id") or user["id"]) if user["role"] == "admin" else int(user["id"])
        rows = conn.execute(
            "SELECT ws.*, p.project_key, p.name AS project_name, wi.title AS work_title, "
            "i.issue_key, i.title AS issue_title, "
            "(SELECT COUNT(*) FROM minute_ticks mt WHERE mt.work_session_id = ws.id) AS tick_count "
            "FROM work_sessions ws "
            "JOIN work_items wi ON wi.id = ws.work_item_id "
            "JOIN projects p ON p.id = wi.project_id "
            "LEFT JOIN issues i ON i.id = wi.issue_id "
            "WHERE ws.user_id = ? AND ws.status != 'discarded' AND ws.started_at < ? "
            "AND COALESCE(ws.ended_at, ws.last_seen_at, ws.started_at) >= ? "
            "ORDER BY ws.started_at",
            (target_user, end_iso, start_iso),
        ).fetchall()
    days: dict[str, dict] = {}
    for r in rows:
        started = parse_iso(r["started_at"]) or _now_utc()
        day_key = started.date().isoformat()
        minutes = _work_session_minutes(r, int(r["tick_count"] or 0))
        issue = f"#{r['issue_key']}" if r["issue_key"] else ""
        title = r["issue_title"] or r["work_title"]
        day = days.setdefault(day_key, {"date": day_key, "minutes": 0, "items": [], "text": ""})
        day["minutes"] += minutes
        day["items"].append({
            "project": r["project_name"], "project_key": r["project_key"], "issue": issue,
            "title": title, "tool": r["tool"], "summary": r["summary"], "minutes": minutes,
        })
    for day in days.values():
        parts = []
        for item in day["items"]:
            label_i = " ".join(x for x in [item["project"], item["issue"], item["title"]] if x)
            summary = f": {item['summary']}" if item["summary"] else ""
            parts.append(f"{label_i} ({item['tool']}, {item['minutes']} min){summary}")
        day["text"] = "; ".join(parts)
    return {"ok": True, "period": label, "from": start_iso, "to": end_iso,
            "days": [days[k] for k in sorted(days)]}


def _db_rows_for_day(path: Path, token: str, date: str) -> list[list]:
    _db_init(path)
    with _db_connect(path) as conn:
        user = _db_user_by_token(conn, token)
        rows = conn.execute(
            "SELECT date, hour, objekt, saavutus, takistus, teadmine, tool, row_key "
            "FROM hour_rows WHERE user_id = ? AND date = ? ORDER BY hour, row_key",
            (user["id"], date),
        ).fetchall()
    return [[r["date"], r["hour"], r["objekt"], r["saavutus"], r["takistus"],
             r["teadmine"], r["tool"], r["row_key"]] for r in rows]


def _db_days(path: Path, token: str) -> list[str]:
    _db_init(path)
    with _db_connect(path) as conn:
        user = _db_user_by_token(conn, token)
        rows = conn.execute("SELECT DISTINCT date FROM hour_rows WHERE user_id = ? ORDER BY date",
                            (user["id"],)).fetchall()
    return [r["date"] for r in rows]


def _db_keys(path: Path, token: str) -> set[str]:
    _db_init(path)
    with _db_connect(path) as conn:
        user = _db_user_by_token(conn, token)
        rows = conn.execute("SELECT row_key FROM hour_rows WHERE user_id = ?", (user["id"],)).fetchall()
    return {r["row_key"] for r in rows}


def _db_ingest_rows(path: Path, token: str, rows: list[list], keys: list[str]) -> None:
    _db_init(path)
    now = _now_utc().isoformat()
    with _db_connect(path) as conn:
        user = _db_user_by_token(conn, token)
        for row, key in zip(rows, keys):
            if len(row) < 7 or not str(key).startswith("k:"):
                continue
            # Ingest ei kirjuta olemasolevat üle: UI-s tehtud parandused säilivad.
            conn.execute(
                "INSERT OR IGNORE INTO hour_rows "
                "(user_id, date, hour, objekt, saavutus, takistus, teadmine, tool, row_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user["id"], row[0], row[1], row[2], row[3], row[4], row[5], row[6], key, now, now),
            )


def _db_replace_day_rows(path: Path, token: str, date: str, items: list[dict]) -> None:
    if not _valid_date(date):
        raise ValueError("vigane kuupäev")
    _db_init(path)
    now = _now_utc().isoformat()
    with _db_connect(path) as conn:
        user = _db_user_by_token(conn, token)
        conn.execute("DELETE FROM hour_rows WHERE user_id = ? AND date = ?", (user["id"], date))
        used: set[str] = set()
        for item in items:
            hour = str(item.get("hour", "")).strip() or "00:00–01:00"
            objekt = str(item.get("objekt", "")).strip()
            saavutus = str(item.get("saavutus", "")).strip()
            takistus = str(item.get("takistus", "")).strip()
            teadmine = str(item.get("teadmine", "")).strip()
            tool = str(item.get("tool", "")).strip() or "Käsitsi"
            if not any([objekt, saavutus, takistus, teadmine]):
                continue
            key = str(item.get("key", "")).strip()
            if not key.startswith("k:") or key in used:
                key = f"k:{date}|ui|{secrets.token_hex(8)}"
            used.add(key)
            conn.execute(
                "INSERT INTO hour_rows "
                "(user_id, date, hour, objekt, saavutus, takistus, teadmine, tool, row_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user["id"], date, hour, _cell_safe(objekt), _cell_safe(saavutus or _NA),
                 _cell_safe(takistus or _NA), _cell_safe(teadmine or _NA), _cell_safe(tool), key, now, now),
            )


def _db_ingest_events(path: Path, token: str, events: list[dict]) -> None:
    _db_init(path)
    now = _now_utc().isoformat()
    with _db_connect(path) as conn:
        user = _db_user_by_token(conn, token)
        for e in events:
            key = str(e.get("event_key", ""))
            if not key:
                continue
            project_raw = str(e.get("project", ""))
            ctx = _project_context(project_raw or ".")
            project_id = _db_upsert_project(conn, {
                "project_key": ctx["project_key"], "repo_url": ctx["repo_url"],
                "name": ctx["name"], "local_path": ctx["local_path"],
            }, now)
            issue_id = None
            if ctx.get("issue_key"):
                issue_id = _db_upsert_issue(conn, project_id, {
                    "provider": ctx.get("issue_provider") or "local", "issue_key": ctx.get("issue_key")
                }, ctx.get("issue_provider") or "local", now)
            conn.execute(
                "INSERT OR IGNORE INTO prompt_events "
                "(user_id, event_key, tool, project, prompt_text, started_at, ended_at, duration_seconds, "
                "created_at, project_id, issue_id, confidence, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user["id"], key, str(e.get("tool", "")), project_raw,
                 str(e.get("prompt_text", "")), str(e.get("started_at", "")),
                 str(e.get("ended_at", "")), int(e.get("duration_seconds") or 0), now,
                 project_id, issue_id, float(e.get("confidence") or 0.5),
                 json.dumps(e, ensure_ascii=False, sort_keys=True)),
            )


def _server_url(cfg: dict, op: str) -> str:
    base = cfg.get("sink", {}).get("server_url", "").strip().rstrip("/")
    if not base:
        raise ValueError("server_url puudub configis")
    return f"{base}/api/{op}"


def _server_get(op: str, params: dict, cfg: dict) -> dict | None:
    sink = cfg.get("sink", {})
    params = {"token": sink.get("token", ""), **params}
    qs = urllib.parse.urlencode(params)
    url = _server_url(cfg, op) + (f"?{qs}" if qs else "")
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        log(f"server sink: GET /api/{op} ebaõnnestus: {e}")
        return None


def _server_post(op: str, payload: dict, cfg: dict) -> dict | None:
    sink = cfg.get("sink", {})
    payload = {"token": sink.get("token", ""), **payload}
    data = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(_server_url(cfg, op), data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        log(f"server sink: POST /api/{op} ebaõnnestus: {e}")
        return None


def _server_append_rows(rows: list[list], keys: list[str], cfg: dict) -> bool:
    body = _server_post("ingest", {"rows": rows, "keys": keys}, cfg)
    return bool(body and body.get("ok"))


def _server_fetch_keys(cfg: dict) -> set[str] | None:
    body = _server_post("keys", {}, cfg)
    if body and body.get("ok") and isinstance(body.get("keys"), list):
        return {str(x) for x in body["keys"]}
    return None


def _prompt_events_payload(records: list[Record], allow: list[str], start: dt.datetime,
                           end: dt.datetime) -> list[dict]:
    scoped: list[tuple[Record, str]] = []
    for r in sorted(records, key=lambda x: x.ts):
        if not (start <= r.ts < end):
            continue
        proj = match_project(r.project, allow)
        if proj is None:
            continue
        scoped.append((r, proj))
    events: list[dict] = []
    for i, (r, proj) in enumerate(scoped):
        next_ts = scoped[i + 1][0].ts if i + 1 < len(scoped) else r.ts + dt.timedelta(minutes=1)
        if next_ts <= r.ts or next_ts - r.ts > dt.timedelta(minutes=30):
            next_ts = r.ts + dt.timedelta(minutes=1)
        duration = max(60, int((next_ts - r.ts).total_seconds()))
        raw = f"{r.tool}|{proj}|{r.ts.isoformat()}|{r.text}"
        ekey = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()
        events.append({
            "event_key": ekey,
            "tool": r.tool,
            "project": proj,
            "prompt_text": r.text,
            "started_at": r.ts.isoformat(),
            "ended_at": next_ts.isoformat(),
            "duration_seconds": duration,
        })
    return events


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
    sink_type = cfg.get("sink", {}).get("type")
    if sink_type == "local":
        # 1) lisa tunnid sisemisse algandmestikku (dedup), 2) renderda päevavaade ümber
        if not _raw_append(rows, keys):
            return False
        return _render_day_view(cfg)
    if sink_type == "server":
        return _server_append_rows(rows, keys, cfg)
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
    sink_type = cfg.get("sink", {}).get("type")
    if sink_type == "local":
        return _local_keys(HOURS_CSV)  # dedup-võtmed sisemisest algandmestikust
    if sink_type == "server":
        return _server_fetch_keys(cfg)
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

    if cfg.get("sink", {}).get("type") == "local":
        _migrate_old_log(cfg)  # ühekordne: vana 5-veeru log → uus algandmestik (ei kao andmed)

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
        item = summarize(prompts, proj_label, hour_label, cfg)  # 4-väljaline dict
        notes = notes_by_hour.get(_hour_iso(hstart), [])  # käsitsi-märkmed selle tunni kohta
        if notes:  # käsitsi-märge → "Uued teadmised" veergu (sinna kuuluvad õpitud asjad)
            note_txt = "Märge: " + " · ".join(notes)
            item["teadmine"] = (note_txt if item["teadmine"] == _NA
                                else f"{item['teadmine']} · {note_txt}")
        # _cell_safe kasutajast tuletatud lahtritel → ei käivitu valemina (CSV/Sheets injection)
        rows.append([date_str, hour_label,
                     _cell_safe(item["objekt"]), _cell_safe(item["saavutus"]),
                     _cell_safe(item["takistus"]), _cell_safe(item["teadmine"]),
                     ", ".join(tools)])
        keys.append(key)
        log(f"  → {date_str} {hour_label} | {', '.join(tools)} | {item['objekt'][:70]}")

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
        note_txt = " · ".join(texts)
        rows.append([date_str, hour_label, _cell_safe("(märge)"), _cell_safe(_NA),
                     _cell_safe(_NA), _cell_safe("Märge: " + note_txt), ""])
        keys.append(key)
        log(f"  → {date_str} {hour_label} | (märge) | {note_txt[:70]}")

    if skipped:
        log(f"backfill: {skipped} juba-olemas tundi jäeti vahele (LLM-kõnet ei tehtud)")

    if rows:
        if append_rows(rows, keys, cfg):
            stype = cfg.get("sink", {}).get("type")
            dest = str(_local_path(cfg)) if stype == "local" else ("aitrack server" if stype == "server" else "Google Sheetsi")
            log(f"run: {len(rows)} rida saadetud → {dest}")
            if stype == "server":
                events = _prompt_events_payload(records, allow, last_hour, process_until)
                if events:
                    body = _server_post("events", {"events": events}, cfg)
                    if body and body.get("ok"):
                        log(f"run: {len(events)} prompt-eventi saadetud → aitrack server")
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


def cmd_day(args, cfg):
    """Prindi päevarida (üks rida päevas) tab-eraldusega — valmis Google Sheetsi kleepimiseks."""
    if cfg.get("sink", {}).get("type") == "local":
        _migrate_old_log(cfg)  # taga, et vana log oleks nähtav ka enne järgmist run'i
    days = _read_raw_days()
    if not days:
        print("Andmeid pole veel. Käivita: aitrack run (või oota tunniajastit).")
        return
    if args.all:
        targets = sorted(days)
    elif args.date:
        if args.date not in days:
            print(f"Kuupäeval {args.date} andmeid pole. Olemas: {', '.join(sorted(days))}")
            return
        targets = [args.date]
    else:
        targets = [sorted(days)[-1]]  # vaikimisi viimane päev, mille kohta on andmeid
    # Vaikimisi ainult sisuveerud D–G (Objekt/Saavutused/Takistused/Uued teadmised) — kasutaja
    # täidab A/B/C (Kuupäev/Punkte/Nädalapäev) ise; --full annab kõik 7 veergu.
    cols = slice(None) if args.full else slice(3, 7)
    if getattr(args, "html", False):
        # Google Sheets oskab HTML-tabelit clipboardist kindlalt lahtritesse jagada;
        # <br> hoiab nummerdatud punktid sama lahtri sees rea alguses.
        import html as _html
        def cell(c):
            txt = _html.escape(str(c).replace("\r", "").strip()).replace("\n", "<br>")
            return f"<td>{txt}</td>"
        trs = []
        if args.header or args.all:
            trs.append("<tr>" + "".join(cell(c) for c in DAY_HEADER[cols]) + "</tr>")
        for d in targets:
            trs.append("<tr>" + "".join(cell(c) for c in _day_row(d, days[d])[cols]) + "</tr>")
        print("<table>" + "".join(trs) + "</table>")
        return

    if getattr(args, "flat", False):
        # Clipboardi plain-text paste ei käitu kõigis Sheets/browser/OS kombinatsioonides
        # CSV-jutumärkides mitmerealiste lahtritega ühtemoodi. --flat teeb ühe füüsilise
        # TSV-rea: kindel D–G/A–G veergudesse kleepimine, ilma sisemiste reavahetusteta.
        def flat_row(row):
            return "\t".join(str(c).replace("\t", " ").replace("\r", " ").replace("\n", " · ").strip()
                             for c in row)
        if args.header or args.all:
            print(flat_row(DAY_HEADER[cols]))
        for d in targets:
            print(flat_row(_day_row(d, days[d])[cols]))
        return

    # csv.writer tabiga → mitmerealised lahtrid lähevad jutumärkidesse (sobib failiks,
    # kuid plain-text clipboardis võib Sheetsis sõltuda OS/browserist; kopeerimiseks eelista --flat).
    w = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    if args.header or args.all:
        w.writerow(DAY_HEADER[cols])
    for d in targets:
        w.writerow(_day_row(d, days[d])[cols])


def _valid_date(s: str) -> bool:
    try:
        dt.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _today_local_str(cfg: dict) -> str:
    return _now_utc().astimezone(get_tz(cfg)).strftime("%Y-%m-%d")


def _ui_day_rows(date: str) -> list[dict]:
    rows = _read_raw_days().get(date, [])
    return [
        {
            "key": r[7],
            "hour": r[1],
            "objekt": r[2],
            "saavutus": r[3],
            "takistus": r[4],
            "teadmine": r[5],
            "tool": r[6],
        }
        for r in rows if len(r) >= len(RAW_HEADER)
    ]


def _write_all_raw_rows(rows: list[list]) -> None:
    _mkconfdir()
    tmp = HOURS_CSV.with_name(f"{HOURS_CSV.name}.{os.getpid()}.tmp")
    _create_private(tmp)
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(RAW_HEADER)
        w.writerows(rows)
    os.replace(tmp, HOURS_CSV)


def _replace_day_rows(date: str, items: list[dict], cfg: dict) -> bool:
    """Asenda ühe päeva tunniread UI-st tulnud väärtustega ja renderda päevavaade."""
    if not _valid_date(date):
        raise ValueError("vigane kuupäev")
    kept: list[list] = []
    keys: set[str] = set()
    if HOURS_CSV.exists():
        with HOURS_CSV.open(newline="", encoding="utf-8") as f:
            rdr = csv.reader(f)
            next(rdr, None)
            for row in rdr:
                if len(row) < len(RAW_HEADER):
                    continue
                if row[0] == date:
                    continue
                kept.append(row)
                if row[-1].startswith("k:"):
                    keys.add(row[-1])

    new_rows: list[list] = []
    for item in items:
        hour = str(item.get("hour", "")).strip()
        objekt = str(item.get("objekt", "")).strip()
        saavutus = str(item.get("saavutus", "")).strip()
        takistus = str(item.get("takistus", "")).strip()
        teadmine = str(item.get("teadmine", "")).strip()
        tool = str(item.get("tool", "")).strip() or "Käsitsi"
        # Täiesti tühi rida visatakse ära; ainult tunni väli ei ole sisu.
        if not any([objekt, saavutus, takistus, teadmine]):
            continue
        if not hour:
            hour = "00:00–01:00"
        key = str(item.get("key", "")).strip()
        if not key.startswith("k:") or key in keys:
            key = f"k:{date}|ui|{secrets.token_hex(8)}"
        keys.add(key)
        new_rows.append([
            date, hour, _cell_safe(objekt), _cell_safe(saavutus or _NA),
            _cell_safe(takistus or _NA), _cell_safe(teadmine or _NA),
            _cell_safe(tool), key,
        ])

    all_rows = kept + new_rows
    all_rows.sort(key=lambda r: (r[0], r[1], r[-1]))
    _write_all_raw_rows(all_rows)
    return _render_day_view(cfg)


def _html_table_for_day(date: str, full: bool = False) -> tuple[str, str]:
    """Tagasta (html, tekst-fallback) Google Sheetsi kleepimiseks."""
    rows = _read_raw_days().get(date, [])
    day = _day_row(date, rows) if rows else [date, 0, _weekday_letter(date), "", "", "", ""]
    cols = slice(None) if full else slice(3, 7)
    selected = day[cols]

    import html as _html
    def esc(c) -> str:
        return _html.escape(str(c).replace("\r", "").strip()).replace("\n", "<br>")
    html = "<table><tbody><tr>" + "".join(f"<td>{esc(c)}</td>" for c in selected) + "</tr></tbody></table>"
    text = "\t".join(str(c).replace("\t", " ").replace("\r", " ").replace("\n", "\n") for c in selected)
    return html, text


def _start_page_html() -> str:
    return r"""<!doctype html>
<html lang="et">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aitrack</title>
<style>
:root { color-scheme: light dark; --bg:#0f172a; --panel:#111827; --muted:#94a3b8; --text:#e5e7eb; --accent:#38bdf8; --ok:#22c55e; --bad:#f97316; --line:#334155; }
@media (prefers-color-scheme: light) { :root { --bg:#f8fafc; --panel:#ffffff; --muted:#64748b; --text:#0f172a; --accent:#0369a1; --ok:#15803d; --bad:#c2410c; --line:#cbd5e1; } }
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--text); }
header { padding:18px 22px; border-bottom:1px solid var(--line); display:flex; gap:16px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
h1 { margin:0; font-size:22px; }
main { padding:18px 22px 40px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:14px; margin-bottom:16px; box-shadow:0 8px 30px rgba(0,0,0,.12); }
.toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
button, input, select, textarea { font:inherit; }
button { border:1px solid var(--line); background:transparent; color:var(--text); border-radius:10px; padding:8px 11px; cursor:pointer; }
button.primary { background:var(--accent); color:white; border-color:var(--accent); }
button.good { background:var(--ok); color:white; border-color:var(--ok); }
button.warn { border-color:var(--bad); color:var(--bad); }
button:hover { filter:brightness(1.08); }
input, select, textarea { background:transparent; color:var(--text); border:1px solid var(--line); border-radius:10px; padding:8px; }
textarea { width:100%; min-height:92px; resize:none; line-height:1.35; overflow:hidden; }
.small { color:var(--muted); font-size:13px; }
.status { color:var(--muted); min-height:20px; }
table { width:100%; border-collapse:collapse; }
th, td { border-top:1px solid var(--line); padding:8px; vertical-align:top; }
th { color:var(--muted); text-align:left; font-weight:600; font-size:13px; }
.hour { width:120px; }
.tool { width:110px; }
.actions { width:74px; text-align:right; }
.empty { text-align:center; color:var(--muted); padding:30px; }
@media (max-width: 900px) { table, thead, tbody, tr, td, th { display:block; } thead { display:none; } tr { border:1px solid var(--line); border-radius:12px; margin:10px 0; padding:8px; } td { border:0; padding:6px; } td::before { content:attr(data-label); display:block; color:var(--muted); font-size:12px; margin-bottom:3px; } .hour, .tool { width:100%; } }
</style>
</head>
<body>
<header>
  <div><h1>aitrack</h1><div class="small">Tänased ja varasemad tööpäeviku read — muuda, lisa ja kopeeri Google Sheetsi.</div></div>
  <div class="toolbar"><button onclick="showHelp()">Abi</button><button onclick="refreshFromLogs()">Töötle lõpetatud tunnid</button><button onclick="backfill()">Backfill 12h</button></div>
</header>
<main>
  <section class="panel toolbar">
    <label>Kuupäev <input type="date" id="dateInput"></label>
    <select id="daySelect" title="Olemasolevad päevad"></select>
    <label title="Vajalik ainult aitrack serve keskserveri puhul">Server token <input id="tokenInput" type="password" placeholder="keskserveri token"></label>
    <button onclick="loadDay()">Ava</button>
    <button onclick="addRow()">+ Lisa rida</button>
    <button class="primary" onclick="saveDay(true)">Salvesta</button>
    <button class="good" onclick="copyDay(false)">Kopeeri D–G</button>
    <button class="good" onclick="copyDay(true)">Kopeeri A–G</button>
    <span class="status" id="status"></span>
  </section>
  <section class="panel">
    <table id="rowsTable">
      <thead><tr><th>Tund</th><th>Objekt ja ülesanne</th><th>Saavutused</th><th>Takistused</th><th>Uued teadmised</th><th>Tööriist</th><th></th></tr></thead>
      <tbody id="rowsBody"><tr><td class="empty" colspan="7">Laen…</td></tr></tbody>
    </table>
  </section>
  <section class="panel small" id="help" hidden>
    <b>Kuidas kasutada?</b><br>
    1. Vali kuupäev. 2. Muuda/lisa read. 3. Vajuta Salvesta. 4. Vajuta “Kopeeri D–G” ja kleebi Sheetsis D-lahtrisse.<br>
    “Kopeeri A–G” kasuta siis, kui tahad ka kuupäeva/punktide/nädalapäeva veerud kaasa võtta ja kleebid A-lahtrisse.
  </section>
</main>
<script>
let currentDate = '';
let days = [];
const $ = (id) => document.getElementById(id);
function setStatus(msg, isError=false) { $('status').textContent = msg; $('status').style.color = isError ? 'var(--bad)' : 'var(--muted)'; }
function authToken() { return $('tokenInput') ? $('tokenInput').value.trim() : ''; }
async function api(path, opts={}) {
  opts.headers = Object.assign({}, opts.headers || {});
  const tok = authToken();
  if (tok) opts.headers['X-Aitrack-Token'] = tok;
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
  return data;
}
function escapeHtml(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function rowTemplate(row={}) {
  const key = escapeHtml(row.key || '');
  return `<tr data-key="${key}">
    <td data-label="Tund"><input class="hour" value="${escapeHtml(row.hour || '')}" placeholder="14:00–15:00"></td>
    <td data-label="Objekt"><textarea class="objekt">${escapeHtml(row.objekt || '')}</textarea></td>
    <td data-label="Saavutused"><textarea class="saavutus">${escapeHtml(row.saavutus || '')}</textarea></td>
    <td data-label="Takistused"><textarea class="takistus">${escapeHtml(row.takistus || '')}</textarea></td>
    <td data-label="Uued teadmised"><textarea class="teadmine">${escapeHtml(row.teadmine || '')}</textarea></td>
    <td data-label="Tööriist"><input class="tool" value="${escapeHtml(row.tool || 'Käsitsi')}"></td>
    <td class="actions"><button class="warn" onclick="deleteRow(this)">Kustuta</button></td>
  </tr>`;
}
function autoResizeTextarea(el) {
  el.style.height = 'auto';
  el.style.height = Math.max(92, el.scrollHeight + 2) + 'px';
}
function autoResizeAll() {
  document.querySelectorAll('textarea').forEach(autoResizeTextarea);
}
function wireTextareas(scope=document) {
  scope.querySelectorAll('textarea').forEach(el => {
    autoResizeTextarea(el);
    el.addEventListener('input', () => autoResizeTextarea(el));
  });
}
function deleteRow(button) {
  if (!confirm('Kas kustutan selle rea? Salvestamiseks vajuta pärast ka “Salvesta”.')) return;
  button.closest('tr').remove();
}
function collectRows() {
  return Array.from(document.querySelectorAll('#rowsBody tr[data-key]')).map(tr => ({
    key: tr.dataset.key || '',
    hour: tr.querySelector('.hour').value,
    objekt: tr.querySelector('.objekt').value,
    saavutus: tr.querySelector('.saavutus').value,
    takistus: tr.querySelector('.takistus').value,
    teadmine: tr.querySelector('.teadmine').value,
    tool: tr.querySelector('.tool').value
  }));
}
function renderRows(rows) {
  $('rowsBody').innerHTML = rows.length ? rows.map(rowTemplate).join('') : '<tr><td class="empty" colspan="7">Sellel päeval pole veel ridu. Vajuta “+ Lisa rida”.</td></tr>';
  wireTextareas($('rowsBody'));
}
async function init() {
  currentDate = new Date().toISOString().slice(0, 10);
  $('dateInput').value = currentDate;
  if ($('tokenInput')) {
    $('tokenInput').value = localStorage.getItem('aitrackToken') || '';
    $('tokenInput').addEventListener('input', () => localStorage.setItem('aitrackToken', authToken()));
  }
  const data = await api('/api/days');
  days = data.days;
  currentDate = data.today;
  $('dateInput').value = currentDate;
  renderDaySelect();
  await loadDay();
}
function renderDaySelect() {
  $('daySelect').innerHTML = days.map(d => `<option value="${d}">${d}</option>`).join('');
  if (!days.includes(currentDate)) $('daySelect').insertAdjacentHTML('afterbegin', `<option value="${currentDate}">${currentDate}</option>`);
  $('daySelect').value = currentDate;
  $('daySelect').onchange = () => { $('dateInput').value = $('daySelect').value; loadDay(); };
  $('dateInput').onchange = () => { currentDate = $('dateInput').value; $('daySelect').value = currentDate; loadDay(); };
}
async function loadDay() {
  currentDate = $('dateInput').value || currentDate;
  setStatus('Laen…');
  const data = await api('/api/day?date=' + encodeURIComponent(currentDate));
  renderRows(data.rows || []);
  setStatus(`Avatud ${currentDate}`);
}
function addRow() {
  const body = $('rowsBody');
  if (!body.querySelector('tr[data-key]')) body.innerHTML = '';
  body.insertAdjacentHTML('beforeend', rowTemplate({hour:'', tool:'Käsitsi'}));
  wireTextareas(body.lastElementChild);
}
async function saveDay(show=true) {
  currentDate = $('dateInput').value || currentDate;
  const rows = collectRows();
  setStatus('Salvestan…');
  const data = await api('/api/day', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({date: currentDate, rows})});
  renderRows(data.rows || []);
  if (!days.includes(currentDate)) { days.push(currentDate); days.sort(); renderDaySelect(); }
  if (show) setStatus('Salvestatud');
}
async function copyRich(html, text) {
  if (navigator.clipboard && window.ClipboardItem) {
    await navigator.clipboard.write([new ClipboardItem({
      'text/html': new Blob([html], {type:'text/html'}),
      'text/plain': new Blob([text], {type:'text/plain'})
    })]);
    return;
  }
  const div = document.createElement('div');
  div.contentEditable = 'true'; div.style.position = 'fixed'; div.style.left = '-9999px'; div.innerHTML = html;
  document.body.appendChild(div);
  const range = document.createRange(); range.selectNodeContents(div);
  const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
  document.execCommand('copy'); sel.removeAllRanges(); div.remove();
}
async function copyDay(full) {
  await saveDay(false);
  const data = await api('/api/copy?date=' + encodeURIComponent(currentDate) + '&full=' + (full ? '1' : '0'));
  await copyRich(data.html, data.text);
  setStatus(full ? 'Kopeeritud A–G. Kleebi Sheetsis A-lahtrisse.' : 'Kopeeritud D–G. Kleebi Sheetsis D-lahtrisse.');
}
async function refreshFromLogs() {
  if (!confirm('Käivitada aitrack run? See võib võtta aega.')) return;
  setStatus('Töötlen logisid…');
  await api('/api/run', {method:'POST'});
  await init();
  setStatus('Logid töödeldud');
}
async function backfill() {
  if (!confirm('Töödelda viimased 12 tundi tagantjärele?')) return;
  setStatus('Backfill 12h…');
  await api('/api/backfill', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({hours:12})});
  await init();
  setStatus('Backfill tehtud');
}
function showHelp() { $('help').hidden = !$('help').hidden; }
init().catch(e => setStatus(e.message, true));
</script>
</body>
</html>"""


class _AitrackHandler(BaseHTTPRequestHandler):
    cfg: dict = {}

    def log_message(self, fmt, *args):  # vaiksem server; olulised vead lähevad vastusesse
        return

    def _json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _token(self, q: dict | None = None, data: dict | None = None) -> str:
        if data and data.get("token"):
            return str(data.get("token"))
        if q and q.get("token"):
            return str(q.get("token", [""])[0])
        return self.headers.get("X-Aitrack-Token", "")

    def _server_mode(self) -> bool:
        return bool(self.cfg.get("_server_mode"))

    def _db_path(self) -> Path:
        return _server_db_path(self.cfg.get("_db_path"))

    def do_HEAD(self) -> None:  # noqa: N802 (http.server API)
        if urllib.parse.urlparse(self.path).path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        try:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                self._html(_start_page_html())
                return
            if u.path == "/api/days":
                today = _today_local_str(self.cfg)
                if self._server_mode():
                    days = sorted(set(_db_days(self._db_path(), self._token(q))) | {today})
                else:
                    days = sorted(set(_read_raw_days().keys()) | {today})
                self._json({"ok": True, "today": today, "days": days})
                return
            if u.path == "/api/day":
                date = (q.get("date") or [_today_local_str(self.cfg)])[0]
                if not _valid_date(date):
                    self._json({"ok": False, "error": "vigane kuupäev"}, 400)
                    return
                if self._server_mode():
                    db_rows = _db_rows_for_day(self._db_path(), self._token(q), date)
                    rows = [
                        {"key": r[7], "hour": r[1], "objekt": r[2], "saavutus": r[3],
                         "takistus": r[4], "teadmine": r[5], "tool": r[6]}
                        for r in db_rows
                    ]
                else:
                    rows = _ui_day_rows(date)
                self._json({"ok": True, "date": date, "rows": rows})
                return
            if u.path == "/api/copy":
                date = (q.get("date") or [_today_local_str(self.cfg)])[0]
                full = (q.get("full") or ["0"])[0] in ("1", "true", "yes")
                if not _valid_date(date):
                    self._json({"ok": False, "error": "vigane kuupäev"}, 400)
                    return
                if self._server_mode():
                    db_rows = _db_rows_for_day(self._db_path(), self._token(q), date)
                    day = _day_row(date, db_rows) if db_rows else [date, 0, _weekday_letter(date), "", "", "", ""]
                    cols = slice(None) if full else slice(3, 7)
                    selected = day[cols]
                    import html as _html
                    def esc(c):
                        return _html.escape(str(c).replace("\r", "").strip()).replace("\n", "<br>")
                    html = "<table><tbody><tr>" + "".join(f"<td>{esc(c)}</td>" for c in selected) + "</tr></tbody></table>"
                    text = "\t".join(str(c).replace("\t", " ") for c in selected)
                else:
                    html, text = _html_table_for_day(date, full)
                self._json({"ok": True, "html": html, "text": text})
                return
            if u.path == "/api/work/status" and self._server_mode():
                self._json({"ok": True, "sessions": _db_work_status(self._db_path(), self._token(q))})
                return
            if u.path == "/api/billing/invoice-lines" and self._server_mode():
                self._json(_db_invoice_lines(self._db_path(), self._token(q), q))
                return
            if u.path == "/api/practice/summary" and self._server_mode():
                self._json(_db_practice_summary(self._db_path(), self._token(q), q))
                return
            self._json({"ok": False, "error": "not found"}, 404)
        except PermissionError as e:
            self._json({"ok": False, "error": str(e)}, 403)
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": str(e)}, 500)

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        try:
            u = urllib.parse.urlparse(self.path)
            data = self._read_json()
            if u.path == "/api/keys" and self._server_mode():
                keys = sorted(_db_keys(self._db_path(), self._token(data=data)))
                self._json({"ok": True, "keys": keys})
                return
            if u.path == "/api/ingest" and self._server_mode():
                rows = data.get("rows") if isinstance(data.get("rows"), list) else []
                keys = data.get("keys") if isinstance(data.get("keys"), list) else []
                _db_ingest_rows(self._db_path(), self._token(data=data), rows, [str(k) for k in keys])
                self._json({"ok": True})
                return
            if u.path == "/api/events" and self._server_mode():
                events = data.get("events") if isinstance(data.get("events"), list) else []
                _db_ingest_events(self._db_path(), self._token(data=data), events)
                self._json({"ok": True})
                return
            if u.path == "/api/work/start" and self._server_mode():
                self._json(_db_work_start(self._db_path(), self._token(data=data), data))
                return
            if u.path == "/api/work/tick" and self._server_mode():
                self._json(_db_work_tick(self._db_path(), self._token(data=data), data))
                return
            if u.path == "/api/work/done" and self._server_mode():
                self._json(_db_work_finish(self._db_path(), self._token(data=data), data, status="done"))
                return
            if u.path == "/api/work/discard" and self._server_mode():
                self._json(_db_work_finish(self._db_path(), self._token(data=data), data, status="discarded"))
                return
            if u.path == "/api/day":
                date = str(data.get("date") or _today_local_str(self.cfg))
                rows = data.get("rows") if isinstance(data.get("rows"), list) else []
                if self._server_mode():
                    _db_replace_day_rows(self._db_path(), self._token(data=data), date, rows)
                    db_rows = _db_rows_for_day(self._db_path(), self._token(data=data), date)
                    ui_rows = [
                        {"key": r[7], "hour": r[1], "objekt": r[2], "saavutus": r[3],
                         "takistus": r[4], "teadmine": r[5], "tool": r[6]}
                        for r in db_rows
                    ]
                else:
                    _replace_day_rows(date, rows, self.cfg)
                    ui_rows = _ui_day_rows(date)
                self._json({"ok": True, "date": date, "rows": ui_rows})
                return
            if u.path == "/api/run":
                if self._server_mode():
                    self._json({"ok": False, "error": "server ei loe kliendi lokaalseid logisid"}, 400)
                    return
                run_once(load_config(), load_projects())
                self._json({"ok": True})
                return
            if u.path == "/api/backfill":
                if self._server_mode():
                    self._json({"ok": False, "error": "server ei loe kliendi lokaalseid logisid"}, 400)
                    return
                hours = int(data.get("hours") or 12)
                hours = max(1, min(hours, 72))
                run_once(load_config(), load_projects(), backfill_hours=hours)
                self._json({"ok": True, "hours": hours})
                return
            self._json({"ok": False, "error": "not found"}, 404)
        except PermissionError as e:
            self._json({"ok": False, "error": str(e)}, 403)
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": str(e)}, 500)


def _serve_http(host: str, port: int, cfg: dict, *, no_browser: bool = False) -> None:
    last_error = None
    httpd = None
    for p in range(port, port + 20):
        try:
            _AitrackHandler.cfg = cfg
            httpd = ThreadingHTTPServer((host, p), _AitrackHandler)
            port = p
            break
        except OSError as e:
            last_error = e
    if httpd is None:
        print(f"Ei saanud serverit käivitada: {last_error}")
        return
    display_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{display_host}:{port}/"
    print(f"aitrack UI: {url}")
    print("Sulgemiseks vajuta Ctrl+C")
    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def cmd_start(args, cfg):
    """Käivita lokaalne brauseri-UI tänaste/varasemate kirjete vaatamiseks ja muutmiseks."""
    _serve_http(args.host, args.port, cfg, no_browser=args.no_browser)


def cmd_serve(args, cfg):
    """Käivita keskserver SQLite andmebaasiga."""
    db_path = _server_db_path(args.db)
    _db_init(db_path)
    server_cfg = copy.deepcopy(cfg)
    server_cfg["_server_mode"] = True
    server_cfg["_db_path"] = str(db_path)
    print(f"SQLite DB: {db_path}")
    _serve_http(args.host, args.port, server_cfg, no_browser=True)


def cmd_user(args, cfg):
    db_path = _server_db_path(args.db)
    _db_init(db_path)
    if args.user_cmd == "add":
        token = _db_add_user(db_path, args.name, args.role)
        print(f"Kasutaja loodud: {args.name}")
        print(f"Token: {token}")
        print("Kliendis seadista:")
        print(f"  aitrack connect --url http://SERVER:8765 --token {token}")
        return
    if args.user_cmd == "list":
        with _db_connect(db_path) as conn:
            rows = conn.execute("SELECT id, name, role, created_at FROM users ORDER BY id").fetchall()
        if not rows:
            print("Kasutajaid pole. Lisa: aitrack user add <nimi>")
            return
        for r in rows:
            print(f"{r['id']:3d}  {r['name']:20s}  {r['role']:8s}  {r['created_at']}")


def cmd_connect(args, cfg):
    cfg2 = load_config()
    cfg2["sink"] = {"type": "server", "server_url": args.url.rstrip("/"), "token": args.token,
                    "webapp_url": "", "path": ""}
    save_config(cfg2)
    print(f"aitrack server seadistatud: {args.url.rstrip('/')}")
    print("Edaspidi saadab 'aitrack run' tunniread ja prompt-eventid serverisse.")


def _require_server_cfg(cfg: dict) -> None:
    sink = cfg.get("sink", {})
    if sink.get("type") != "server" or not sink.get("server_url") or not sink.get("token"):
        raise SystemExit("See käsk vajab keskserverit: aitrack connect --url URL --token TOKEN")


def _work_payload_from_args(args, cfg: dict, *, summary: str = "") -> tuple[dict, dict, dict]:
    ctx = _project_context(getattr(args, "cwd", None) or ".", getattr(args, "issue", None))
    client = _client_info()
    tool = _detect_cli(getattr(args, "tool", None))
    title = summary or " ".join(getattr(args, "summary", []) or []).strip()
    issue_key = _normalise_issue_key(getattr(args, "issue", None)) or ctx.get("issue_key", "")
    project = {
        "project_key": ctx["project_key"], "repo_url": ctx["repo_url"], "name": ctx["name"],
        "local_path": ctx["local_path"], "checkout_id": ctx["checkout_id"], "branch": ctx["branch"],
    }
    session = {
        "client_id": client["client_id"], "device_name": client.get("name", "unknown"),
        "platform": client.get("platform", _platform()), "checkout_id": ctx["checkout_id"],
        "tool": tool, "cwd": ctx["cwd"], "local_path": ctx["local_path"], "branch": ctx["branch"],
    }
    issue = {"provider": ctx.get("issue_provider") or "local", "issue_key": issue_key} if issue_key else {}
    payload = {"project": project, "issue": issue, "session": session,
               "work": {"title": title, "summary": title, "billable": not getattr(args, "non_billable", False)},
               "started_at": _now_utc().isoformat()}
    return payload, ctx, client


def cmd_project_id(args, cfg):
    ctx = _project_context(args.path, args.issue)
    if args.json:
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
    else:
        for k in ("project_key", "repo_url", "name", "local_path", "branch", "issue_key", "checkout_id"):
            print(f"{k}: {ctx.get(k, '')}")


def cmd_work(args, cfg):
    _require_server_cfg(cfg)
    if args.work_cmd == "start":
        summary = " ".join(args.summary).strip()
        if not summary:
            raise SystemExit("Kasuta: aitrack work start [--issue N] 'töö kirjeldus'")
        payload, ctx, client = _work_payload_from_args(args, cfg, summary=summary)
        tool = payload["session"]["tool"]
        active = _active_work_sessions(tool=tool, checkout_id=ctx["checkout_id"])
        if active and not args.force:
            print("Selles checkout'is ja tööriistas on juba aktiivne work_session:")
            for s in active:
                print(f"  session {s['work_session_id']}: {s.get('summary', '')} ({s.get('project_key', '')} {s.get('issue_key', '')})")
            print("Lõpeta enne: aitrack work done 'kokkuvõte'  või kasuta --force teadlikuks paralleelsuseks.")
            return
        body = _server_post("work/start", payload, cfg)
        if not body or not body.get("ok"):
            raise SystemExit(f"work start ebaõnnestus: {body.get('error') if body else 'server ei vastanud'}")
        state = _load_work_state()
        session_id = int(body["work_session_id"])
        state["sessions"] = [s for s in state.get("sessions", []) if s.get("work_session_id") != session_id]
        state["sessions"].append({
            "work_session_id": session_id, "work_item_id": body.get("work_item_id"), "status": "active",
            "summary": summary, "project_key": ctx["project_key"], "issue_key": ctx.get("issue_key", ""),
            "checkout_id": ctx["checkout_id"], "tool": tool, "client_id": client["client_id"],
            "started_at": payload["started_at"], "state_key": _work_state_key(client["client_id"], ctx["checkout_id"], tool),
        })
        _save_work_state(state)
        print(f"Alustatud work_session {session_id}: {ctx['project_key']}" + (f" #{ctx['issue_key']}" if ctx.get("issue_key") else ""))
        return

    if args.work_cmd == "status":
        local = _active_work_sessions()
        if local:
            print("Kohalikud aktiivsed sessioonid:")
            for s in local:
                print(f"  {s['work_session_id']:>5}  {s.get('tool','')}  {s.get('project_key','')} {('#' + s.get('issue_key')) if s.get('issue_key') else ''}  {s.get('summary','')}")
        else:
            print("Kohalikus state'is aktiivseid sessioone pole.")
        body = _server_get("work/status", {}, cfg)
        if body and body.get("ok") and body.get("sessions"):
            print("Serveri aktiivsed sessioonid selle tokeni all:")
            for s in body["sessions"]:
                issue = f"#{s.get('issue_key')}" if s.get("issue_key") else ""
                print(f"  {s['id']:>5}  {s.get('tool','')}  {s.get('project_key','')} {issue}  {s.get('summary','')}")
        return

    if args.work_cmd in ("tick", "done", "discard"):
        cmd_tick(args, cfg) if args.work_cmd == "tick" else _cmd_work_finish(args, cfg, discard=(args.work_cmd == "discard"))
        return

    if args.work_cmd == "switch":
        _cmd_work_finish(args, cfg, discard=False, summary_override=args.done_summary or "pooleli: " + " ".join(args.summary).strip())
        start_args = argparse.Namespace(**vars(args))
        start_args.work_cmd = "start"
        start_args.force = False
        cmd_work(start_args, cfg)
        return


def _select_local_session(args) -> dict:
    sessions = _active_work_sessions()
    if getattr(args, "session_id", None):
        sid = int(args.session_id)
        for s in sessions:
            if int(s.get("work_session_id") or 0) == sid:
                return s
        return {"work_session_id": sid, "summary": "", "status": "active"}
    tool = _detect_cli(getattr(args, "tool", None))
    ctx = _project_context(getattr(args, "cwd", None) or ".", getattr(args, "issue", None))
    matches = [s for s in sessions if s.get("tool") == tool and s.get("checkout_id") == ctx["checkout_id"]]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit("Aktiivset work_session'it ei leitud. Vaata: aitrack work status")
    raise SystemExit("Mitu aktiivset sessiooni sobib. Anna --session-id.")


def cmd_tick(args, cfg):
    _require_server_cfg(cfg)
    sessions = _active_work_sessions()
    if getattr(args, "session_id", None):
        sessions = [s for s in sessions if int(s.get("work_session_id") or 0) == int(args.session_id)]
    if not sessions:
        print("Aktiivseid work_session'eid pole; ticki ei saadetud.")
        return
    sent = 0
    for s in sessions:
        body = _server_post("work/tick", {"work_session_id": s["work_session_id"], "tick_at": _now_utc().isoformat(),
                                          "source": "heartbeat"}, cfg)
        if body and body.get("ok"):
            sent += 1
    print(f"Saadetud ticke: {sent}")


def _cmd_work_finish(args, cfg, *, discard: bool, summary_override: str | None = None):
    _require_server_cfg(cfg)
    s = _select_local_session(args)
    summary = summary_override or " ".join(getattr(args, "summary", []) or []).strip() or s.get("summary", "")
    payload = {"work_session_id": int(s["work_session_id"]), "summary": summary, "ended_at": _now_utc().isoformat()}
    if getattr(args, "minutes", None):
        payload["minutes"] = int(args.minutes)
    if getattr(args, "result", None):
        payload["result"] = args.result
    if getattr(args, "non_billable", False):
        payload["billable"] = False
    op = "work/discard" if discard else "work/done"
    body = _server_post(op, payload, cfg)
    if not body or not body.get("ok"):
        raise SystemExit(f"work finish ebaõnnestus: {body.get('error') if body else 'server ei vastanud'}")
    state = _load_work_state()
    for item in state.get("sessions", []):
        if int(item.get("work_session_id") or 0) == int(s["work_session_id"]):
            item["status"] = "discarded" if discard else "done"
            item["ended_at"] = payload["ended_at"]
            item["minutes"] = body.get("minutes")
    _save_work_state(state)
    print(f"Lõpetatud work_session {s['work_session_id']} ({body.get('minutes')} min, {body.get('status')}).")


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


def _cli_base_cmd() -> str:
    """Käsk, millega kasutaja saab seda skripti käivitada selles checkout'is."""
    if shutil.which("aitrack"):
        return "aitrack"
    py = "python" if _platform() == "windows" else "python3"
    script = ".\\aitrack.py" if _platform() == "windows" else "aitrack.py"
    return f"{py} {script}"


def _copy_command(date: str = "", *, full: bool = False, plat: str | None = None) -> tuple[str, str]:
    """Tagasta (käsk, märkus) Google Sheetsi clipboardi jaoks jooksval OS-il."""
    plat = plat or _platform()
    base = _cli_base_cmd()
    date_arg = f" {date}" if date else ""
    full_arg = " --full" if full else ""
    day_plain = f"{base} day{date_arg}{full_arg}"
    target = "A-lahter" if full else "D-lahter"

    if plat == "linux":
        if shutil.which("wl-copy"):
            return (f"{day_plain} --html | wl-copy -t text/html",
                    f"Vali Sheetsis {target}. HTML hoiab punktid lahtris eraldi ridadel.")
        if shutil.which("xclip"):
            return (f"{day_plain} --html | xclip -selection clipboard -t text/html",
                    f"Vali Sheetsis {target}. HTML hoiab punktid lahtris eraldi ridadel.")
        if shutil.which("xsel"):
            return (f"{day_plain} --flat | xsel --clipboard --input",
                    f"Vali Sheetsis {target}. Märkus: xsel ei anna HTML-i; --flat paneb punktid ühele reale.")
        return (f"{day_plain} --html > /tmp/aitrack-day.html",
                "Clipboardi tööriista ei leitud; paigalda wl-clipboard või xclip ja kleebi HTML väljund.")
    if plat == "macos":
        return (f"{day_plain} | pbcopy",
                f"Vali Sheetsis {target}. pbcopy kasutab macOS-i clipboardi.")
    if plat == "windows":
        # PowerShell Set-Clipboard on tavakasutajale kõige lühem ja töötab ilma lisapakettideta.
        return (f"{day_plain} | Set-Clipboard",
                f"Vali Sheetsis {target}. PowerShellis kasuta Set-Clipboard; CMD-s võib kasutada '| clip'.")
    return (day_plain, f"Kopeeri väljund ja kleebi Sheetsis {target}.")


def _help_text(cfg: dict) -> str:
    last = ""
    days = _read_raw_days()
    if days:
        last = sorted(days)[-1]
    cmd_d, note_d = _copy_command(last)
    cmd_a, note_a = _copy_command(last, full=True)
    date_hint = last or "YYYY-MM-DD"
    cd_line = "" if shutil.which("aitrack") else f"cd {THIS.parent} && "
    return f"""aitrack — kiire abi

Visuaalne päevavaade/editor:
  { _cli_base_cmd() } start                  ava brauseris tänased/varasemad päevad, muuda ja lisa ridu

Kõige sagedasem: kopeeri päeva väljund Google Sheetsi
  D-lahtrisse (ainult Objekt/Saavutused/Takistused/Uued teadmised):
    {cd_line}{cmd_d}
    → {note_d}

  A-lahtrisse (kõik veerud A–G):
    {cd_line}{cmd_a}
    → {note_a}

Põhikäsud
  { _cli_base_cmd() } status                 näita seadistust ja logiallikaid
  { _cli_base_cmd() } day {date_hint}        prindi päeva D–G väljund terminali
  { _cli_base_cmd() } start                  ava brauseris visuaalne päevavaade/editor
  { _cli_base_cmd() } serve                  käivita keskserver SQLite andmebaasiga
  { _cli_base_cmd() } connect --url URL --token TOKEN  ühenda klient keskserveriga
  { _cli_base_cmd() } project-id             näita repo URL-il põhinevat ühist project_key'd
  { _cli_base_cmd() } work start --issue 662 "töö"  alusta serveris work_session'it
  { _cli_base_cmd() } tick                   saada kõigi aktiivsete work_session'ite minut
  { _cli_base_cmd() } work done "kokkuvõte"  lõpeta aktiivne work_session
  { _cli_base_cmd() } note "tekst"           lisa käsitsi märge praegusele tunnile
  { _cli_base_cmd() } note                   näita käsitsi märkmeid
  { _cli_base_cmd() } preview --hours 8      vaata, mida tracker leiaks
  { _cli_base_cmd() } suggest --days 7       soovita logidest projektikaustu
  { _cli_base_cmd() } add <tee>              lisa projekt jälgimisse
  { _cli_base_cmd() } list                   näita jälgitavaid projekte
  { _cli_base_cmd() } backfill --hours 12    töötle tagantjärele viimased tunnid

OS-ide copy-käsud (D-lahtrisse)
  Linux/Wayland:       { _cli_base_cmd() } day {date_hint} --html | wl-copy -t text/html
  Linux/X11:           { _cli_base_cmd() } day {date_hint} --html | xclip -selection clipboard -t text/html
  macOS:               { _cli_base_cmd() } day {date_hint} | pbcopy
  Windows PowerShell:  { _cli_base_cmd() } day {date_hint} | Set-Clipboard
  Windows CMD:         { _cli_base_cmd() } day {date_hint} | clip

Abi konkreetse käsu kohta:
  { _cli_base_cmd() } day --help
  { _cli_base_cmd() } note --help
"""


def cmd_help(args, cfg):
    print(_help_text(cfg))


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
        print(f"Väljund:         päevavaade → {path} ({exists})")
        print(f"Algandmestik:    {HOURS_CSV} ({'olemas' if HOURS_CSV.exists() else 'puudub veel'})")
        copy_cmd, _ = _copy_command()
        print(f"Kleebi Sheetsi:  {copy_cmd}   (vali D-lahter)")
    elif sink.get("type") == "server":
        print(f"Väljund:         aitrack server ({sink.get('server_url') or 'URL PUUDUB'})")
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
                    ("Antigravity", ANTIGRAVITY_HISTORY), ("Pi", PI_SESSIONS),
                    ("OpenCode", OPENCODE_DB)]:
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
    p = argparse.ArgumentParser(
        prog="aitrack",
        description="AI-tööriistade tunnipõhine tööpäevik",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Kiireim Sheets-copy käsk sõltub OS-ist. Vaata: aitrack help\n"
                "Näide Linux/Wayland: aitrack day --html | wl-copy -t text/html"),
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("help", help="näita praktilist abi ja OS-iga sobivaid copy-käske").set_defaults(fn=cmd_help)

    st = sub.add_parser("start", help="ava brauseris visuaalne päevavaade/editor")
    st.add_argument("--host", default="127.0.0.1", help="lokaalse UI host (vaikimisi 127.0.0.1)")
    st.add_argument("--port", type=int, default=8765, help="lokaalse UI port (vaikimisi 8765)")
    st.add_argument("--no-browser", action="store_true", help="ära ava brauserit automaatselt")
    st.set_defaults(fn=cmd_start)

    sv = sub.add_parser("serve", help="käivita keskserver SQLite andmebaasiga")
    sv.add_argument("--host", default="127.0.0.1", help="serveri host (deploys tavaliselt 0.0.0.0)")
    sv.add_argument("--port", type=int, default=8765, help="serveri port")
    sv.add_argument("--db", default=str(SERVER_DB), help="SQLite andmebaasi tee")
    sv.set_defaults(fn=cmd_serve)

    cn = sub.add_parser("connect", help="ühenda see klient aitrack keskserveriga")
    cn.add_argument("--url", required=True, help="serveri URL, nt https://aitrack.example.com")
    cn.add_argument("--token", required=True, help="kasutaja token serverist")
    cn.set_defaults(fn=cmd_connect)

    pid = sub.add_parser("project-id", help="näita serveri ühist projektivõtit (git remote URL või local:<kaust>)")
    pid.add_argument("path", nargs="?", default=".")
    pid.add_argument("--issue", help="issue võti; puudumisel proovitakse branchi nimest")
    pid.add_argument("--json", action="store_true", help="väljasta JSON")
    pid.set_defaults(fn=cmd_project_id)

    tick = sub.add_parser("tick", help="saada kõigi aktiivsete work_session'ite minut serverisse")
    tick.add_argument("--session-id", type=int, help="saada tick ainult sellele sessioonile")
    tick.set_defaults(fn=cmd_tick)

    wk = sub.add_parser("work", help="serveripõhine work_session ajamõõtmine")
    ws = wk.add_subparsers(dest="work_cmd", required=True)
    wstart = ws.add_parser("start", help="alusta uut work_session'it praeguses checkout'is")
    wstart.add_argument("summary", nargs="+", help="töö lühikirjeldus")
    wstart.add_argument("--issue", help="issue number/võti; puudumisel proovitakse branchist")
    wstart.add_argument("--tool", help="agent/tööriist, nt pi/claude/opencode/codex")
    wstart.add_argument("--cwd", help="projekti/checkout'i tee (vaikimisi praegune kaust)")
    wstart.add_argument("--non-billable", action="store_true", help="märgi sessioon vaikimisi mittearveldatavaks")
    wstart.add_argument("--force", action="store_true", help="luba sama checkout+tool paralleelsessioon teadlikult")
    wstart.set_defaults(fn=cmd_work)
    wstatus = ws.add_parser("status", help="näita aktiivseid work_session'eid")
    wstatus.set_defaults(fn=cmd_work)
    wtick = ws.add_parser("tick", help="saada minut serverisse")
    wtick.add_argument("--session-id", type=int)
    wtick.set_defaults(fn=cmd_work)
    wdone = ws.add_parser("done", help="lõpeta aktiivne work_session")
    wdone.add_argument("summary", nargs="*", help="lõpetamise kokkuvõte")
    wdone.add_argument("--session-id", type=int)
    wdone.add_argument("--minutes", type=int, help="käsitsi hinnatud aktiivsed minutid")
    wdone.add_argument("--issue")
    wdone.add_argument("--tool")
    wdone.add_argument("--cwd")
    wdone.add_argument("--result", choices=["kept", "discarded", "superseded", "merged", "review", ""], default="")
    wdone.add_argument("--non-billable", action="store_true")
    wdone.set_defaults(fn=cmd_work)
    wdiscard = ws.add_parser("discard", help="lõpeta sessioon mittearvestatavana")
    wdiscard.add_argument("summary", nargs="*", help="põhjus")
    wdiscard.add_argument("--session-id", type=int)
    wdiscard.add_argument("--minutes", type=int)
    wdiscard.add_argument("--issue")
    wdiscard.add_argument("--tool")
    wdiscard.add_argument("--cwd")
    wdiscard.set_defaults(fn=cmd_work)
    wswitch = ws.add_parser("switch", help="lõpeta praegune sessioon ja alusta uus")
    wswitch.add_argument("summary", nargs="+", help="uue töö kirjeldus")
    wswitch.add_argument("--done-summary", help="eelmise sessiooni kokkuvõte")
    wswitch.add_argument("--minutes", type=int, help="eelmise sessiooni aktiivsed minutid")
    wswitch.add_argument("--issue", help="uue töö issue")
    wswitch.add_argument("--tool")
    wswitch.add_argument("--cwd")
    wswitch.add_argument("--non-billable", action="store_true")
    wswitch.set_defaults(fn=cmd_work)

    up = sub.add_parser("user", help="halda keskserveri kasutajaid")
    us = up.add_subparsers(dest="user_cmd", required=True)
    ua = us.add_parser("add", help="lisa kasutaja ja väljasta token")
    ua.add_argument("name")
    ua.add_argument("--role", default="user", choices=["user", "admin"])
    ua.add_argument("--db", default=str(SERVER_DB))
    ua.set_defaults(fn=cmd_user)
    ul = us.add_parser("list", help="näita kasutajaid")
    ul.add_argument("--db", default=str(SERVER_DB))
    ul.set_defaults(fn=cmd_user)

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

    dy = sub.add_parser("day", help="prindi päeva sisuveerud D–G (tab-eraldus) Sheetsi kleepimiseks")
    dy.add_argument("date", nargs="?", help="kuupäev YYYY-MM-DD (vaikimisi viimane päev)")
    dy.add_argument("--all", action="store_true", help="prindi kõik päevad")
    dy.add_argument("--header", action="store_true", help="lisa ka päiserida")
    dy.add_argument("--full", action="store_true", help="kõik 7 veergu (ka Kuupäev/Punkte/Nädalapäev)")
    dy.add_argument("--flat", action="store_true", help="clipboard-kindel üks füüsiline TSV-rida (sisemised reavahetused → ·)")
    dy.add_argument("--html", action="store_true", help="HTML-tabel clipboardi jaoks (säilitab punktid lahtris eri ridadel)")
    dy.set_defaults(fn=cmd_day)

    args = p.parse_args()
    if not hasattr(args, "fn"):
        cmd_help(args, cfg)
        return
    args.fn(args, cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
