"""Bounded, chronological screenshots from one execution only."""
from pathlib import Path
import json
import xml.etree.ElementTree as ET
from .schemas import ExecutionPlan, SourceCase


def compilation_evidence(source: SourceCase, runs_dir: Path) -> dict:
    """Reuse one genuinely executed matching flow, never an imported/manual verdict."""
    root = runs_dir.resolve()
    candidates = []
    for directory in root.iterdir() if root.exists() else []:
        if not directory.is_dir() or not directory.resolve().is_relative_to(root):
            continue
        if any(not (directory / name).resolve().is_relative_to(root)
               for name in ('result.json', 'plan.json')):
            continue
        try:
            result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
            if (result.get('case_id') != source.case_id or result.get('status') != 'passed'
                    or result.get('execution_mode') == 'native_evidence_import'
                    or not result.get('steps') or not result.get('oracles')
                    or any(step.get('status') != 'passed' for step in result['steps'])
                    or any(oracle.get('passed') is not True for oracle in result['oracles'])):
                continue
            plan = ExecutionPlan.model_validate_json((directory / 'plan.json').read_text(encoding='utf-8'))
            if any(getattr(plan.source, key) != getattr(source, key)
                   for key in ('case_id', 'title', 'precondition', 'steps', 'expected')):
                continue
            if not select_screenshots(directory, result.get('events', []), limit=2):
                continue
            candidates.append((str(result.get('finished_at') or ''), directory, result, plan))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    from .collection import collection_context
    import os
    try:
        collection = collection_context(source, root.parent, os.getenv("ANDROID_APP_PACKAGE", "com.example.app1"))
    except ValueError as exc:
        collection = {'warning': str(exc), 'pages': [], 'routes': []}
    if not candidates:
        return {"collection_evidence": collection} if collection else {}
    _, directory, result, plan = max(candidates, key=lambda item: (item[0], item[1].name))
    context = {'run_id': directory.name, 'observed_at': result.get('finished_at'),
               'previous_verified_plan': plan.model_dump(mode='json'),
               'scope': '历史真机流程证据，只用于可用导航和定位；不是新验收依据，不证明旧数据仍存在。'}
    if collection:
        context["collection_evidence"] = collection
    page = (directory / str(result.get('final_page_source') or '')).resolve()
    if page.is_relative_to(directory.resolve()) and page.is_file():
        context['observed_page'] = compact_page_source(page.read_text(encoding='utf-8'), [source.title, source.expected])
    return context


def compact_page_source(content: str, terms: list[str], limit: int = 24000) -> dict:
    """Keep readable nodes and hierarchy paths; preserve the raw file separately."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return {"content": content[:limit], "omitted_nodes": None,
                "warning": "XML 无法解析；提供原文片段，截断不表示节点不存在", "truncated": len(content) > limit}
    nodes = []
    def visit(node, path):
        attributes = {key: value for key, value in node.attrib.items() if value and key in {
            "text", "resource-id", "content-desc", "class", "type", "name", "label", "value",
            "enabled", "visible", "displayed", "selected", "checked", "clickable", "bounds", "x", "y", "width", "height"}}
        if attributes:
            nodes.append({"path": path, "tag": node.tag, **attributes})
        for index, child in enumerate(node):
            visit(child, f"{path}/{index}")
    visit(root, "0")
    if not nodes:
        return {"content": content[:limit], "omitted_nodes": 0, "truncated": len(content) > limit}
    # Prefer text/identity and relevant nodes when a large tree exceeds the budget.
    def priority(node):
        text = json.dumps(node, ensure_ascii=False)
        return (any(term and term in text for term in terms),
                any(node.get(key) for key in ("text", "resource-id", "content-desc", "label", "name", "value")))
    selected, size = [], 0
    for index, node in sorted(enumerate(nodes), key=lambda item: priority(item[1]), reverse=True):
        line = json.dumps(node, ensure_ascii=False)
        if size + len(line) + 1 <= limit:
            selected.append((index, line))
            size += len(line) + 1
    return {"content": "\n".join(line for _, line in sorted(selected)),
            "omitted_nodes": len(nodes) - len(selected), "truncated": len(selected) < len(nodes)}


def select_screenshots(run_dir: Path, events: list[dict], limit: int = 12) -> list[tuple[Path, dict]]:
    root = run_dir.resolve()
    available, seen = [], set()
    for event in events:
        name = str(event.get("screenshot") or "")
        path = (root / name).resolve()
        if (path in seen or not path.is_relative_to(root) or not path.is_file()
                or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}
                or path.stat().st_size > 5_000_000):
            continue
        seen.add(path)
        available.append((path, {key: event[key] for key in
            ("event_id", "type", "step_id", "oracle_id", "phase", "recorded_at", "screenshot",
             "widget_bounds", "pixel_x", "pixel_y",
             "checkpoint_started_at", "checkpoint_finished_at", "page_source", "page_source_warning",
             "screenshot_started_at", "screenshot_finished_at", "page_source_started_at", "page_source_finished_at") if key in event}))
    if len(available) > limit:
        # Keep endpoints and failed actions, then spread the remaining budget.
        indices = {0, len(available) - 1}
        indices.update(i for i, (_, event) in enumerate(available) if event.get("type") == "step_failed")
        for i in range(limit):
            if len(indices) >= limit:
                break
            indices.add(round(i * (len(available) - 1) / (limit - 1)))
        for i in range(len(available)):
            if len(indices) >= limit:
                break
            indices.add(i)
        available = [available[i] for i in sorted(indices)[:limit]]
    selected, size = [], 0
    for path, event in available:
        size += path.stat().st_size
        if size > 24_000_000:
            break
        selected.append((path, event))
    return selected


def select_failure_screenshots(run_dir: Path, events: list[dict]) -> list[tuple[Path, dict]]:
    """Return the failed checkpoint and the preceding screenshot, if available."""
    photographed = [index for index, event in enumerate(events) if event.get("screenshot")]
    failures = [index for index in photographed if events[index].get("type") == "step_failed" or (
        events[index].get("type") == "oracle_result" and events[index].get("passed") is False)]
    if failures:
        last = failures[-1]
        nearby = [index for index in photographed if index < last][-1:] + [last]
    else:
        nearby = photographed[-2:]
    return select_screenshots(run_dir, [events[index] for index in nearby], limit=2)
