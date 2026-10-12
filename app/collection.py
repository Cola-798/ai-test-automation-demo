"""AI-led preparation tasks. Evidence here is never a test pass/fail record."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .collection_ai import CollectionDecision, StrictModel


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_hash(case: dict) -> str:
    value = {k: case.get(k, "") for k in ("case_id", "title", "precondition", "steps", "expected")}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_file(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        raise FileNotFoundError("资料文件不存在或路径越界")
    return candidate


def existing_pages(root: Path) -> list[dict]:
    """Expose existing captures without publishing them as newly verified evidence."""
    pages = {}
    for folder in ('page-capture', 'artifacts/locator_capture'):
        directory = root / folder
        if not directory.exists():
            continue
        for path in sorted(directory.rglob('*')):
            if path.suffix.lower() not in {'.xml', '.png'} or not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
                continue
            key = path.with_suffix('').relative_to(root).as_posix()
            entry = pages.setdefault(key, {'page_id': key, 'name': path.stem, 'xml': None, 'screenshot': None,
                                          'state': '历史采集资料，未做本次真机核验'})
            entry['xml' if path.suffix.lower() == '.xml' else 'screenshot'] = path.relative_to(root).as_posix()
    if pages or not (root / 'demo.db').is_file():
        return list(pages.values())
    # Read only evidence created in the configured local runtime.
    import sqlite3
    try:
        with sqlite3.connect(f"file:{(root / 'demo.db').resolve().as_posix()}?mode=ro", uri=True) as connection:
            runs = connection.execute('SELECT case_id, qualification_run_id FROM case_device_state WHERE qualification_run_id IS NOT NULL').fetchall()
    except sqlite3.DatabaseError:
        return []
    for case_id, run_id in runs:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', run_id):
            continue
        try:
            directory = root / 'runs' / run_id
            result = json.loads(safe_file(root, f'runs/{run_id}/result.json').read_text(encoding='utf-8'))
            plan_file = directory / 'plan.json'
            plan = json.loads(safe_file(root, f'runs/{run_id}/plan.json').read_text(encoding='utf-8')) if plan_file.is_file() else {}
            steps = {step['step_id']: step for step in plan.get('steps', [])}
            refs = [(event, event.get('screenshot'), event.get('page_source')) for event in result.get('events', []) if event.get('screenshot') or event.get('page_source')]
            refs.append(({'title': '运行结束页面结构'}, None, result.get('final_page_source')))
            for event, screenshot, xml in refs:
                item = {'case_id': case_id, 'xml': None, 'screenshot': None,
                        'state': '当前首轮记录中的历史页面证据，未做本次真机核验'}
                for field, reference in [('screenshot', screenshot), ('xml', xml)]:
                    if reference:
                        candidate = (directory / reference).resolve()
                        if candidate.is_relative_to(directory.resolve()) and candidate.is_file() and candidate.suffix.lower() == ('.png' if field == 'screenshot' else '.xml'):
                            item[field] = candidate.relative_to(root.resolve()).as_posix()
                if not (item['xml'] or item['screenshot']):
                    continue
                step = steps.get(event.get('step_id'), {})
                item['page_id'] = item['xml'] or item['screenshot']
                item['name'] = f"{case_id} · {Path(item['page_id']).stem}"
                item['title'] = f"{case_id} · {step.get('title') or event.get('title') or '验收截图'}"
                item['related_targets'] = [step['target_key']] if step.get('target_key') else []
                pages[item['page_id']] = item
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return list(pages.values())


def collection_app_sources(root: Path, project: Path, package: str) -> dict:
    # Only the configured local runtime is available; no historical app roots.
    name = "app2" if package == "com.example.app2" else "app1"
    return {"current": {"name": name, "app_package": package, "root": root}}


def collection_library(root: Path, package: str) -> dict:
    path = root / "locator_catalog.json"
    payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"app_package": package, "targets": {}}
    if payload.get("app_package") != package:
        raise ValueError("定位资料与所选 App 不一致")
    targets = dict(payload.get("targets", {}))
    data = published_collection(root, package)
    for key, target in data["targets"].items():
        proof = target.get("verification", {})
        if key not in targets and proof.get("status") == "locator_verified" and proof.get("matched_count") == 1:
            targets[key] = target
    pages = existing_pages(root)
    for page in pages:
        related = [key for key, target in targets.items() if any(ref in target.get("evidence", []) for ref in (page["xml"], page["screenshot"]) if ref)]
        page["related_targets"] = list(dict.fromkeys([*page.get("related_targets", []), *related]))
        page.setdefault("title", targets[related[0]]["description"] if related else page["name"])
    return {**data, "app_package": package, "existing_pages": pages,
            "existing_targets": [{"target_key": key, **value} for key, value in targets.items()]}


def case_preparations(root: Path, package: str, library: dict) -> list[dict]:
    """Associate declared plan targets with evidence; never infer live readiness."""
    import sqlite3
    from .schemas import ExecutionPlan, SourceCase
    with sqlite3.connect(f"file:{(root / 'demo.db').resolve().as_posix()}?mode=ro", uri=True) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'source_cases' not in tables:
            return []
        cases = [SourceCase.model_validate_json(row[0]) for row in connection.execute('SELECT source_json FROM source_cases ORDER BY case_id')]
        plans = {}
        if 'plans' in tables:
            for row in connection.execute('SELECT plan_json FROM plans ORDER BY version DESC, record_id DESC'):
                plan = ExecutionPlan.model_validate_json(row[0])
                plans.setdefault(plan.source.case_id, plan)
    observations = {}
    task_files = []
    for path in (root / 'collection/tasks').glob('*/task.json'):
        try:
            task = json.loads(safe_file(root, path.relative_to(root).as_posix()).read_text(encoding='utf-8'))
            if task.get('app_package') == package:
                task_files.append(task)
        except (OSError, ValueError):
            continue
    signatures = {case.case_id: source_hash(case.model_dump()) for case in cases}
    for task in sorted(task_files, key=lambda task: task.get('updated_at', '')):
        for observation in task.get('readiness', []):
            cid = observation.get('case_id')
            if cid in signatures and task.get('source_hashes', {}).get(cid) == signatures[cid]:
                observations[(cid, observation.get('condition'))] = observation
    targets = {target['target_key']: target for target in library['existing_targets']}
    rows = []
    for case in cases:
        plan = plans.get(case.case_id)
        stale = plan is not None and source_hash(plan.source.model_dump()) != signatures[case.case_id]
        if stale:
            plan = None
        keys, conditions = set(), []
        if plan:
            groups = [plan.steps, plan.oracles, plan.state_contract.restore_steps, plan.state_contract.restore_oracles]
            for requirement in plan.state_contract.requires:
                conditions.append(requirement.description)
                groups.extend([requirement.check_steps, requirement.check_oracles, requirement.prepare_steps, requirement.prepare_oracles])
            keys = {item.target_key for group in groups for item in group if item.target_key}
        configured = [{'target_key': key, 'description': targets[key]['description']} for key in sorted(keys & targets.keys())]
        frozen = sorted(key for key in keys if key not in targets and plan.locator_snapshot.get(key)) if plan else []
        missing = sorted(keys - targets.keys() - set(frozen))
        pages = [page for page in library['existing_pages'] if page.get('case_id') == case.case_id or
                 (not page.get('case_id') and keys.intersection(page.get('related_targets', [])))]
        external = [r for (cid, _), r in observations.items() if cid == case.case_id and r.get('state') == '需外部条件']
        gaps, next_steps = [], []
        if not plan:
            gaps.append('原文已更新，旧计划不能作为本次准备依据。' if stale else '还没有与原文对应的执行计划，暂不能确定所需页面与控件。')
            next_steps.append('先在用例与计划中整理当前原文的执行计划，再按计划补充资料。')
        if missing:
            gaps.append('计划引用的定位资料未找到：' + '、'.join(missing))
            next_steps.append('连接手机后补采这些控件，不猜测控件 ID 或新增验收条件。')
        if plan and not pages:
            gaps.append('尚未找到与这条用例或计划控件明确关联的页面证据；不表示 App 没有该页面。')
            next_steps.append('关联已有页面证据，或连接手机后补采对应页面。')
        for observation in external:
            gaps.append('历史采集记录提示：' + observation['condition'] + '。' + observation.get('reason', ''))
        if external:
            next_steps.append('确认历史记录提示的外部条件是否已补齐，再重新检查现场。')
        next_steps.append('按原文检查当前账号、权限和测试数据；这些条件尚未做本次现场核验。')
        state = '需要外部条件' if external else '待整理计划' if not plan else '缺定位资料' if missing else '缺页面关联证据' if not pages else '已有资料'
        rows.append({'case_id': case.case_id, 'title': case.title, 'source': case.model_dump(mode='json'),
                     'state': state, 'live_state': '尚未现场核验', 'plan_version': plan.version if plan else None,
                     'plan_status': plan.status.value if plan else None, 'declared_conditions': conditions,
                     'targets': configured, 'snapshot_targets': frozen, 'missing_targets': missing,
                     'pages': [{'page_id': p['page_id'], 'name': p['name'], 'title': p.get('title', p['name']),
                                'xml': p['xml'], 'screenshot': p['screenshot']} for p in pages],
                     'gaps': gaps, 'next_steps': next_steps})
    return rows


def published_collection(root: Path, package: str) -> dict:
    pointer = root / "collection" / "current.json"
    if not pointer.is_file():
        return {"targets": {}, "pages": [], "routes": []}
    try:
        version = json.loads(pointer.read_text(encoding="utf-8"))["version"]
        if not re.fullmatch(r"[a-f0-9]{32}", version):
            raise ValueError("发布版本无效")
        path = safe_file(root / "collection", f"versions/{version}/catalog.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("app_package") != package or data.get("version") != version:
            raise ValueError("采集库包名或版本不匹配")
        if not isinstance(data.get("targets"), dict) or not isinstance(data.get("pages"), list):
            raise ValueError("采集库结构不完整")
        from .schemas import Locator
        for key, target in data['targets'].items():
            if not re.fullmatch(r'[a-z][a-z0-9_.]{1,119}', key) or not isinstance(target, dict):
                raise ValueError('采集目标结构无效')
            if not isinstance(target.get('description'), str) or not isinstance(target.get('verification'), dict):
                raise ValueError('采集目标缺少描述或核验依据')
            if not isinstance(target.get('android_locators'), list) or not target['android_locators']:
                raise ValueError('采集目标缺少定位')
            for locator in target['android_locators']:
                Locator.model_validate(locator)
        if not isinstance(data.get('routes'), list) or any(not isinstance(item, dict) for item in [*data['pages'], *data['routes']]):
            raise ValueError('页面或路径结构无效')
        return data
    except (KeyError, OSError, ValueError, TypeError) as exc:
        raise ValueError(f"采集资料损坏，未采用增量：{exc}") from exc


class CollectionInput(StrictModel):
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    device_udid: str = Field(min_length=1, max_length=150)
    app_id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r'^[a-z0-9_-]+$')
    case_ids: list[str] = Field(default_factory=list, max_length=20)
    scope_text: str = Field(default="", max_length=1000)
    authorized_effects: list[Literal["create", "delete", "purchase", "logout", "cancel_account", "share"]] = Field(default_factory=list)
    max_steps: int = Field(default=40, ge=1, le=80)
    max_seconds: int = Field(default=900, ge=10, le=1800)

    @model_validator(mode="after")
    def scope_required(self):
        if not self.case_ids and not self.scope_text.strip():
            raise ValueError("请选择用例或填写采集范围")
        return self


def parse_nodes(xml: str) -> tuple[str, list[dict], bool]:
    tree = ET.fromstring(xml)
    nodes, sensitive = [], False
    for index, element in enumerate(tree.iter()):
        identity = ' '.join(element.get(k, '') for k in ('resource-id', 'content-desc', 'hint'))
        secret_input = 'EditText' in element.get('class', '') and bool(re.search(r'password|phone|mobile|verification|验证码|密码|手机号', identity, re.I))
        if element.get("password") == "true" or secret_input:
            sensitive = True
            element.set("text", "[敏感字段]")
            element.set('password', 'true')
        elif 'EditText' in element.get('class', ''):
            # Unknown Apps may not identify verification fields; keep all editable values local.
            sensitive = True
            if element.get('text'):
                element.set('text', '[输入内容已隐藏]')
        attrs = {k: v for k, v in element.attrib.items() if k in {
            "text", "resource-id", "content-desc", "class", "package", "bounds",
            "clickable", "scrollable", "enabled", "selected", "checked", "password"}}
        if attrs:
            nodes.append({"node_id": str(index), **attrs})
    return ET.tostring(tree, encoding="unicode"), nodes, sensitive


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return "'" + value + "'"
    if '"' not in value:
        return '"' + value + '"'
    return "concat(" + ',"\'",'.join("'" + p + "'" for p in value.split("'")) + ")"


def node_locator(node: dict) -> dict | None:
    from .schemas import Locator
    if node.get("password") == "true":
        return None
    # Derive from observed XML; never accept a model-supplied XPath or resource-id.
    fields = [(k, node[k]) for k in ("resource-id", "content-desc", "class", "text") if node.get(k)
              and node[k] not in {'[敏感字段]', '[输入内容已隐藏]'}]
    if not any(k in {"resource-id", "content-desc", "text"} for k, _ in fields):
        return None
    value = "//*[" + " and ".join(f"@{k}={xpath_literal(v)}" for k, v in fields) + "]"
    if len(value) > 500:
        return None
    return Locator(strategy="xpath", value=value).model_dump(mode="json")


def find_locator(driver, locator: dict):
    from .executor import AppiumPlanExecutor
    from .schemas import Locator
    by, value = AppiumPlanExecutor._appium_locator(Locator.model_validate(locator))
    return [e for e in driver.find_elements(by, value) if e.is_displayed()]


def page_fingerprint(nodes: list[dict]) -> str:
    structural = [{k: re.sub(r"\d+", "#", v) for k, v in n.items() if k not in {"bounds", "node_id"}}
                  for n in nodes]
    return hashlib.sha256(json.dumps(structural, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class CollectionManager:
    def __init__(self, root: Path, coordinator, *, ai_factory=None, session_factory=None):
        self.root = root.resolve()
        self.tasks_dir = self.root / "collection" / "tasks"
        self.coordinator = coordinator
        self.ai_factory = ai_factory
        self.session_factory = session_factory
        self._lock = threading.RLock()
        self._stops: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}

    def task_dir(self, task_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise KeyError("任务编号无效")
        path = self.tasks_dir / task_id
        if not path.resolve().is_relative_to(self.tasks_dir.resolve()):
            raise KeyError(task_id)
        return path

    def get(self, task_id: str) -> dict:
        path = self.task_dir(task_id) / "task.json"
        if not path.is_file():
            raise KeyError(task_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict]:
        if not self.tasks_dir.exists():
            return []
        return sorted([self.get(p.parent.name) for p in self.tasks_dir.glob("*/task.json")],
                      key=lambda t: t["updated_at"], reverse=True)

    def recover_interrupted(self) -> None:
        for task in self.list():
            if task["status"] == "running":
                task.update(status="interrupted", message="服务重启导致采集中断；继续前将重新核验现场")
                self.save(task)

    def save(self, task: dict) -> None:
        task["updated_at"] = now()
        atomic_json(self.task_dir(task["task_id"]) / "task.json", task)

    def start(self, request: CollectionInput, cases: list[dict], *, plans: list[dict] | None = None) -> dict:
        from .device import device_config_for_platform
        from .collection_ai import CollectionAI
        with self._lock:
            for old in self.list():
                if old["request"]["request_id"] == request.request_id:
                    if old["request"] != request.model_dump():
                        raise ValueError("同一请求编号不能更换采集范围")
                    return old
            config = device_config_for_platform("android", device_udid=request.device_udid)
            ai = self.ai_factory() if self.ai_factory else CollectionAI()
            release = self.coordinator.reserve()
            task_id = uuid4().hex
            task = {"task_id": task_id, "request": request.model_dump(), "app_package": config.package,
                    "device_udid": config.udid, "device_name": config.device_name, "app_version": "",
                    "sources": cases, "source_hashes": {c["case_id"]: source_hash(c) for c in cases},
                    "existing_preparations": plans or [], "status": "running", "created_at": now(),
                    "steps": 0, "elapsed_seconds": 0, "pages": [], "routes": [], "targets": {},
                    "controls": [], "readiness": [{'case_id': c['case_id'], 'condition': '用例准备覆盖',
                    'state': '尚未核实', 'reason': '逐项观察不等于整条用例准备就绪；解析和正式执行仍需校验。'} for c in cases],
                    "objects": [], "missing": [], "message": "准备连接设备",
                    "test_name": f"AUTO-{task_id[:12]}", "creation_pending": False}
            try:
                self.save(task)
                self._launch(task, config, ai, release)
            except Exception:
                release()
                raise
            return task

    def resume(self, task_id: str, current_cases: list[dict]) -> dict:
        from .device import device_config_for_platform
        from .collection_ai import CollectionAI
        with self._lock:
            task = self.get(task_id)
            if task["status"] not in {"waiting_input", "partial", "stopped", "interrupted", "failed"}:
                raise ValueError("当前任务状态不能继续")
            if task["source_hashes"] != {c["case_id"]: source_hash(c) for c in current_cases}:
                raise ValueError("用例原文已变化，请创建新采集任务")
            if task["app_package"] != os.getenv("ANDROID_APP_PACKAGE", "com.example.app1"):
                raise ValueError("任务所属 App 与当前环境不一致")
            config = device_config_for_platform("android", device_udid=task["device_udid"])
            ai = self.ai_factory() if self.ai_factory else CollectionAI()
            release = self.coordinator.reserve()
            task.update(status="running", message="重新核验现场后继续", missing=[])
            try:
                self.save(task)
                self._launch(task, config, ai, release)
            except Exception:
                release()
                raise
            return task

    def stop(self, task_id: str) -> dict:
        with self._lock:
            task = self.get(task_id)
            event = self._stops.get(task_id)
            if task["status"] != "running" or event is None:
                raise ValueError("任务当前未运行")
            event.set()
            return {"task_id": task_id, "stop_requested": True}

    def _launch(self, task, config, ai, release) -> None:
        event = threading.Event()
        self._stops[task["task_id"]] = event
        worker = threading.Thread(target=self._run, args=(task, config, ai, event, release), daemon=True)
        self._threads[task["task_id"]] = worker
        worker.start()

    def _capture(self, task: dict, driver) -> dict:
        raw_xml = driver.page_source
        xml, nodes, sensitive = parse_nodes(raw_xml)
        snapshot_id = uuid4().hex
        folder = self.task_dir(task["task_id"]) / "pages"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (snapshot_id + ".xml")).write_text(xml, encoding="utf-8")
        png = folder / (snapshot_id + ".png")
        # Password screens stay local-text-only; do not persist/send their screenshot.
        if not sensitive and not driver.get_screenshot_as_file(str(png)):
            raise RuntimeError("设备未返回截图")
        snap = {"snapshot_id": snapshot_id, "nodes": nodes, "fingerprint": page_fingerprint(nodes),
                "captured_at": now(), "xml": f"pages/{snapshot_id}.xml",
                "screenshot": f"pages/{snapshot_id}.png" if not sensitive else None,
                "sensitive": sensitive, "raw_hash": hashlib.sha256(raw_xml.encode()).hexdigest()}
        atomic_json(folder / (snapshot_id + ".json"), snap)
        return snap

    def _collect(self, task, decision: CollectionDecision, snap, driver) -> None:
        if decision.snapshot_id != snap["snapshot_id"]:
            raise ValueError("AI 引用过期页面，未执行动作")
        nodes = {n["node_id"]: n for n in snap["nodes"]}
        page = {k: snap[k] for k in ("snapshot_id", "fingerprint", "captured_at", "xml", "screenshot")}
        page.update(name=decision.page_name, summary=decision.summary, task_id=task["task_id"],
                    source_hashes=task['source_hashes'],
                    device_udid=task["device_udid"], app_version=task["app_version"], case_ids=[c["case_id"] for c in task["sources"]])
        page['observed_nodes'] = [{k: v for k, v in n.items() if k != 'bounds'} for n in snap['nodes'][:200]]
        page['omitted_nodes'] = max(0, len(snap['nodes']) - 200)
        task["pages"].append(page)
        if task['creation_pending']:
            matches = [n for n in snap['nodes'] if task['test_name'] in n.get('text', '') and 'EditText' not in n.get('class', '')]
            for node in matches:
                locator = node_locator(node)
                if locator and len(find_locator(driver, locator)) == 1:
                    task['objects'].append({'name': task['test_name'], 'snapshot_id': snap['snapshot_id'],
                                            'locator': locator, 'device_udid': task['device_udid']})
                    task['creation_pending'] = False
                    task['test_name'] = f"AUTO-{task['task_id'][:12]}-{len(task['objects']) + 1}"
                    task['unique_input_written'] = False
                    break
        from .locator_catalog import active_targets
        existing = active_targets()
        for control in decision.controls:
            node = nodes.get(control.node_id)
            if node is None:
                raise ValueError("AI 控件引用不存在的节点")
            locator = node_locator(node)
            count = len(find_locator(driver, locator)) if locator else 0
            receipt = {**control.model_dump(), "snapshot_id": snap["snapshot_id"], "matched_count": count,
                       "status": "locator_verified" if count == 1 else "candidate", "effect_verified": False}
            task["controls"].append(receipt)
            if count != 1:
                continue
            key = control.target_key
            if key in existing:
                expected = [l.model_dump(mode="json") for l in existing[key].android_locators]
                if locator not in expected:
                    receipt.update(status="conflict", reason="保留现有同名控件，未覆盖")
                    continue
            task["targets"][key] = {"description": control.description, "android_locators": [locator],
                                      "verification": receipt, "device_udid": task["device_udid"],
                                      "app_version": task["app_version"], "task_id": task["task_id"],
                                      "evidence": [f"collection/tasks/{task['task_id']}/{ref}" for ref in (snap["xml"], snap["screenshot"]) if ref]}
        sources = {c["case_id"]: c for c in task["sources"]}
        for observation in decision.observations:
            source = sources.get(observation.case_id)
            if not source or not any(observation.source_basis in str(source.get(k, "")) for k in ("precondition", "steps")):
                raise ValueError("准备条件未引用用例原文，不采用新增限制")
            supported = bool(observation.node_ids and observation.expected_text)
            for nid in observation.node_ids:
                node = nodes.get(nid)
                if not node:
                    raise ValueError("准备核验引用不存在的节点")
                supported = supported and observation.expected_text in " ".join(node.get(k, "") for k in ("text", "content-desc"))
            status = "已观察（执行前仍需复查）" if observation.status == "observed" and supported else "需外部条件" if observation.status == "external" else "尚未核实"
            task["readiness"] = [r for r in task["readiness"] if not (r["case_id"] == observation.case_id and r["condition"] == observation.condition)]
            task["readiness"].append({**observation.model_dump(), "state": status, "snapshot_id": snap["snapshot_id"],
                                      "device_udid": task["device_udid"], "verified_at": now()})

    def _perform(self, task, action, snap, driver, stop) -> dict:
        if stop.is_set():
            return {"stopped": True}
        if hashlib.sha256(driver.page_source.encode()).hexdigest() != snap["raw_hash"]:
            raise ValueError("页面在 AI 分析期间已变化，请重新观察，未执行动作")
        nodes = {n["node_id"]: n for n in snap["nodes"]}
        node = nodes.get(action.node_id) if action.node_id else None
        if action.node_id and node is None:
            raise ValueError("动作引用不存在的节点")
        if node and node.get("password") == "true":
            raise ValueError("凭据输入需由用户完成")
        if action.type in {"wait", "stop"}:
            if action.type == "wait":
                stop.wait(action.seconds)
            return {"type": action.type, "effect": "navigate"}
        label = " ".join(node.get(k, "") for k in ("text", "content-desc")) if node else action.visual_hint
        effect = action.effect
        for words, detected in [(('注销',), 'cancel_account'), (('支付', '购买', '开通会员'), 'purchase'),
                                (('退出登录', '退出账号'), 'logout'), (('分享', '发送', '转发'), 'share'),
                                (('删除', '移除'), 'delete'), (('保存', '提交'), 'create')]:
            if action.type == "tap" and any(word in label for word in words):
                effect = detected
                break
        if effect != "navigate" and effect not in task["request"]["authorized_effects"]:
            raise ValueError(f"动作需要额外授权：{effect}")
        if effect == "delete":
            objects = [o for o in task['objects'] if o['name'] == action.object_name]
            visible_names = {o['name'] for o in task['objects'] if any(o['name'] in n.get('text', '') for n in snap['nodes'])}
            if not objects or action.object_name not in visible_names or action.object_name not in label:
                raise ValueError("删除目标无法映射到当前页面的本轮已核验对象")
            # shortcut: generic list delete controls cannot prove object ownership; use its detail page.
            if len(visible_names) != 1 or len([n for n in snap['nodes'] if n.get('text') == action.object_name]) != 1:
                raise ValueError("请先进入唯一测试对象详情，不能在列表中推断删除归属")
        if effect == "create" and action.type == 'tap' and task["creation_pending"]:
            raise ValueError("创建结果尚未核实，禁止重复提交")
        if effect == "create" and action.type == 'tap' and not task.get("unique_input_written"):
            raise ValueError("尚未填写本轮唯一名称，禁止提交创建")
        if action.type == "input" and effect == "create" and "${test_name}" not in action.value:
            raise ValueError("新建数据输入必须包含本轮唯一名称")
        if action.type == 'input' and re.search(r'密码|验证码|手机号|手机号码', label):
            raise ValueError("账号及凭据输入需由用户完成")
        if effect == 'create' and action.type == 'tap':
            # Persist intent before a command that may succeed and then time out.
            task['creation_pending'] = True
            task['unique_input_written'] = False
            self.save(task)
        element = None
        if action.type in {"tap", "input"}:
            if node:
                locator = node_locator(node)
                matches = find_locator(driver, locator) if locator else []
                if len(matches) != 1:
                    raise ValueError("目标不唯一，未点击或输入")
                element = matches[0]
            else:
                if action.type == "input":
                    raise ValueError("视觉输入缺少确定的输入框，需补采节点")
                from .visual_repair import VisualRepairService
                from .schemas import PlanStep
                service = VisualRepairService.from_environment()
                if not service or not snap["screenshot"]:
                    raise ValueError("缺少可用视觉定位配置或安全截图")
                service.timeout_seconds = min(service.timeout_seconds, 30)
                proposal = service.propose(PlanStep(step_id="collection", title=action.visual_hint, action="tap"),
                                           self.task_dir(task["task_id"]) / snap["screenshot"])
                if not service.accepted(proposal):
                    raise ValueError("视觉定位未确认目标")
                if stop.is_set() or hashlib.sha256(driver.page_source.encode()).hexdigest() != snap["raw_hash"]:
                    raise ValueError("视觉分析后现场变化或已停止，未点击")
                size = driver.get_window_size()
                driver.execute_script("mobile: clickGesture", {"x": int(proposal.x * (size['width'] - 1)), "y": int(proposal.y * (size['height'] - 1))})
        if element is not None:
            if action.type == "tap":
                element.click()
            else:
                value = action.value.replace("${test_name}", task["test_name"])
                if re.search(r"密码|验证码", label):
                    raise ValueError("不自动填写密码或验证码")
                element.clear()
                element.send_keys(value)
                if task["test_name"] in value:
                    task["unique_input_written"] = True
        elif action.type == "back":
            driver.back()
        elif action.type == "swipe":
            size = driver.get_window_size()
            fractions = {"up": (.5,.8,.5,.25), "down": (.5,.25,.5,.8), "left": (.8,.5,.2,.5), "right": (.2,.5,.8,.5)}[action.direction]
            driver.swipe(*[int(v * size['width' if i % 2 == 0 else 'height']) for i,v in enumerate(fractions)], 500)
        key = next((c['target_key'] for c in task['controls'] if c['snapshot_id'] == snap['snapshot_id']
                    and c['node_id'] == action.node_id and c['status'] == 'locator_verified'), None)
        return {"type": action.type, "node_id": action.node_id, "target_key": key, "effect": effect,
                "value": "[本轮测试数据]" if action.type == "input" else "", "expected_text": action.expected_text}

    def _publish(self, task) -> None:
        package = task["app_package"]
        data = published_collection(self.root, package)
        targets = dict(data.get("targets", {}))
        for key, value in task["targets"].items():
            if key not in targets or targets[key]['android_locators'] == value['android_locators']:
                targets[key] = value
        pages = list(data.get("pages", []))
        for page in task["pages"]:
            pages = [p for p in pages if not (p['fingerprint'] == page['fingerprint'] and p.get('device_udid') == page['device_udid'])]
            pages.append(page)
        version = uuid4().hex
        data = {"version": version, "app_package": package, "published_at": now(), "targets": targets,
                "pages": pages, "routes": (data.get("routes", []) + task["routes"])[-200:]}
        atomic_json(self.root / "collection" / "versions" / version / "catalog.json", data)
        atomic_json(self.root / "collection" / "current.json", {"version": version})

    def _check_existing(self, task, driver, stop, deadline) -> None:
        """Reuse approved state checks; AI prepares gaps without altering effective plans."""
        from .executor import AppiumPlanExecutor
        from .runtime import _prepare_state
        from .schemas import ExecutionPlan

        class CheckedExecutor(AppiumPlanExecutor):
            def _execute_step(self, step):
                if stop.is_set() or time.monotonic() >= deadline:
                    raise RuntimeError('采集已停止或超时')
                if step.action.value not in {'launch_app', 'tap', 'back', 'swipe', 'wait_for'}:
                    raise ValueError('既有前置检查包含写操作，改由逐步观察准备')
                if step.action.value == 'tap':
                    elements = self._find_all(step.locator, step.target_key)
                    labels = ' '.join((e.text or '') + ' ' + (e.get_attribute('content-desc') or '') for e in elements)
                    if re.search(r'保存|提交|创建|删除|移除|支付|购买|注销|退出登录|分享|发送', labels):
                        raise ValueError('既有前置检查包含业务写入口，未自动执行')
                return super()._execute_step(step)

        for saved in task['existing_preparations']:
            if stop.is_set() or time.monotonic() >= deadline:
                return
            plan = ExecutionPlan.model_validate(saved)
            if not plan.state_contract.requires:
                continue
            plan = plan.model_copy(deep=True)
            for requirement in plan.state_contract.requires:
                requirement.prepare_steps = []
                requirement.prepare_oracles = []
                for item in [*requirement.check_steps, *requirement.check_oracles]:
                    item.timeout_seconds = min(item.timeout_seconds, 10)
            events = []
            evidence = SimpleNamespace(record=events.append)
            executor = CheckedExecutor(driver, package=task['app_package'], event_sink=events.append,
                                       locator_snapshot=plan.locator_snapshot)
            try:
                _prepare_state(plan, executor, evidence)
                state, reason = '计划前置条件已现场核验（执行前仍需复查）', '沿用已有计划的检查与验收；未执行正式用例。'
            except Exception:
                state, reason = '尚未核实', '已有条件检查未完成，不能因此断定数据不存在；交给 AI 补采或说明缺口。'
            snap = self._capture(task, driver)
            checks = task.setdefault('state_checks', [])
            checks.append({'case_id': plan.source.case_id, 'state': state, 'snapshot_id': snap['snapshot_id'],
                           'events': [{k: v for k, v in e.items() if k in {'type', 'step_id', 'oracle_id', 'passed', 'phase', 'domain'}} for e in events]})
            atomic_json(self.task_dir(task['task_id']) / 'state-checks.json', {'checks': checks})
            task['readiness'] = [r for r in task['readiness'] if not (r['case_id'] == plan.source.case_id and r['condition'] == '已有计划前置检查')]
            task['readiness'].append({'case_id': plan.source.case_id, 'condition': '已有计划前置检查',
                                      'state': state, 'reason': reason, 'verified_at': now(),
                                      'xml': snap['xml'], 'screenshot': snap['screenshot']})

    def _run(self, task, config, ai, stop, release) -> None:
        session = None
        started = time.monotonic()
        path = self.task_dir(task["task_id"])
        try:
            from .appium_service import ensure_appium_ready
            from .runtime import AppiumRuntimeSession
            from .device import _adb_path
            import subprocess
            ensure_appium_ready(config.server_url, self.root / "logs")
            if not self.session_factory:
                version = subprocess.run([str(_adb_path()), '-s', config.udid, 'shell', 'dumpsys', 'package', config.package],
                                         capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15, check=True).stdout
                match = re.search(r"versionName=([^\s]+)", version)
                if not match:
                    raise RuntimeError("目标 App 未安装或无法读取版本")
                task['app_version'] = match.group(1)
            session = self.session_factory() if self.session_factory else AppiumRuntimeSession('android', device_udid=config.udid)
            driver = session.prepare()
            if getattr(driver, 'current_package', config.package) not in {config.package, 'com.android.permissioncontroller', 'com.google.android.permissioncontroller'}:
                raise RuntimeError('目标 App 未进入前台，未开始探索')
            self._check_existing(task, driver, stop, started + task['request']['max_seconds'])
            history, visits = [], {}
            for _ in range(task["request"]["max_steps"]):
                remaining = task["request"]["max_seconds"] - (time.monotonic() - started)
                if getattr(driver, 'current_package', config.package) not in {config.package, 'com.android.permissioncontroller', 'com.google.android.permissioncontroller'}:
                    task.update(status='waiting_input', message='已离开目标 App；外部页面需用户确认后继续')
                    task['missing'].append('外部 App 操作不自动继续；请回到目标 App 后恢复。')
                    break
                if stop.is_set():
                    task.update(status="stopped", message="已停止，保留现有证据")
                    break
                if remaining <= 0:
                    task.update(status="partial", message="已达到时间上限，未覆盖范围仍需补充")
                    break
                snap = self._capture(task, driver)
                task['last_snapshot'] = snap['snapshot_id']
                self.save(task)
                visits[snap['fingerprint']] = visits.get(snap['fingerprint'], 0) + 1
                if visits[snap['fingerprint']] > 4:
                    task.update(status='partial', message='相同页面反复出现，停止该分支以避免重复操作')
                    break
                from .locator_catalog import compiler_catalog
                context = {"snapshot_id": snap["snapshot_id"], "nodes": snap['nodes'][:350],
                           "nodes_omitted": max(0, len(snap['nodes']) - 350), "source_cases": task['sources'],
                           "scope": task['request']['scope_text'], "existing_targets": compiler_catalog(),
                           "existing_preparations": task['existing_preparations'], "history": history[-8:],
                           "authorized_effects": task['request']['authorized_effects'], "test_name": '${test_name}',
                           "creation_pending": task['creation_pending'], "known_objects": task['objects']}
                decision = ai.decide(context, path / snap['screenshot'] if snap['screenshot'] else None, timeout=min(30,remaining))
                if stop.is_set():
                    task.update(status='stopped', message='已停止，AI建议未执行')
                    break
                if time.monotonic() - started >= task['request']['max_seconds']:
                    task.update(status='partial', message='模型返回时已达到时间上限，未执行建议动作')
                    break
                self._collect(task, decision, snap, driver)
                task['missing'] = decision.missing
                if decision.action.type == 'stop':
                    external = any(r['state'] == '需外部条件' for r in task['readiness'])
                    task.update(status='waiting_input' if external else 'partial' if task['missing'] or task['sources'] else 'completed',
                                message=decision.action.reason)
                    self.save(task)
                    break
                receipt = self._perform(task, decision.action, snap, driver, stop)
                after = self._capture(task, driver)
                texts = ' '.join(n.get('text','') + ' ' + n.get('content-desc','') for n in after['nodes'])
                before_texts = ' '.join(n.get('text','') + ' ' + n.get('content-desc','') for n in snap['nodes'])
                verified = bool(decision.action.expected_text and decision.action.expected_text in texts
                                and decision.action.expected_text not in before_texts and not receipt.get('stopped'))
                route = {'from':snap['snapshot_id'], 'to':after['snapshot_id'], 'task_id':task['task_id'],
                         'action':receipt, 'verified':verified, 'changed': snap['raw_hash'] != after['raw_hash'],
                         'case_ids':[c['case_id'] for c in task['sources']], 'source_hashes':task['source_hashes'], 'observed_at':now()}
                task['routes'].append(route)
                if verified and decision.action.node_id:
                    for control in task['controls']:
                        if control['snapshot_id'] == snap['snapshot_id'] and control['node_id'] == decision.action.node_id:
                            control['effect_verified'] = True
                history.append({'page':decision.page_name, 'action':receipt, 'verified':verified})
                with (path / 'events.jsonl').open('a', encoding='utf-8') as log:
                    log.write(json.dumps(route, ensure_ascii=False)+'\n')
                task['steps'] += 1
                task['elapsed_seconds'] = round(time.monotonic()-started,1)
                task['message'] = decision.page_name+'：'+decision.action.reason
                self.save(task)
            else:
                task.update(status='partial', message='已达到操作上限，保留已核验资料')
        except Exception as exc:
            message = str(exc)[:300] if isinstance(exc, (ValueError, RuntimeError)) else '设备或模型请求异常'
            task.update(status='failed', message=f'{type(exc).__name__}: {message}；未自动重复动作')
            # Do not persist raw provider responses, credentials or entered text.
            task['missing'].append('请检查模型、设备或前置条件；详细动作及资料见本任务证据。')
        finally:
            try:
                if session:
                    session.close()
                self._publish(task)
            except Exception as exc:
                task.update(status='failed', message=f'发布未完成：{type(exc).__name__}；保留上次有效资料')
            finally:
                task['elapsed_seconds'] = round(time.monotonic()-started,1)
                try:
                    self.save(task)
                finally:
                    release()


def collection_context(source, root: Path, package: str) -> dict:
    data = published_collection(root, package)
    signature = source_hash(source.model_dump())
    pages = [p for p in data.get('pages',[]) if p.get('source_hashes',{}).get(source.case_id) == signature][-8:]
    routes = [r for r in data.get('routes',[]) if r.get('verified') and r.get('source_hashes',{}).get(source.case_id) == signature][-12:]
    return {'scope':'探索资料只辅助导航及准备，不是用例通过证明或新增验收条件',
            'pages':pages, 'routes':routes} if pages or routes else {}
