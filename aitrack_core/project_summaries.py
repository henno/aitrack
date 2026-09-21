"""Keep parallel projects visible while retaining one diary row per hour."""
from pathlib import Path

FIELDS = ('objekt', 'saavutus', 'takistus', 'teadmine')


def combine_summaries(summaries):
    """Label every column consistently so results stay attached to their project."""
    if len(summaries) == 1:
        return summaries[0][1]
    return {field: '\n'.join(f'• {label}: {summary[field]}' for label, summary in summaries)
            for field in FIELDS}


def summarize_projects(items, transcript_items, hour_label, cfg, *, summarize,
                       transcript_inputs, before_summary):
    projects = sorted({project for _, project in items + transcript_items})
    limit = int(cfg.get('max_prompts_per_bucket', 40))
    names = cfg.get('object_names') or {}
    summaries = []
    for project in projects:
        records = sorted((r for r, p in items if p == project), key=lambda r: r.ts)
        transcript = [(r, p) for r, p in transcript_items if p == project]
        inputs = transcript_inputs(transcript, limit=limit * 2) or [r.text for r in records][:limit]
        name = Path(project).name
        before_summary()
        summary = summarize(inputs, name, hour_label, cfg)
        label = names.get(name) or name
        summaries.append((label, summary))
    return combine_summaries(summaries)
