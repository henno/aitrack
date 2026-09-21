"""Read Codex desktop rollouts and CLI history without tool or internal context."""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class Message:
    session_id: str
    project: str
    ts: dt.datetime
    role: str
    text: str
    source: str


def _objects(path: Path) -> Iterator[dict]:
    try:
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue  # An active rollout may end with an incomplete JSON line.
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


def _timestamp(value) -> dt.datetime | None:
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _user_text(text: str) -> str:
    # These are app-injected envelopes, sometimes followed by the actual request.
    text = re.sub(
        r'<(environment_context|in-app-browser-context|app-context|recommended_plugins|permissions instructions)\b[^>]*>.*?</\1>',
        '', text, flags=re.DOTALL,
    ).strip()
    if text.startswith(('# AGENTS.md instructions', '[Request interrupted',
                        'The following is the Codex agent history',
                        'The following is the Codex agent history added')):
        return ''
    return text


def _message(item: dict) -> tuple[str, str, str] | None:
    payload = item.get('payload')
    if not isinstance(payload, dict):
        return None
    kind = item.get('type')
    if kind == 'response_item' and payload.get('type') == 'message':
        role = payload.get('role')
        if role not in {'user', 'assistant'} or payload.get('channel') == 'analysis':
            return None
        content = payload.get('content')
        if not isinstance(content, list):
            return None
        text = '\n'.join(block['text'] for block in content
                         if isinstance(block, dict) and block.get('type') in {'input_text', 'output_text', 'text'}
                         and isinstance(block.get('text'), str))
        return role, text, 'response'
    if kind == 'event_msg':
        role = {'user_message': 'user', 'agent_message': 'assistant'}.get(payload.get('type'))
        text = payload.get('message')
        if role and isinstance(text, str):
            return role, text, 'event'
    return None


def _internal_session(payload: dict) -> bool:
    source = payload.get('source')
    if not isinstance(source, dict):
        return False
    subagent = source.get('subagent')
    return subagent == 'guardian' or (isinstance(subagent, dict) and subagent.get('other') == 'guardian')


def _deduplicate(messages: list[Message]) -> list[Message]:
    # Prefer response items, then event messages, then second-resolution history.
    # Match one copy from each representation; repeated turns in one source survive.
    groups: dict[tuple, list[tuple[Message, set[str]]]] = {}
    output = []
    for msg in sorted(messages, key=lambda m: ({'response': 0, 'event': 1, 'history': 2}[m.source], m.ts)):
        key = (msg.session_id, msg.project, msg.role, ' '.join(msg.text.split()))
        matches = groups.setdefault(key, [])
        for previous, sources in matches:
            if msg.source not in sources and abs((previous.ts - msg.ts).total_seconds()) <= 2:
                sources.add(msg.source)
                break
        else:
            matches.append((msg, {msg.source}))
            output.append(msg)
    return sorted(output, key=lambda m: m.ts)


def read_messages(sessions: Path, history: Path, since: dt.datetime, is_user_prompt) -> list[Message]:
    history_items = []
    for item in _objects(history):
        ts = _timestamp(item.get('ts'))
        text = item.get('text')
        sid = item.get('session_id')
        if ts and ts > since and isinstance(text, str) and isinstance(sid, str):
            text = _user_text(text)
            if is_user_prompt(text):
                history_items.append((sid, ts, text))
    needed_ids = {sid for sid, _, _ in history_items}
    projects = {}
    excluded = set()
    messages = []
    for path in sorted(sessions.rglob('rollout-*.jsonl')):
        try:
            recent = path.stat().st_mtime >= since.timestamp() - 3600
        except OSError:
            continue
        if not recent and not any(sid in path.name for sid in needed_ids):
            continue
        sid = ''
        cwd = ''
        for item in _objects(path):
            payload = item.get('payload')
            if not isinstance(payload, dict):
                continue
            if item.get('type') == 'session_meta':
                sid = payload.get('id') if isinstance(payload.get('id'), str) else ''
                if _internal_session(payload):
                    excluded.add(sid)
                    break
                cwd = payload.get('cwd') if isinstance(payload.get('cwd'), str) else ''
                if sid and cwd:
                    projects[sid] = cwd
                if not recent:
                    break  # Only metadata is needed for legacy history resolution.
                continue
            if item.get('type') == 'turn_context':
                if isinstance(payload.get('cwd'), str) and payload['cwd']:
                    cwd = payload['cwd']
                continue
            parsed = _message(item)
            ts = _timestamp(item.get('timestamp'))
            if not parsed or not ts or ts <= since or not sid or not cwd:
                continue
            role, text, source = parsed
            text = _user_text(text) if role == 'user' else text.strip()
            if not text or (role == 'user' and not is_user_prompt(text)):
                continue
            messages.append(Message(sid, cwd, ts, role, text, source))
    for sid, ts, text in history_items:
        if sid in projects and sid not in excluded:
            # Rollout turn_context can override session cwd. Resolve duplicates by
            # session/text/time before falling back to the legacy session project.
            duplicate = any(m.session_id == sid and m.role == 'user' and
                            ' '.join(m.text.split()) == ' '.join(text.split()) and
                            abs((m.ts - ts).total_seconds()) <= 2 for m in messages)
            if not duplicate:
                messages.append(Message(sid, projects[sid], ts, 'user', text, 'history'))
    return _deduplicate(messages)
