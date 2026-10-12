from __future__ import annotations

import csv
import json
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .compiler import configured_compiler
from .collection import CollectionInput, CollectionManager, case_preparations, collection_app_sources, collection_library, existing_pages, published_collection, safe_file
from .evidence_context import compilation_evidence
from .appium_service import ensure_appium_ready
from .archive import RunArtifactQueue
from .device import connected_android_devices, device_config_for_platform, platform_status
from .execution_export import build_execution_workbook
from .importer import WorkbookFormatError, import_cases
from .locator_catalog import locator_coverage, resolve_target
from .model_config import (
    ModelEndpointConfig,
    load_plan_gateway_config,
    load_vision_model_config,
    model_config_status,
)
from .model_gateway import (
    auto_select_plan_model,
    discover_models,
    discover_plan_models,
    probe_plan_model,
    probe_vision_model,
)
from .model_settings import ModelConnectionInput, save_model_settings
from .repository import CaseStateRepository, PlanRepository, SourceCaseRepository
from .repair import RepairPatchStore, patched_record, plan_execution_hash, promote_plan_version
from .review import ReviewRepository, ReviewUpdate
from .review_repair import RepairAnalysis, ReviewRepairInput, analyze_review_plan, check_repair_scope, plan_changes, repair_progress, evidence_file
from .run_service import (
    DeviceBusyError,
    DeviceExecutionCoordinator,
    execute_plan_record,
    reusable_plan_runner,
)
from .schemas import DevicePlatform, ExecutionPlan, PlanRecord, PlanStatus, SourceCase, StateMode, ValidationResult
from .stability import (
    EfficiencyBaselineInput,
    StabilityBatchInput,
    StabilityBatchManager,
    StabilityRepository,
    render_stability_report,
)
from .runtime_paths import runtime_data_dir
from .upgrade import UpgradeCandidateRepository
from .state import state_conflicts
from .validator import validate_plan


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
RUNTIME_DIR = runtime_data_dir()


def create_app(database_path: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application):
        if not read_only:
            stability_repository.recover_interrupted()
            collection_manager.recover_interrupted()
        for existing_batch in stability_repository.list_batches():
            report_path = stability_dir / existing_batch["batch_id"] / "report.html"
            if existing_batch["status"] in {"completed", "stopped", "interrupted"} and not report_path.exists():
                detail = stability_repository.get_batch(existing_batch["batch_id"])
                if detail is not None:
                    render_stability_report(detail, stability_dir, BASE_DIR / "templates")
        yield

    application = FastAPI(title="AI 自动化测试平台 Demo", version="0.1.0", lifespan=lifespan)
    read_only = os.environ.get("DEMO_READ_ONLY") == "1"
    application.state.read_only = read_only

    @application.middleware("http")
    async def block_writes_in_read_only_mode(request: Request, call_next):
        if read_only and request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JSONResponse(status_code=403, content={"detail": "旧版 Demo 为只读对比基线，不能修改或执行"})
        return await call_next(request)

    application.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    repository = PlanRepository(database_path or RUNTIME_DIR / "demo.db")
    source_case_repository = SourceCaseRepository(repository.database_path)
    runs_dir = repository.database_path.parent / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = repository.database_path.parent / "archive" / "runs"
    artifact_queue = RunArtifactQueue(
        repository.database_path, runs_dir, archive_dir, BASE_DIR / "templates",
        ai_review_enabled=os.environ.get("AI_SUMMARY_ENABLED") == "1",
    )
    case_state_repository = CaseStateRepository(repository.database_path, runs_dir)
    source_case_repository.sync_from_plans(repository.list())
    migrated_case_ids = set()
    primary_device_udid = ""
    fallback_device_udid = ""

    case_state_lock = threading.RLock()
    case_state_signature: tuple[int, int] | None = None

    def refresh_case_state(*, force: bool = False) -> None:
        nonlocal case_state_signature
        try:
            stat = runs_dir.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = (0, 0)
        if not force and signature == case_state_signature:
            return
        with case_state_lock:
            if not force and signature == case_state_signature:
                return
            source_cases = source_case_repository.list()
            case_state_repository.sync(
                repository.list(), migrated_case_ids, primary_device_udid, fallback_device_udid,
                {case.case_id: case for case in source_cases},
            )
            case_state_signature = signature

    def invalidate_case_state() -> None:
        nonlocal case_state_signature
        with case_state_lock:
            case_state_signature = None

    refresh_case_state()
    application.mount("/runs", StaticFiles(directory=runs_dir), name="runs")
    stability_dir = repository.database_path.parent / "stability"
    stability_dir.mkdir(parents=True, exist_ok=True)
    application.mount(
        "/stability-artifacts", StaticFiles(directory=stability_dir), name="stability-artifacts"
    )
    stability_repository = StabilityRepository(repository.database_path)
    repair_patch_store = RepairPatchStore(repository.database_path)
    upgrade_candidates = UpgradeCandidateRepository(repository.database_path)
    review_repository = ReviewRepository(repository.database_path, runs_dir, BASE_DIR / "templates")
    review_repository.sync_from_runs()
    platform_cache_lock = threading.Lock()
    platform_cache: dict[str, object] = {"expires_at": 0.0, "payload": None}

    def execution_platform_status() -> dict:
        with platform_cache_lock:
            if time.monotonic() < float(platform_cache["expires_at"]):
                return dict(platform_cache["payload"] or {})
        coverage = locator_coverage()
        # Keep the legacy iOS status in the API for stored historical data;
        # current UI and execution controls expose Android only.
        platforms = platform_status()
        for item in platforms:
            platform_coverage = coverage[item["platform"]]
            item["locator_ready"] = platform_coverage["configured_targets"] > 0
            item["executable"] = item["configured"] and item["locator_ready"]
            if item["configured"] and not item["locator_ready"]:
                item["message"] = "设备参数已配置，等待采集真实页面并建立定位库"
        devices = list(platforms[0].get("devices", [])) if platforms else []
        # Keep historical device data readable even when that device is not
        # currently connected.  Execution still validates the live ADB device
        # through ``require_execution_platform`` before it can start.
        known_udids = {str(item.get("udid") or "") for item in devices}
        for _, result in review_repository.run_results():
            device = result.get("device") or {}
            udid = str(device.get("udid") or "")
            # Ignore malformed legacy records (for example a numeric step
            # marker accidentally written as a device id).
            if len(udid) < 8 or udid in known_udids:
                continue
            known_udids.add(udid)
            devices.append(
                {
                    "udid": udid,
                    "status": "historical",
                    "authorized": False,
                    "brand": device.get("brand", ""),
                    "model": device.get("model", ""),
                    "product": device.get("product", ""),
                    "device": device.get("device", ""),
                    "android_version": device.get("android_version", ""),
                    "sdk": device.get("sdk", ""),
                    "label": f"{device.get('name') or udid}（历史数据，未连接）",
                }
            )
        payload = {"platforms": platforms, "devices": devices, "locator_coverage": coverage}
        with platform_cache_lock:
            platform_cache["payload"] = payload
            platform_cache["expires_at"] = time.monotonic() + 3.0
        return dict(payload)

    def require_execution_platform(platform: DevicePlatform, device_udid: str | None = None) -> None:
        if device_udid is None:
            config = device_config_for_platform(platform)
        else:
            config = device_config_for_platform(platform, device_udid=device_udid)
        coverage = locator_coverage()[platform.value]
        if coverage["configured_targets"] == 0:
            raise RuntimeError(
                f"{platform.value} 定位库尚未建立：需要连接真实设备并采集页面结构后才能执行"
            )
        if platform == DevicePlatform.ANDROID:
            ensure_appium_ready(config.server_url, repository.database_path.parent / "logs")

    device_coordinator = DeviceExecutionCoordinator(
        lambda record, platform, device_udid=None: execute_plan_record(
            record, runs_dir, project_root=BASE_DIR.parent, platform=platform, device_udid=device_udid
        )
    )
    def on_stability_batch_finished(detail: dict) -> None:
        for candidate in upgrade_candidates.list():
            if candidate.get("stability_batch_id") != detail.get("batch_id") or candidate.get("status") != "awaiting_stability":
                continue
            record_id = candidate.get("candidate_record_id")
            attempts = [item for item in detail.get("attempts", []) if item.get("plan_record_id") == record_id]
            required = int(detail.get("repetitions") or 0)
            passed = (
                detail.get("status") == "completed"
                and required > 0
                and required >= candidate["required_repetitions"]
                and len(attempts) == required
                and {int(item.get("iteration") or 0) for item in attempts} == set(range(1, required + 1))
                and all(item.get("status") == "completed" and item.get("classification") in {
                    "NORMAL_PASS", "AI_REPAIR_SUCCESS", "AI_VISUAL_ORACLE_PASS", "ENVIRONMENT_RECOVERED_PASS"
                } for item in attempts)
            )
            if passed:
                record = repository.get(int(record_id)) if record_id is not None else None
                if record is None or record.plan.status != PlanStatus.CANDIDATE:
                    passed = False
                elif plan_execution_hash(record.plan) != plan_execution_hash(candidate["plan"]):
                    passed = False
            if passed:
                formal_source = formal_plan_records().get(candidate["case_id"])
                current_source = repository.get(int(candidate["source_record_id"]))
                if (
                    formal_source is None
                    or formal_source.record_id != candidate["source_record_id"]
                    or current_source is None
                    or plan_execution_hash(current_source.plan) != candidate["source_plan_hash"]
                    or not repair_source_is_current(current_source, candidate["source_plan_hash"])
                ):
                    passed = False
            if passed:
                activated_plan = record.plan.model_copy(deep=True)
                activated_plan.status = PlanStatus.VALIDATED
                passed = upgrade_candidates.activate_transactionally(
                    candidate["candidate_id"], detail["batch_id"],
                    source_plan_hash=plan_execution_hash(current_source.plan),
                    candidate_content_hash=plan_execution_hash(record.plan),
                    validated_plan_json=activated_plan.model_dump_json(),
                )
                if passed:
                    invalidate_case_state()
                    if candidate["source_run_id"]:
                        review_repository.update(candidate["source_run_id"], ReviewUpdate(
                            review_outcome="REPAIR_VERIFIED", reviewer=candidate["reviewer"],
                            review_note=f"修复计划 v{record.plan.version} 首轮及 {required} 次稳定性验证通过，已启用",
                        ))
            if not passed:
                upgrade_candidates.finish_candidate(
                    candidate["candidate_id"], detail["batch_id"], passed=False,
                    error="稳定性未达全部有效轮次通过条件，或候选来源版本已变化",
                )

    def environment_device_recovered(platform: DevicePlatform, device_udid: str | None) -> bool:
        if platform != DevicePlatform.ANDROID or not device_udid:
            return False
        return any(
            item.get("udid") == device_udid and item.get("status") == "device"
            for item in connected_android_devices()
        )

    batch_manager = StabilityBatchManager(
        stability_repository,
        repository,
        device_coordinator.run,
        lambda detail: render_stability_report(detail, stability_dir, BASE_DIR / "templates"),
        repair_patch_store,
        lambda platform, device_udid: device_coordinator.reserved_batch(
            lambda: reusable_plan_runner(
                runs_dir,
                platform=platform,
                device_udid=device_udid,
                on_result_saved=lambda run_id: artifact_queue.enqueue("report", run_id),
            )
        ),
        device_coordinator.reserve,
        on_stability_batch_finished,
        environment_device_recovered,
    )
    collection_manager = CollectionManager(repository.database_path.parent, device_coordinator)
    @application.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"compiler_mode": type(configured_compiler()).__name__},
        )

    @application.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request):
        return templates.TemplateResponse(request=request, name="dashboard.html", context={})

    @application.get("/stability", response_class=HTMLResponse)
    def stability_workbench(request: Request):
        return templates.TemplateResponse(request=request, name="stability.html", context={})

    @application.get("/reviews", response_class=HTMLResponse)
    def review_workbench(request: Request):
        return templates.TemplateResponse(request=request, name="reviews.html", context={})

    @application.get("/history", response_class=HTMLResponse)
    def history_workbench(request: Request):
        return templates.TemplateResponse(request=request, name="history.html", context={})

    @application.get("/models", response_class=HTMLResponse)
    def model_workbench(request: Request):
        return templates.TemplateResponse(request=request, name="models.html", context={})

    @application.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "component": "demo-platform"}

    @application.get("/api/device-platforms")
    def get_device_platforms() -> dict:
        return execution_platform_status()

    @application.get("/api/model-config")
    def get_model_config() -> dict:
        """Expose model readiness without ever returning credentials."""
        return model_config_status()

    def connection_for(role: str, payload: ModelConnectionInput) -> ModelEndpointConfig:
        current = load_plan_gateway_config() if role == "plan" else load_vision_model_config()
        api_key = payload.api_key.strip() or (current.api_key if current else "")
        if not api_key:
            raise HTTPException(status_code=400, detail="请填写 API Key")
        return ModelEndpointConfig(
            base_url=payload.base_url,
            api_key=api_key,
            model=payload.model.strip(),
            source="workbench_preview",
        )

    @application.put("/api/model-settings/{role}")
    def update_model_settings(role: str, payload: ModelConnectionInput) -> dict:
        if role not in {"plan", "vision"}:
            raise HTTPException(status_code=404, detail="未知模型职责")
        if not payload.model.strip():
            raise HTTPException(status_code=400, detail="请选择或填写模型名称")
        config = connection_for(role, payload)
        settings_file = repository.database_path.parent / "model-settings.json"
        save_model_settings(
            role,
            payload.model_copy(update={"api_key": config.api_key}),
            settings_file,
        )
        if role == "plan":
            os.environ.update(
                {
                    "PLAN_MODEL_BASE_URL": config.base_url,
                    "PLAN_MODEL_API_KEY": config.api_key,
                    "PLAN_MODEL_NAME": config.model,
                }
            )
        else:
            os.environ.update(
                {
                    "VISION_MODEL_BASE_URL": config.base_url,
                    "VISION_MODEL_API_KEY": config.api_key,
                    "VISION_MODEL_NAME": config.model,
                }
            )
        return {"saved": True, "role": role, "settings": model_config_status()[f"{role}_model"]}

    @application.post("/api/model-settings/{role}/discover")
    def discover_role_models(role: str, payload: ModelConnectionInput) -> dict:
        if role not in {"plan", "vision"}:
            raise HTTPException(status_code=404, detail="未知模型职责")
        config = connection_for(role, payload)
        try:
            models = discover_plan_models(config) if role == "plan" else discover_models(config)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"读取模型列表失败：{exc}") from exc
        return {"models": models, "count": len(models)}

    @application.post("/api/model-settings/{role}/probe")
    def probe_role_model(role: str, payload: ModelConnectionInput) -> dict:
        if role not in {"plan", "vision"}:
            raise HTTPException(status_code=404, detail="未知模型职责")
        if not payload.model.strip():
            raise HTTPException(status_code=400, detail="请先选择或填写模型名称")
        config = connection_for(role, payload)
        result = (
            probe_plan_model(payload.model, config)
            if role == "plan"
            else probe_vision_model(payload.model, config)
        )
        return result.as_dict()

    @application.get("/api/plan-models")
    def get_plan_models() -> dict:
        try:
            models = discover_plan_models()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"读取中转站模型失败：{exc}") from exc
        return {"models": models, "count": len(models)}

    @application.post("/api/plan-models/{model}/probe")
    def test_plan_model(model: str) -> dict:
        result = probe_plan_model(model)
        return result.as_dict()

    @application.post("/api/plan-models/auto-select")
    def select_plan_model_automatically() -> dict:
        selection_file = repository.database_path.parent / "model-selection.json"
        try:
            selected, probes, candidates = auto_select_plan_model(selection_file=selection_file)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"自动识别解析模型失败：{exc}") from exc
        if selected:
            # Make the new selection effective immediately in this process.
            os.environ["PLAN_MODEL_NAME"] = selected
        return {
            "selected_model": selected,
            "candidate_count": len(candidates),
            "probes": [probe.as_dict() for probe in probes],
        }

    @application.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @application.get("/collection", response_class=HTMLResponse)
    def collection_page(request: Request):
        return templates.TemplateResponse(request, "collection.html", {"read_only": read_only})

    @application.get("/api/collection/tasks")
    def collection_tasks():
        return collection_manager.list()

    def collection_sources(case_ids: list[str]) -> list[dict]:
        cases = {case.case_id: case for case in source_case_repository.list()}
        if any(cid not in cases for cid in case_ids):
            raise HTTPException(404, "采集范围包含不存在的用例")
        return [cases[cid].model_dump(mode="json") for cid in dict.fromkeys(case_ids)]

    @application.post("/api/collection/tasks")
    def start_collection(payload: CollectionInput):
        _, selected_app = collection_app(payload.app_id)
        if selected_app['root'].resolve() != repository.database_path.parent.resolve() or selected_app['app_package'] != os.getenv('ANDROID_APP_PACKAGE', 'com.example.app1'):
            raise HTTPException(409, '所选 App 不属于当前采集服务，请切回当前 App；未操作手机')
        sources = collection_sources(payload.case_ids)
        selected = {}
        for record in repository.list():
            if record.plan.source.case_id in payload.case_ids and record.validation.valid and record.plan.status.value == 'confirmed':
                previous = selected.get(record.plan.source.case_id)
                if previous is None or record.plan.version > previous.plan.version:
                    selected[record.plan.source.case_id] = record
        plans = [r.plan.model_dump(mode='json') for r in selected.values()
                 if r.plan.source.model_dump(mode='json') in sources]
        try:
            return collection_manager.start(payload, sources, plans=plans)
        except DeviceBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @application.get("/api/collection/tasks/{task_id}")
    def get_collection(task_id: str):
        try:
            return collection_manager.get(task_id)
        except KeyError as exc:
            raise HTTPException(404, "采集任务不存在") from exc

    @application.post("/api/collection/tasks/{task_id}/stop")
    def stop_collection(task_id: str):
        try:
            return collection_manager.stop(task_id)
        except KeyError as exc:
            raise HTTPException(404, "采集任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @application.post("/api/collection/tasks/{task_id}/resume")
    def resume_collection(task_id: str):
        try:
            task = collection_manager.get(task_id)
            return collection_manager.resume(task_id, collection_sources(task["request"]["case_ids"]))
        except KeyError as exc:
            raise HTTPException(404, "采集任务不存在") from exc
        except (DeviceBusyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    def collection_sources_by_app():
        return collection_app_sources(repository.database_path.parent, PROJECT_ROOT, os.getenv('ANDROID_APP_PACKAGE', 'com.example.app1'))

    def collection_app(app_id: str | None):
        sources = collection_sources_by_app()
        if app_id is None:
            app_id = next(key for key, value in sources.items() if value['root'].resolve() == repository.database_path.parent.resolve()
                          and value['app_package'] == os.getenv('ANDROID_APP_PACKAGE', 'com.example.app1'))
        if app_id not in sources:
            raise HTTPException(404, 'App 资料不存在')
        return app_id, sources[app_id]

    @application.get('/api/collection/apps')
    def collection_apps():
        current_id, _ = collection_app(None)
        return {'current_app_id': current_id, 'apps': [{'app_id': key, 'name': value['name'], 'app_package': value['app_package']}
                for key, value in collection_sources_by_app().items()]}

    @application.get("/api/collection/pages")
    def collection_pages(app_id: str | None = None):
        try:
            selected_id, source = collection_app(app_id)
            data = collection_library(source['root'], source['app_package'])
            return {**data, 'app_id': selected_id, 'read_only': read_only}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @application.get('/api/collection/existing-files/{relative:path}')
    def existing_collection_file(relative: str, app_id: str | None = None):
        _, source = collection_app(app_id)
        root = source['root']
        allowed = {ref for page in existing_pages(root) for ref in (page['xml'], page['screenshot']) if ref}
        if relative not in allowed:
            raise HTTPException(404, '既有页面资料不存在')
        try:
            return FileResponse(safe_file(root, relative))
        except FileNotFoundError as exc:
            raise HTTPException(404, '既有页面资料不存在') from exc

    @application.get('/api/collection/preparations')
    def collection_preparations(app_id: str | None = None):
        selected_id, source = collection_app(app_id)
        try:
            library = collection_library(source['root'], source['app_package'])
            return {'app_id': selected_id, 'cases': case_preparations(source['root'], source['app_package'], library)}
        except ValueError as exc:
            raise HTTPException(409, '准备资料读取失败，请检查原文、计划或定位资料') from exc

    @application.get("/api/collection/tasks/{task_id}/files/{relative:path}")
    def collection_file(task_id: str, relative: str):
        try:
            collection_manager.get(task_id)
            path = safe_file(collection_manager.task_dir(task_id), relative)
            if path.suffix.lower() not in {".png", ".xml"}:
                raise FileNotFoundError("此文件不提供公开查看")
            return FileResponse(path)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(404, "采集证据不存在") from exc

    @application.post("/api/import", response_model=list[SourceCase])
    async def import_workbook(file: UploadFile = File(...)):
        filename = file.filename or ""
        if not filename.lower().endswith(".xlsx"):
            raise HTTPException(status_code=400, detail="只支持 .xlsx 文件")
        content = await file.read()
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Excel 文件不能超过 10 MB")
        try:
            cases = import_cases(content)
            source_case_repository.import_cases(cases)
            invalidate_case_state()
            return source_case_repository.list()
        except WorkbookFormatError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/api/cases", response_model=list[SourceCase])
    def list_imported_cases():
        return source_case_repository.list()

    @application.get("/api/cases/{case_id}/revisions")
    def list_case_revisions(case_id: str) -> list[dict]:
        return source_case_repository.revisions(case_id)

    @application.post("/api/compile")
    def compile_case(source: SourceCase) -> dict:
        try:
            context = compilation_evidence(source, runs_dir)
            plan = configured_compiler(execution_context=context).compile(source)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"计划编译失败：{type(exc).__name__}: {exc}") from exc
        validation = validate_plan(plan)
        return {"plan": plan, "validation": validation}

    @application.get("/api/plans", response_model=list[PlanRecord])
    def list_plans():
        return repository.list()

    @application.post("/api/plans", response_model=PlanRecord)
    def save_plan(plan: ExecutionPlan):
        validation = validate_plan(plan)
        try:
            record = repository.save(plan, validation)
            source_case_repository.ensure_source(plan.source)
            invalidate_case_state()
            return record
        except Exception as exc:
            if "UNIQUE constraint" in str(exc):
                raise HTTPException(status_code=409, detail="该计划版本已存在，请增加版本号") from exc
            raise

    @application.post("/api/plans/{record_id}/confirm", response_model=PlanRecord)
    def confirm_plan(record_id: int, qa_name: str = Query(min_length=1, max_length=100)):
        record = repository.get(record_id)
        if record is None:
            raise HTTPException(status_code=404, detail="计划不存在")
        if record.plan.status == PlanStatus.CANDIDATE:
            raise HTTPException(status_code=409, detail="修复候选需完成验证，不能直接确认成正式计划")
        validation = validate_plan(record.plan)
        if not validation.valid:
            raise HTTPException(
                status_code=422,
                detail={"message": "计划校验未通过，不能确认", "issues": validation.model_dump()["issues"]},
            )
        plan = record.plan.model_copy(deep=True)
        plan.status = PlanStatus.CONFIRMED
        plan.qa_confirmed_by = qa_name
        plan.qa_confirmed_at = datetime.now(timezone.utc).isoformat()
        updated = repository.update(record_id, plan, validation)
        invalidate_case_state()
        return updated

    @application.get("/api/runs")
    def list_runs(
        device_udid: str | None = Query(default=None),
        representative: bool = Query(default=False),
        run_id: str | None = Query(default=None),
    ) -> list[dict]:
        results: list[dict] = []
        reviews_by_run = {item["run_id"]: item for item in review_repository.history()}
        cached_results = review_repository.run_results()
        selected_ids: set[str] | None = None
        if representative:
            latest: dict[tuple[str, str], tuple[str, dict]] = {}
            for directory_run_id, result in cached_results:
                device = result.get("device") or {}
                key = (
                    str(result.get("case_id") or directory_run_id),
                    str(device.get("udid") or ""),
                )
                current = latest.get(key)
                sort_key = (str(result.get("finished_at") or result.get("started_at") or ""), directory_run_id)
                current_key = (
                    str(current[1].get("finished_at") or current[1].get("started_at") or ""),
                    current[0],
                ) if current else None
                if current_key is None or sort_key > current_key:
                    latest[key] = (directory_run_id, result)
            selected_ids = {item[0] for item in latest.values()}
            if run_id:
                selected_ids.add(run_id)
        for directory_run_id, result in cached_results:
            if selected_ids is not None and directory_run_id not in selected_ids:
                continue
            payload = dict(result)
            payload["run_id"] = directory_run_id
            payload["report_url"] = f"/runs/{directory_run_id}/report.html"
            payload["review"] = reviews_by_run.get(payload["run_id"])
            if device_udid and str((payload.get("device") or {}).get("udid") or "") != device_udid:
                continue
            results.append(payload)
        return results

    @application.get("/api/execution/summary")
    def execution_summary(device_udid: str | None = Query(default=None)) -> dict:
        rows = execution_case_rows(device_udid=device_udid)
        counts = {status: sum(row["status"] == status for row in rows) for status in ("PASSED", "FAILED", "BLOCKED", "NOT_RUN")}
        return {
            "device_cases": len(rows),
            "passed_cases": counts["PASSED"],
            "failed_cases": counts["FAILED"],
            "blocked_cases": counts["BLOCKED"],
            "not_run_cases": counts["NOT_RUN"],
            "formula": "设备用例数 = 已通过 + 未通过 + 阻塞 + 未执行",
        }

    @application.get("/api/ai-summary/latest")
    def read_latest_ai_summary() -> dict:
        """Read the automatic evidence report; never starts a model call."""
        if os.environ.get("AI_SUMMARY_ENABLED") != "1":
            raise HTTPException(status_code=404, detail="AI 整批总结功能尚未启用")
        path = runs_dir / "ai-latest-summary.md"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="尚无 AI 证据核验报告")
        return {"markdown": path.read_text(encoding="utf-8")}

    def formal_plan_records() -> dict[str, PlanRecord]:
        selected: dict[str, PlanRecord] = {}
        for record in repository.list():
            case_id = record.plan.source.case_id
            if case_id.startswith("CLEANUP-") or record.plan.status not in {
                PlanStatus.CONFIRMED, PlanStatus.VALIDATED
            }:
                continue
            current = selected.get(case_id)
            score = (record.plan.status == PlanStatus.VALIDATED, record.plan.version, record.record_id)
            current_score = (
                (current.plan.status == PlanStatus.VALIDATED, current.plan.version, current.record_id)
                if current else None
            )
            if current is None or score > current_score:
                selected[case_id] = record
        return selected

    def execution_case_rows(device_udid: str | None = None) -> list[dict]:
        refresh_case_state()
        source_by_id = {case.case_id: case for case in source_case_repository.list()}
        states = {
            row["case_id"]: row for row in case_state_repository.list()
        }
        latest_plans = formal_plan_records()
        rows = []
        for case_id, source in source_by_id.items():
            state = states.get(case_id)
            if state is None:
                continue
            status = state["qualification_status"]
            plan = latest_plans.get(case_id)
            if status == "NOT_RUN":
                if plan is not None and plan.plan.source != source:
                    reason = "原始用例已修订，当前计划需重新核对并执行"
                elif plan is not None and state.get("qualification_plan_record_id") != plan.record_id:
                    reason = "当前计划版本尚无首轮执行通过记录"
                else:
                    reason = "尚无首轮执行记录"
            elif status == "BLOCKED":
                reason = state.get("block_reason") or "首轮执行被设备、环境或测试数据条件阻塞"
            elif status == "FAILED":
                reason = "当前计划版本的首轮执行未通过"
            else:
                reason = "当前计划版本首轮执行通过"
            detail = ""
            run_id = state.get("qualification_run_id")
            if run_id:
                result_path = runs_dir / str(run_id) / "result.json"
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    detail = str(result.get("message") or result.get("classification_label") or "")
                except (OSError, json.JSONDecodeError):
                    pass
            rows.append(
                {
                    **source.model_dump(mode="json"),
                    "owner_device_udid": state.get("owner_device_udid"),
                    "owner_device_name": state.get("owner_device_name"),
                    "status": status,
                    "reason": reason,
                    "result_detail": detail,
                    "qualification_run_id": run_id,
                    "qualification_plan_record_id": state.get("qualification_plan_record_id"),
                    "current_plan_record_id": plan.record_id if plan else None,
                }
            )
        # An audited first-run ledger may contain human evidence review outcomes
        # that intentionally differ from immutable raw automation results.
        ledgers = sorted(
            runs_dir.glob("first-run-batch-*/execution-scope.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if ledgers:
            try:
                ledger = json.loads(ledgers[0].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                ledger = {}
            status_map = {
                "passed": "PASSED",
                "failed": "FAILED",
                "blocked_precondition": "BLOCKED",
            }
            reviewed = {
                str(item.get("case_id")): item
                for item in ledger.get("cases", [])
                if item.get("case_id") and item.get("first_run_status") in status_map
            }
            for row in rows:
                item = reviewed.get(row["case_id"])
                if item is None:
                    continue
                # A review is scoped to its original run/version, never a blanket
                # override of future executions or edited source requirements.
                if (item.get("run_id") and item.get("run_id") != row.get("qualification_run_id")):
                    continue
                unexecuted_block = (item["first_run_status"] == "blocked_precondition"
                                    and not item.get("run_id") and not row.get("current_plan_record_id"))
                if (item.get("plan_record_id") and item.get("plan_record_id") != row.get("current_plan_record_id")
                        and not unexecuted_block):
                    continue
                if not item.get("run_id") and item.get("accepted_at") and row.get("qualification_run_id"):
                    try:
                        latest = json.loads((runs_dir / row["qualification_run_id"] / "result.json").read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if str(latest.get("finished_at") or "") > item["accepted_at"]:
                        continue
                row["status"] = status_map[item["first_run_status"]]
                row["reason"] = str(item.get("reason") or row["reason"])
                row["qualification_run_id"] = item.get("run_id") or None
                row["first_run_evidence_run_id"] = (item.get("original_first_run_binding") or {}).get("run_id")
                evidence_path = (runs_dir.parent / str(item.get("evidence") or "")).resolve()
                binding = item.get("original_first_run_binding") or item
                row["first_run_executed"] = (
                    binding.get("verification_method") == "authorized_native_ui_execution"
                    and evidence_path.is_relative_to(runs_dir.parent.resolve()) and evidence_path.is_file()
                )
                row["qualification_plan_record_id"] = item.get("plan_record_id")
                if item.get("owner_device_udid"):
                    row["owner_device_udid"] = item["owner_device_udid"]
                    row["owner_device_name"] = item.get("owner_device_name")
                row["stability_ready"] = item.get("stability_ready", True)
                row["stability_block_reason"] = item.get("stability_block_reason", "")
                row["stability_exemption_reason"] = (
                    str(item.get("stability_exemption_reason") or "")
                    if item.get("planned_stability_repetitions") == 0 else ""
                )
                if item.get("verification_method") == "manual_evidence_review":
                    row["result_detail"] = "最终结论依据独立截图/文件证据人工复核；原始自动化运行结果保留在下方报告中。"
                elif item.get("first_run_status") == "blocked_precondition":
                    row["result_detail"] = "本轮前置条件阻塞，未能完整执行验收。"
                elif item.get("verification_method") == "accepted_first_run_baseline":
                    row["result_detail"] = "沿用已核验的首轮结论；对应新计划已整理，尚未真机复跑。旧报告仅作为首轮证据。"
        return [row for row in rows if device_udid is None or row.get("owner_device_udid") == device_udid]

    @application.get("/api/execution/cases")
    def execution_cases(
        device_udid: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ) -> list[dict]:
        rows = execution_case_rows(device_udid)
        return [row for row in rows if status is None or row["status"] == status]

    @application.get("/api/execution/cases.xlsx")
    def export_execution_cases(
        device_udid: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ) -> Response:
        rows = [
            row for row in execution_case_rows(device_udid)
            if status is None or row["status"] == status
        ]
        return Response(
            build_execution_workbook(rows, runs_dir),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="execution-cases.xlsx"'},
        )

    @application.get("/api/reviews")
    def list_reviews(
        status: str | None = None,
        keyword: str | None = None,
        observed: str | None = None,
        outcome: str | None = None,
        device_udid: str | None = None,
    ) -> list[dict]:
        items = review_repository.list(
            run_ids=current_review_run_ids(device_udid=device_udid),
            status=status,
            keyword=keyword,
            observed=observed,
            outcome=outcome,
            device_udid=device_udid,
        )
        return items

    def current_review_run_ids(device_udid: str | None = None) -> set[str]:
        """Use the same current qualification and stability facts as the dashboard."""
        device_udid = device_udid or None
        effective = {row["case_id"]: row for row in execution_case_rows(device_udid=device_udid)}
        # Only projects without an imported case registry need the legacy fallback.
        legacy = not source_case_repository.list()
        if legacy:
            for state in case_state_repository.list(device_udid=device_udid):
                effective.setdefault(state["case_id"], {"status": state["qualification_status"],
                    "qualification_run_id": state["qualification_run_id"]})
        allowed = set()
        for row in effective.values():
            if row["status"] != "FAILED" and not (legacy and row["status"] != "PASSED"):
                continue
            run_id = row.get("qualification_run_id") or row.get("first_run_evidence_run_id")
            if run_id:
                allowed.add(str(run_id))
        outcomes = stability_repository.latest_case_outcomes(
            device_udid=device_udid,
            group_by_device=device_udid is None,
            exclude_record_ids=unpublished_plan_ids(),
        )
        active_stability = {
            (row["case_id"], row["plan_record_id"])
            for row in stability_case_rows(device_udid) if not row.get("exempt")
        } if not legacy else set()
        allowed.update(
            str(outcome["latest_run_id"])
            for key, outcome in outcomes.items()
            if not outcome["stable"] and outcome.get("latest_run_id")
            and (legacy or (key[0] if isinstance(key, tuple) else key,
                            outcome["plan_record_id"]) in active_stability)
        )
        for run_id in stability_repository.latest_unresolved_interruption_run_ids(device_udid=device_udid):
            review = review_repository.get(run_id)
            if legacy or (review and (review["case_id"], review["plan_record_id"]) in active_stability):
                allowed.add(run_id)
        return allowed

    @application.get("/api/reviews/metrics")
    def review_metrics(device_udid: str | None = Query(default=None)) -> dict:
        items = review_repository.list(device_udid=device_udid,
                                      run_ids=current_review_run_ids(device_udid=device_udid))
        return {
            "total": len(items),
            "pending": sum(item.get("review_status") == "PENDING" for item in items),
            "execution_failures": sum(not item.get("is_stability") for item in items),
            "stability_failures": sum(bool(item.get("is_stability")) for item in items),
        }

    @application.get("/api/reviews/history")
    def review_history(
        keyword: str | None = None,
        device_udid: str | None = None,
    ) -> list[dict]:
        return review_repository.history(keyword=keyword, device_udid=device_udid)

    @application.get("/api/reviews/{run_id}")
    def get_review(run_id: str) -> dict:
        detail = review_repository.detail(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="复核记录不存在")
        if not detail["source_case"]:
            source = next((case for case in source_case_repository.list()
                           if case.case_id == detail["review"]["case_id"]), None)
            if source:
                detail["source_case"] = source.model_dump(mode="json")
        detail["repair_candidates"] = [
            public_candidate(item) for item in upgrade_candidates.list()
            if item["source_run_id"] == run_id
        ]
        analysis_path = runs_dir / run_id / "repair-analysis.json"
        if analysis_path.is_file():
            try:
                detail["latest_repair_analysis"] = json.loads(analysis_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return detail

    def public_candidate(candidate: dict) -> dict:
        public = {**candidate, "plan": candidate["plan"].model_dump(mode="json")}
        result_file = evidence_file(runs_dir, f"{candidate.get('first_run_id')}/result.json")
        if result_file:
            try:
                result = json.loads(result_file.read_text(encoding="utf-8"))
                public["repair_progress"] = result.get("repair_progress")
            except (OSError, json.JSONDecodeError):
                pass
        return public

    def repair_source_is_current(record: PlanRecord, source_hash: str) -> bool:
        formal = formal_plan_records().get(record.plan.source.case_id)
        source = next((item for item in source_case_repository.list() if item.case_id == record.plan.source.case_id), None)
        return (formal is not None and formal.record_id == record.record_id
                and plan_execution_hash(formal.plan) == source_hash
                and (source is None or source == record.plan.source))

    @application.post("/api/reviews/{run_id}/analyze")
    def analyze_failed_run(run_id: str, payload: ReviewRepairInput) -> dict:
        detail = review_repository.detail(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="复核记录不存在")
        review = detail["review"]
        record = repository.get(int(review["plan_record_id"])) if review.get("plan_record_id") else None
        if record is None:
            raise HTTPException(status_code=409, detail="缺少失败运行对应的执行计划，无法生成修复")
        source_hash = plan_execution_hash(record.plan)
        if not repair_source_is_current(record, source_hash):
            raise HTTPException(status_code=409, detail="正式计划或原始用例已更新，请复核当前版本的运行")
        device_udid = review.get("device_udid")
        if not device_udid:
            raise HTTPException(status_code=409, detail="失败运行没有设备归属，无法绑定修复验证")
        try:
            platform = DevicePlatform(str((detail["result"].get("device") or {}).get("platform") or "android").lower())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="失败记录的设备平台无法识别") from exc
        repetitions = 1
        lane = "stability" if review.get("batch_id") else "execution"
        if lane == "stability":
            batch = stability_repository.get_batch(review["batch_id"])
            if batch is None:
                raise HTTPException(status_code=409, detail="缺少原稳定性批次，无法确定验证次数")
            repetitions = batch["repetitions"]
        analysis_plan, analysis_detail, analyzed_run_id = record.plan, detail, run_id
        analyzed_candidate = None
        latest_failure = None
        for candidate in upgrade_candidates.list():
            if (candidate["source_run_id"] != run_id or candidate["source_plan_hash"] != source_hash
                    or candidate["source_record_id"] != record.record_id
                    or candidate["device_udid"] != device_udid or candidate["platform"] != platform.value
                    or candidate["status"] not in {"failed_first_run", "failed_stability"}):
                continue
            failed_runs = [candidate.get("first_run_id")]
            if candidate.get("stability_batch_id"):
                candidate_batch = stability_repository.get_batch(candidate["stability_batch_id"])
                failed_runs.extend(item.get("run_id") for item in (candidate_batch or {}).get("attempts", [])
                                   if item.get("status") == "completed")
            for failed_run in filter(None, failed_runs):
                if Path(failed_run).name != failed_run:
                    continue
                path = evidence_file(runs_dir, f"{failed_run}/result.json")
                if path is None:
                    continue
                try:
                    result = json.loads(path.read_text(encoding="utf-8"))
                    finished = datetime.fromisoformat(result["finished_at"].replace("Z", "+00:00")).timestamp()
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                if (result.get("status") != "failed" or result.get("case_id") != record.plan.source.case_id
                        or result.get("plan_record_id") != candidate.get("candidate_record_id")
                        or result.get("upgrade_candidate_id", candidate["candidate_id"]) != candidate["candidate_id"]
                        or (result.get("device") or {}).get("udid") != device_udid
                        or str((result.get("device") or {}).get("platform", "")).lower() != platform.value):
                    continue
                if latest_failure is None or finished > latest_failure:
                    latest_failure = finished
                    analyzed_candidate, analyzed_run_id = candidate, failed_run
                    analysis_plan = candidate["plan"]
                    analysis_detail = {"result": result, "execution_plan": analysis_plan.model_dump(mode="json"),
                        "previous_repair": {"candidate_id": candidate["candidate_id"],
                            "analysis": candidate["analysis"], "repair_progress": result.get("repair_progress")}}
        replay_marker_path = runs_dir / run_id / "repair-evidence-replay.json"
        if replay_marker_path.is_file() and analyzed_candidate is None:
            analysis = RepairAnalysis(summary="已有一次补跑证据，直接分析新现场", needs_evidence_rerun=True)
        else:
            try:
                analysis = RepairAnalysis.model_validate(analyze_review_plan(analysis_plan, analysis_detail, runs_dir / analyzed_run_id, payload.note))
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"AI 分析未完成：{type(exc).__name__}；请检查模型配置后重试") from exc
        failure_events = [event for event in analysis_detail["result"].get("events") or []
                          if event.get("type") == "step_failed" or (
                              event.get("type") == "oracle_result" and event.get("passed") is False)]
        if (analysis.plan is None and failure_events and not analysis.missing_requirements
                and not any(event.get("page_source") for event in failure_events)):
            analysis.needs_evidence_rerun = True
        evidence_rerun = None
        if analyzed_candidate is not None and analysis.needs_evidence_rerun:
            analysis.needs_evidence_rerun = False
            analysis.cannot_repair_reason = (analysis.cannot_repair_reason or "最新候选现场证据不足") + "；请用候选计划验证补采现场，不自动重跑旧正式计划。"
        if analysis.plan is None and analysis.needs_evidence_rerun:
            if record.plan.state_contract.mode not in {StateMode.READ_ONLY, StateMode.IDEMPOTENT_WRITE}:
                analysis.cannot_repair_reason = (
                    (analysis.cannot_repair_reason or "现有证据不足")
                    + "；该计划可能重复创建或删除数据，请使用页面上的“按原计划重跑”补采证据。"
                )
            else:
                marker_path = replay_marker_path
                replay_result = None
                if marker_path.is_file():
                    try:
                        marker = json.loads(marker_path.read_text(encoding="utf-8"))
                        replay_id = str(marker.get("run_id") or "")
                        result_path = (evidence_file(runs_dir, f"{replay_id}/result.json")
                                       if replay_id.startswith("evidence-") and Path(replay_id).name == replay_id else None)
                        if result_path:
                            replay_result = json.loads(result_path.read_text(encoding="utf-8"))
                            if (replay_result.get("case_id") == record.plan.source.case_id
                                    and str((replay_result.get("device") or {}).get("udid") or "") == device_udid):
                                replay_result["run_id"] = replay_id
                            else:
                                replay_result = None
                    except (OSError, ValueError, TypeError):
                        replay_result = None
                    if replay_result is None:
                        analysis.cannot_repair_reason = "本次补证据复跑已尝试但没有完整结果；请查看运行记录或手动复测。"
                        analysis.needs_evidence_rerun = False
                else:
                    try:
                        if batch_manager.active_batch_id is not None:
                            raise DeviceBusyError("稳定性批次正在占用设备")
                        require_execution_platform(platform, device_udid)
                        release_device = device_coordinator.reserve()
                    except RuntimeError as exc:
                        analysis.cannot_repair_reason = f"补证据复跑暂未开始：{exc}。设备就绪后可重新分析。"
                        analysis.retryable = True
                    else:
                        try:
                            marker_path.write_text(json.dumps({"status": "started"}), encoding="utf-8")
                            replay_result = execute_plan_record(
                                record, runs_dir, project_root=BASE_DIR.parent,
                                platform=platform, device_udid=device_udid, evidence_only=True,
                            )
                            marker_path.write_text(json.dumps({"status": "completed", "run_id": replay_result["run_id"]}), encoding="utf-8")
                        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                            marker_path.write_text(json.dumps({"status": "failed", "error": type(exc).__name__}), encoding="utf-8")
                            analysis.cannot_repair_reason = f"补证据复跑未完成（{type(exc).__name__}），没有自动重复执行；可查看运行记录。"
                            analysis.needs_evidence_rerun = False
                        finally:
                            release_device()
                        if replay_result is not None:
                            artifact_queue.enqueue("report", replay_result["run_id"])
                            review_repository.add_rerun(run_id, replay_result)
                if replay_result is not None:
                    replay_id = replay_result["run_id"]
                    evidence_rerun = {"run_id": replay_id, "status": replay_result.get("status"),
                                      "report_url": f"/runs/{replay_id}/report.html"}
                    if replay_result.get("status") == "passed":
                        analysis = RepairAnalysis(summary="按原计划补跑已通过；已保存新证据，本次无需修改计划。",
                            cannot_repair_reason="本次只为补证据，不把补跑结果自动改成首轮通过。")
                    else:
                        try:
                            replay_detail = {"result": replay_result, "execution_plan": record.plan.model_dump(mode="json")}
                            analysis = RepairAnalysis.model_validate(analyze_review_plan(
                                record.plan, replay_detail, runs_dir / replay_id, payload.note))
                            if analysis.needs_evidence_rerun:
                                analysis.needs_evidence_rerun = False
                                analysis.cannot_repair_reason = (
                                    (analysis.cannot_repair_reason or "补跑后仍无法确定修改方式")
                                    + "；已补跑一次，本次不再自动重复。"
                                )
                        except Exception as exc:
                            analysis = RepairAnalysis(summary="新证据已保存，但二次分析未完成", retryable=True,
                                cannot_repair_reason=f"二次分析失败（{type(exc).__name__}）；可稍后重新分析已保存的补跑证据。")
        analysis.analyzed_run_id = evidence_rerun["run_id"] if evidence_rerun else analyzed_run_id
        analysis.analyzed_candidate_id = analyzed_candidate["candidate_id"] if analyzed_candidate else ""
        if analysis.plan is not None and analyzed_candidate:
            reasons = {**analyzed_candidate["analysis"].get("removed_steps", {}), **analysis.removed_steps}
            remaining_ids = {step.step_id for step in analysis.plan.steps}
            analysis.removed_steps = {step.step_id: reasons[step.step_id] for step in record.plan.steps
                                     if step.step_id not in remaining_ids and step.step_id in reasons}
        def analysis_response(candidate=None, issues=None):
            response = {"analysis": candidate["analysis"] if candidate else analysis.model_dump(exclude={"plan"}), "candidate": candidate,
                        "issues": issues or [], "evidence_rerun": evidence_rerun,
                        "analyzed_at": datetime.now(timezone.utc).isoformat()}
            # Keep unsuccessful analysis too, so refresh does not expose only an old draft.
            (runs_dir / run_id / "repair-analysis.json").write_text(
                json.dumps({key: value for key, value in response.items() if key != "candidate"}, ensure_ascii=False),
                encoding="utf-8")
            return response
        if analysis.plan is None:
            return analysis_response()
        plan = analysis.plan
        try:
            check_repair_scope(record.plan, plan, analysis.removed_steps)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not repair_source_is_current(record, source_hash):
            raise HTTPException(status_code=409, detail="分析期间正式计划或原文已变化，请重新分析")
        plan.status = PlanStatus.CANDIDATE
        plan.compiler_mode = "ai_review_repair"
        plan.qa_confirmed_by = plan.qa_confirmed_at = None
        validation = validate_plan(plan, baseline_plan=record.plan)
        if not validation.valid:
            return analysis_response(issues=[item.message for item in validation.issues])
        changes = plan_changes(record.plan, plan)
        if not changes:
            return analysis_response(issues=["没有可验证的步骤或前置数据改动"])
        plan.version = max(
            [item.plan.version for item in repository.list() if item.plan.plan_id == plan.plan_id]
            + [item["plan"].version for item in upgrade_candidates.list() if item["plan"].plan_id == plan.plan_id]
        ) + 1
        plan.locator_snapshot = {
            key: list(resolve_target(key, platform))
            for key in sorted({item.target_key for item in [*plan.steps, *plan.oracles] if item.target_key})
        }
        try:
            candidate = upgrade_candidates.create(
                case_id=plan.source.case_id, source_record_id=record.record_id,
                device_udid=device_udid, platform=platform.value, reviewer=payload.reviewer,
                plan=plan, content_hash=plan_execution_hash(plan), source_plan_hash=source_hash,
                source_run_id=run_id, validation_lane=lane, required_repetitions=repetitions,
                analysis={**analysis.model_dump(exclude={"plan"}), "changes": changes},
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return analysis_response(public_candidate(candidate))

    @application.put("/api/reviews/{run_id}")
    def update_review(run_id: str, payload: ReviewUpdate) -> dict:
        review = review_repository.update(run_id, payload)
        if review is None:
            raise HTTPException(status_code=404, detail="复核记录不存在")
        return review

    @application.post("/api/reviews/{run_id}/rerun")
    def rerun_review(
        run_id: str,
        platform: DevicePlatform = Query(default=DevicePlatform.ANDROID),
        device_udid: str | None = Query(default=None),
    ) -> dict:
        review = review_repository.get(run_id)
        if review is None:
            raise HTTPException(status_code=404, detail="复核记录不存在")
        record_id = review.get("plan_record_id")
        record = repository.get(int(record_id)) if record_id is not None else None
        if record is None:
            raise HTTPException(status_code=409, detail="原始运行无法关联可执行计划，请先补齐计划记录")
        if batch_manager.active_batch_id is not None:
            raise HTTPException(status_code=409, detail="稳定性验证批次正在占用测试设备")
        try:
            require_execution_platform(platform, device_udid)
            result = device_coordinator.run(record, platform, device_udid)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail="真机复测超过 5 分钟，已终止等待") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        artifact_queue.enqueue("report", str(result.get("run_id") or ""))
        review_repository.add_rerun(run_id, result)
        refresh_case_state(force=True)
        return result

    def validatable_plan_records(device_udid: str | None = None) -> list:
        refresh_case_state()
        source_cases = {case.case_id: case for case in source_case_repository.list()}
        latest_by_case = {
            case_id: record for case_id, record in formal_plan_records().items()
            if record.validation.valid
        }
        state_rows = {
            row["case_id"]: row
            for row in execution_case_rows(device_udid)
            if row["status"] == "PASSED"
        }
        latest_by_case = {
            case_id: record
            for case_id, record in latest_by_case.items()
            if case_id in state_rows
            and state_rows[case_id].get("owner_device_udid")
            and state_rows[case_id].get("qualification_plan_record_id") == record.record_id
            and source_cases.get(case_id) == record.plan.source
        }
        return [latest_by_case[case_id] for case_id in sorted(latest_by_case)]

    def stability_case_rows(device_udid: str | None = None) -> list[dict]:
        qualification = {row["case_id"]: row for row in execution_case_rows(device_udid)}
        excluded = unpublished_plan_ids()
        outcomes = stability_repository.latest_case_outcomes(device_udid=device_udid, exclude_record_ids=excluded)
        if not device_udid:
            grouped = {}
            device_outcomes = stability_repository.latest_case_outcomes(group_by_device=True, exclude_record_ids=excluded)
            for (case_id, _device), outcome in device_outcomes.items():
                if not _device:
                    continue
                grouped.setdefault(case_id, []).append(outcome)
            outcomes.update(
                {
                    case_id: _merge_case_outcomes(case_outcomes)
                    for case_id, case_outcomes in grouped.items()
                    if len(case_outcomes) > 1
                }
            )
        rows = []
        for record in validatable_plan_records(device_udid=device_udid):
            case_id = record.plan.source.case_id
            outcome = outcomes.get(case_id)
            if outcome and outcome.get("plan_record_id") != record.record_id:
                outcome = None
            entered = outcome is not None
            complete = bool(outcome and outcome["complete"])
            stable = bool(outcome and outcome["stable"])
            exemption_reason = qualification[case_id].get("stability_exemption_reason", "")
            rows.append(
                {
                    "case_id": case_id,
                    "title": record.plan.source.title,
                    "plan_record_id": record.record_id,
                    "stability_ready": qualification[case_id].get("stability_ready", True),
                    "stability_block_reason": qualification[case_id].get("stability_block_reason", ""),
                    "entered": entered,
                    "complete": complete,
                    "stable": stable,
                    "exempt": bool(exemption_reason),
                    "exemption_reason": exemption_reason,
                    "issue": not stable and not exemption_reason,
                    "pending": not stable and not exemption_reason,
                    "completed_attempts": outcome["completed_attempts"] if outcome else 0,
                    "required_attempts": outcome["required_attempts"] if outcome else 0,
                    "normal_passes": outcome["normal_passes"] if outcome else 0,
                    "acceptance_passes": outcome["acceptance_passes"] if outcome else 0,
                    "replay_complete": outcome["replay_complete"] if outcome else False,
                    "issue_categories": outcome["issue_categories"] if outcome else [],
                    "latest_batch_id": outcome["batch_id"] if outcome else None,
                    "latest_batch_name": outcome["batch_name"] if outcome else None,
                    "latest_run_id": outcome["latest_run_id"] if outcome else None,
                    "latest_finished_at": outcome["latest_finished_at"] if outcome else None,
                }
            )
        return rows

    def unpublished_plan_ids() -> set[int]:
        return {record.record_id for record in repository.list() if record.plan.status == PlanStatus.CANDIDATE}

    def _merge_case_outcomes(outcomes: list[dict]) -> dict:
        latest = max(outcomes, key=lambda item: item.get("latest_finished_at") or "")
        return {
            **latest,
            "complete": all(item["complete"] for item in outcomes),
            "stable": all(item["stable"] for item in outcomes),
            "replay_complete": all(item["replay_complete"] for item in outcomes),
            "completed_attempts": sum(item["completed_attempts"] for item in outcomes),
            "required_attempts": sum(item["required_attempts"] for item in outcomes),
            "normal_passes": sum(item["normal_passes"] for item in outcomes),
            "acceptance_passes": sum(item["acceptance_passes"] for item in outcomes),
            "issue_categories": sorted({issue for item in outcomes for issue in item["issue_categories"]}),
        }

    def filter_stability_case_rows(scope: str, device_udid: str | None = None) -> list[dict]:
        rows = stability_case_rows(device_udid=device_udid)
        if scope == "completed":
            return [row for row in rows if row["complete"]]
        if scope == "stable":
            return [row for row in rows if row["stable"]]
        if scope == "issues":
            return [row for row in rows if row["pending"]]
        return rows

    @application.get("/api/stability/plans")
    def list_stability_plans(device_udid: str | None = Query(default=None)) -> list[dict]:
        qualification = {row["case_id"]: row for row in execution_case_rows(device_udid)}
        return [
            {
                "record_id": record.record_id,
                "case_id": record.plan.source.case_id,
                "title": record.plan.source.title,
                "version": record.plan.version,
                "status": record.plan.status,
                "compiler_mode": record.plan.compiler_mode,
                "stability_ready": qualification[record.plan.source.case_id].get("stability_ready", True)
                    and not qualification[record.plan.source.case_id].get("stability_exemption_reason"),
                "stability_block_reason": qualification[record.plan.source.case_id].get("stability_exemption_reason")
                    or qualification[record.plan.source.case_id].get("stability_block_reason", ""),
                "exemption_reason": qualification[record.plan.source.case_id].get("stability_exemption_reason", ""),
            }
            for record in validatable_plan_records(device_udid=device_udid)
        ]

    @application.get("/api/stability/summary")
    def get_stability_summary(device_udid: str | None = Query(default=None)) -> dict:
        rows = stability_case_rows(device_udid=device_udid)
        validatable_cases = len(rows)
        completed_cases = sum(row["complete"] for row in rows)
        stable_cases = sum(row["stable"] for row in rows)
        issue_cases = sum(row["issue"] for row in rows)
        pass_rate = round(stable_cases / completed_cases * 100, 1) if completed_cases else 0.0
        return {
            "validatable_cases": validatable_cases,
            "completed_cases": completed_cases,
            "stable_cases": stable_cases,
            "issue_cases": issue_cases,
            "exempt_cases": sum(row["exempt"] for row in rows),
            "pass_rate": pass_rate,
        }

    @application.get("/api/stability/cases")
    def list_stability_cases(scope: str = Query(default="validatable", pattern="^(validatable|completed|stable|issues)$"), device_udid: str | None = Query(default=None)) -> list[dict]:
        return filter_stability_case_rows(scope, device_udid=device_udid)

    @application.get("/api/stability/cases/export.csv")
    def export_stability_cases(scope: str = Query(default="validatable", pattern="^(validatable|completed|stable|issues)$"), device_udid: str | None = Query(default=None)) -> Response:
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["用例编号", "用例名称", "状态", "已完成轮次", "验收通过轮次", "要求轮次", "问题分类", "最新批次", "最近执行时间", "执行报告"])
        for row in filter_stability_case_rows(scope, device_udid=device_udid):
            status = "免稳定性验证" if row["exempt"] else "稳定通过" if row["stable"] else "已完成待复核" if row["complete"] else "验证未完成" if row["entered"] else "待开始稳定性验证"
            report_url = f"/dashboard?run={row['latest_run_id']}" if row["latest_run_id"] else ""
            writer.writerow([
                row["case_id"], row["title"], status, row["completed_attempts"], row["acceptance_passes"],
                row["required_attempts"], "、".join(row["issue_categories"]), row["latest_batch_name"] or "",
                row["latest_finished_at"] or "", report_url,
            ])
        return Response(
            "\ufeff" + output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="stability-cases.csv"'},
        )

    @application.get("/api/stability/batches")
    def list_stability_batches() -> list[dict]:
        return stability_repository.list_batches()

    @application.get("/api/repair-patches")
    def list_repair_patches(
        case_id: str,
        device_udid: str,
        platform: DevicePlatform = Query(default=DevicePlatform.ANDROID),
    ) -> list[dict]:
        return [
            {
                **patch.__dict__,
                "locator": patch.locator.model_dump(mode="json"),
            }
            for patch in repair_patch_store.list_for_case(
                case_id, device_udid, platform.value, include_verified=True
            )
        ]

    @application.post("/api/plans/{record_id}/promote-repairs")
    def promote_repairs(
        record_id: int,
        device_udid: str,
        reviewer: str = Query(min_length=1, max_length=100),
        platform: DevicePlatform = Query(default=DevicePlatform.ANDROID),
    ) -> dict:
        record = repository.get(record_id)
        if record is None:
            raise HTTPException(status_code=404, detail="执行计划不存在")
        patches = repair_patch_store.list_for_case(
            record.plan.source.case_id,
            device_udid,
            platform.value,
            include_verified=True,
            source_plan_record_id=record.record_id,
            source_plan_hash=plan_execution_hash(record.plan),
        )
        if not patches:
            raise HTTPException(status_code=409, detail="当前设备没有通过硬验收的修复补丁")
        versions = [
            item.plan.version
            for item in repository.list()
            if item.plan.plan_id == record.plan.plan_id
        ]
        versions.extend(
            item["plan"].version
            for item in upgrade_candidates.list()
            if item["plan"].plan_id == record.plan.plan_id
        )
        plan = promote_plan_version(
            record,
            patches,
            reviewer,
            version=max(versions, default=record.plan.version) + 1,
        )
        validation = validate_plan(plan)
        if not validation.valid:
            raise HTTPException(status_code=409, detail="修复后计划未通过静态校验")
        target_keys = {
            step.target_key for step in plan.steps if step.target_key
        } | {oracle.target_key for oracle in plan.oracles if oracle.target_key}
        plan.locator_snapshot = {
            key: list(resolve_target(key, platform))
            for key in sorted(target_keys)
        }
        plan.status = PlanStatus.CANDIDATE
        candidate = upgrade_candidates.create(
            case_id=plan.source.case_id,
            source_record_id=record.record_id,
            device_udid=device_udid,
            platform=platform.value,
            reviewer=reviewer,
            plan=plan,
            content_hash=plan_execution_hash(plan),
            source_plan_hash=plan_execution_hash(record.plan),
            patch_ids=[patch.patch_id for patch in patches],
        )
        return {
            "candidate_id": candidate["candidate_id"],
            "case_id": candidate["case_id"],
            "status": candidate["status"],
            "plan": candidate["plan"].model_dump(mode="json"),
            "message": "修复内容已保存为隔离候选；完整首轮和指定稳定性验证通过后才会自动启用",
        }

    @application.get("/api/upgrade-candidates")
    def list_upgrade_candidates() -> list[dict]:
        return [
            {**{key: value for key, value in item.items() if key != "plan"},
             "plan": item["plan"].model_dump(mode="json")}
            for item in upgrade_candidates.list()
        ]

    @application.post("/api/upgrade-candidates/{candidate_id}/execute")
    def execute_upgrade_candidate(candidate_id: str) -> dict:
        candidate = upgrade_candidates.get(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="修复候选不存在")
        if candidate["status"] not in {"awaiting_first_run", "failed_first_run", "failed_stability"}:
            raise HTTPException(status_code=409, detail="候选当前不处于等待完整首轮执行状态")
        if batch_manager.active_batch_id is not None:
            raise HTTPException(status_code=409, detail="稳定性验证批次正在占用测试设备")
        source_record = repository.get(int(candidate["source_record_id"]))
        if source_record is None or not repair_source_is_current(source_record, candidate["source_plan_hash"]):
            raise HTTPException(status_code=409, detail="候选来源计划已变化，请重新生成候选")
        try:
            check_repair_scope(source_record.plan, candidate["plan"], candidate["analysis"].get("removed_steps"))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            require_execution_platform(DevicePlatform(candidate["platform"]), candidate["device_udid"])
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not upgrade_candidates.claim_first_run(candidate_id):
            raise HTTPException(status_code=409, detail="候选首轮已开始或已完成")
        plan = candidate["plan"]
        plan.qa_confirmed_by = candidate["reviewer"]
        plan.qa_confirmed_at = datetime.now(timezone.utc).isoformat()
        occupied_versions = [
            item.plan.version for item in repository.list()
            if item.plan.plan_id == plan.plan_id
        ]
        occupied_versions.extend(
            item["plan"].version for item in upgrade_candidates.list()
            if item["candidate_id"] != candidate_id and item["plan"].plan_id == plan.plan_id
        )
        plan.version = max(occupied_versions, default=source_record.plan.version) + 1
        validation = validate_plan(plan, baseline_plan=source_record.plan)
        if not validation.valid:
            upgrade_candidates.mark_first_run(candidate_id, "", False, None, "候选计划未通过静态校验")
            raise HTTPException(status_code=409, detail="候选计划未通过静态校验")
        try:
            candidate_record = repository.save(plan, validation)
        except Exception as exc:
            upgrade_candidates.mark_first_run(candidate_id, "", False, None, f"候选版本保存失败：{exc}")
            raise HTTPException(status_code=409, detail="候选版本保存失败，请重新生成修复候选") from exc
        fake_record = PlanRecord(
            record_id=candidate_record.record_id,
            plan=plan,
            validation=validation,
            created_at=candidate_record.created_at,
            updated_at=candidate_record.updated_at,
        )
        try:
            result = device_coordinator.run(
                fake_record, DevicePlatform(candidate["platform"]), candidate["device_udid"]
            )
        except subprocess.TimeoutExpired as exc:
            upgrade_candidates.mark_first_run(candidate_id, "", False, candidate_record.record_id,
                                               "候选完整首轮执行超时")
            raise HTTPException(status_code=504, detail="候选完整首轮执行超过 5 分钟") from exc
        except DeviceBusyError as exc:
            upgrade_candidates.release_first_run_claim(candidate_id)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            upgrade_candidates.mark_first_run(candidate_id, "", False, candidate_record.record_id,
                                               str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            upgrade_candidates.mark_first_run(candidate_id, "", False, candidate_record.record_id, "候选执行异常，请检查后重试")
            raise HTTPException(status_code=500, detail="候选执行异常，正式计划未更新") from exc
        run_id = str(result.get("run_id") or "")
        passed = result.get("status") == "passed"
        if candidate["source_run_id"]:
            passed = passed and bool(run_id) and (
                {item.get("step_id") for item in result.get("steps", []) if item.get("status") == "passed"}
                == {item.step_id for item in plan.steps}
                and all(item.get("status") == "passed" for item in result.get("steps", []))
                and {item.get("oracle_id") for item in result.get("oracles", []) if item.get("passed")}
                == {item.oracle_id for item in plan.oracles}
                and all(item.get("passed") for item in result.get("oracles", []))
            )
        saved_record_id = None
        error = "" if passed else str(result.get("message") or "候选首轮未通过或缺少完整步骤/验收证据")
        if passed:
            saved_record_id = candidate_record.record_id
        result["plan_record_id"] = candidate_record.record_id
        result["upgrade_candidate_id"] = candidate_id
        if candidate["source_run_id"]:
            original = review_repository.detail(candidate["source_run_id"])
            before = original["result"] if original else {}
            analyzed_run = candidate["analysis"].get("analyzed_run_id")
            path = evidence_file(runs_dir, f"{analyzed_run}/result.json") if analyzed_run and Path(analyzed_run).name == analyzed_run else None
            if path:
                try:
                    previous = json.loads(path.read_text(encoding="utf-8"))
                    if (previous.get("case_id") == plan.source.case_id
                            and (previous.get("device") or {}).get("udid") == candidate["device_udid"]):
                        before = previous
                except (OSError, ValueError):
                    pass
            result["repair_progress"] = repair_progress(before, result)
        result_path = Path(str(result.get("result_path") or ""))
        if result_path.is_file():
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        upgrade_candidates.mark_first_run(candidate_id, run_id, passed, saved_record_id or candidate_record.record_id, error)
        artifact_queue.enqueue("report", run_id)
        if candidate["source_run_id"] and run_id:
            review_repository.add_rerun(candidate["source_run_id"], result)
        if passed and candidate["validation_lane"] == "execution":
            validated = plan.model_copy(update={"status": PlanStatus.VALIDATED})
            activated = repair_source_is_current(source_record, candidate["source_plan_hash"]) and upgrade_candidates.activate_transactionally(
                candidate_id, None, source_plan_hash=candidate["source_plan_hash"],
                candidate_content_hash=plan_execution_hash(plan), validated_plan_json=validated.model_dump_json(),
            )
            if activated:
                review_repository.update(candidate["source_run_id"], ReviewUpdate(
                    review_outcome="REPAIR_VERIFIED", reviewer=candidate["reviewer"],
                    review_note=f"修复计划 v{plan.version} 完整首轮及原验收通过，已启用",
                ))
            else:
                upgrade_candidates.reject_activation(candidate_id, "验证期间正式计划或原文发生变化，未启用修复")
        refresh_case_state(force=True)
        return {**result, "upgrade_candidate_status": upgrade_candidates.get(candidate_id)["status"]}

    @application.post("/api/upgrade-candidates/{candidate_id}/stability")
    def validate_candidate_stability(candidate_id: str) -> dict:
        candidate = upgrade_candidates.get(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="修复候选不存在")
        if candidate["status"] != "awaiting_stability" or candidate["validation_lane"] != "stability":
            raise HTTPException(status_code=409, detail="请先完成候选首轮验证")
        if batch_manager.active_batch_id is not None:
            raise HTTPException(status_code=409, detail="已有稳定性批次正在运行")
        source = repository.get(candidate["source_record_id"])
        if source is None or not repair_source_is_current(source, candidate["source_plan_hash"]):
            raise HTTPException(status_code=409, detail="来源计划已变化，请重新分析")
        try:
            require_execution_platform(DevicePlatform(candidate["platform"]), candidate["device_udid"])
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        batch_id = candidate.get("stability_batch_id")
        if not batch_id:
            batch = create_stability_batch(StabilityBatchInput(
                name=f"修复验证 {candidate['case_id']}", plan_record_ids=[candidate["candidate_record_id"]],
                repetitions=candidate["required_repetitions"], platform=DevicePlatform(candidate["platform"]),
                device_udid=candidate["device_udid"],
            ))
            batch_id = batch["batch_id"]
        try:
            return batch_manager.start(batch_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post("/api/stability/batches")
    def create_stability_batch(payload: StabilityBatchInput) -> dict:
        qualification = {row["case_id"]: row for row in execution_case_rows(payload.device_udid)}
        allowed_record_ids = {
            record.record_id for record in validatable_plan_records(device_udid=payload.device_udid)
        }
        candidate_by_record = {
            item["candidate_record_id"]: item
            for item in upgrade_candidates.list()
            if item.get("status") == "awaiting_stability"
            and item.get("device_udid") == payload.device_udid
            and item.get("platform") == payload.platform.value
            and item.get("candidate_record_id") is not None
        }
        records = []
        for record_id in payload.plan_record_ids:
            record = repository.get(record_id)
            if record is None:
                raise HTTPException(status_code=404, detail=f"计划记录 {record_id} 不存在")
            candidate_eligible = record_id in candidate_by_record
            if not record.validation.valid or record.plan.status not in {
                PlanStatus.CONFIRMED,
                PlanStatus.VALIDATED,
            } and not (candidate_eligible and record.plan.status == PlanStatus.CANDIDATE):
                raise HTTPException(
                    status_code=409,
                    detail=f"计划 {record.plan.source.case_id} 尚未通过 QA 确认和静态校验",
                )
            if record_id not in allowed_record_ids and not candidate_eligible:
                raise HTTPException(status_code=409, detail=f"用例 {record.plan.source.case_id} 首轮执行未通过，不能进入稳定性验证")
            state = qualification.get(record.plan.source.case_id, {})
            if not candidate_eligible and state.get("stability_exemption_reason"):
                raise HTTPException(status_code=409, detail=f"用例 {record.plan.source.case_id} 免稳定性验证：{state['stability_exemption_reason']}")
            if not candidate_eligible and not state.get("stability_ready", True):
                raise HTTPException(status_code=409, detail=f"用例 {record.plan.source.case_id} 前置待准备：{state.get('stability_block_reason', '')}")
            if candidate_eligible:
                candidate = candidate_by_record[record_id]
                if (payload.device_udid != candidate["device_udid"] or payload.platform.value != candidate["platform"]
                        or payload.repetitions < candidate["required_repetitions"]):
                    raise HTTPException(status_code=409, detail="修复候选必须使用原设备，验证次数不能少于原要求")
                if candidate.get("stability_batch_id"):
                    raise HTTPException(status_code=409, detail="候选已有验证批次，请完成该批次")
            records.append(record)
        conflicts = state_conflicts([record.plan for record in records])
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "批次包含未隔离的共享数据状态冲突，请拆分批次或为相关计划声明恢复策略",
                    "conflicts": [item.__dict__ for item in conflicts],
                },
            )
        plan_snapshots = {}
        for record in records:
            execution_record = record
            if payload.device_udid:
                patches = repair_patch_store.list_for_case(
                    record.plan.source.case_id,
                    payload.device_udid,
                    payload.platform.value,
                    batch_id="",
                    source_plan_record_id=record.record_id,
                    source_plan_hash=plan_execution_hash(record.plan),
                )
                execution_record = patched_record(record, patches)
            frozen_plan = execution_record.plan.model_copy(deep=True)
            target_keys = {
                item.target_key
                for item in [*frozen_plan.steps, *frozen_plan.oracles]
                if item.target_key
            }
            frozen_plan.locator_snapshot = {
                key: list(resolve_target(key, payload.platform))
                for key in sorted(target_keys)
            }
            plan_snapshots[record.record_id] = frozen_plan.model_dump(mode="json")
        batch = stability_repository.create_batch(
            payload, records, plan_snapshots=plan_snapshots
        )
        for record in records:
            candidate = candidate_by_record.get(record.record_id)
            if candidate:
                upgrade_candidates.attach_batch(candidate["candidate_id"], batch["batch_id"])
        return batch

    @application.get("/api/stability/batches/{batch_id}")
    def get_stability_batch(batch_id: str) -> dict:
        batch = stability_repository.get_batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="稳定性验证批次不存在")
        return batch

    @application.post("/api/stability/batches/{batch_id}/start")
    def start_stability_batch(batch_id: str) -> dict:
        batch = stability_repository.get_batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="稳定性验证批次不存在")
        try:
            require_execution_platform(
                DevicePlatform(batch["platform"]),
                batch.get("device_udid"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            return batch_manager.start(batch_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="稳定性验证批次不存在") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post("/api/stability/batches/{batch_id}/stop")
    def stop_stability_batch(batch_id: str) -> dict:
        try:
            return batch_manager.stop(batch_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="稳定性验证批次不存在") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.put("/api/stability/batches/{batch_id}/efficiency")
    def update_stability_efficiency(batch_id: str, payload: EfficiencyBaselineInput) -> dict:
        batch = stability_repository.update_efficiency_baseline(batch_id, payload)
        if batch is None:
            raise HTTPException(status_code=404, detail="稳定性验证批次不存在")
        return batch

    @application.get("/api/stability/batches/{batch_id}/export.json")
    def export_stability_batch(batch_id: str) -> Response:
        batch = stability_repository.get_batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="稳定性验证批次不存在")
        return Response(
            json.dumps(batch, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{batch_id}.json"',
            },
        )

    @application.post("/api/plans/{record_id}/execute")
    def execute_plan(
        record_id: int,
        platform: DevicePlatform = Query(default=DevicePlatform.ANDROID),
        device_udid: str | None = Query(default=None),
    ) -> dict:
        record = repository.get(record_id)
        if record is None:
            raise HTTPException(status_code=404, detail="计划不存在")
        if record.plan.status not in {PlanStatus.CONFIRMED, PlanStatus.VALIDATED}:
            raise HTTPException(status_code=409, detail="只有 QA 已确认的计划才允许执行")
        state = next((row for row in execution_case_rows()
                      if row["qualification_plan_record_id"] == record_id), {})
        if not state.get("stability_ready", True):
            raise HTTPException(status_code=409, detail=f"前置待准备：{state.get('stability_block_reason', '')}")
        if batch_manager.active_batch_id is not None:
            raise HTTPException(status_code=409, detail="稳定性验证批次正在占用测试设备")
        try:
            require_execution_platform(platform, device_udid)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        try:
            result = device_coordinator.run(record, platform, device_udid)
        except DeviceBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail="真机执行超过 5 分钟，已终止等待") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        artifact_queue.enqueue("report", str(result.get("run_id") or ""))

        if result.get("status") == "passed" and record.plan.status != PlanStatus.VALIDATED:
            plan = record.plan.model_copy(deep=True)
            plan.status = PlanStatus.VALIDATED
            repository.update(record_id, plan, record.validation)
        refresh_case_state(force=True)
        return result

    application.state.plan_repository = repository
    application.state.runs_dir = runs_dir
    application.state.stability_repository = stability_repository
    application.state.review_repository = review_repository
    application.state.repair_patch_store = repair_patch_store
    application.state.stability_batch_manager = batch_manager
    application.state.device_execution_coordinator = device_coordinator
    application.state.collection_manager = collection_manager
    application.state.artifact_queue = artifact_queue
    application.state.upgrade_candidate_repository = upgrade_candidates

    @application.get("/api/artifact-jobs")
    def list_artifact_jobs() -> list[dict]:
        return artifact_queue.list()

    @application.get("/api/artifact-jobs/{kind}/{run_id}")
    def get_artifact_job(kind: str, run_id: str) -> dict:
        job = artifact_queue.get(kind, run_id)
        if job is None:
            raise HTTPException(status_code=404, detail="后台任务不存在")
        return job

    @application.post("/api/artifact-jobs/{kind}/{run_id}/retry")
    def retry_artifact_job(kind: str, run_id: str) -> dict:
        try:
            return artifact_queue.enqueue(kind, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post("/api/archive/runs/{run_id}")
    def archive_run_evidence(run_id: str) -> dict:
        if batch_manager.active_batch_id is not None:
            raise HTTPException(status_code=409, detail="稳定性批次运行中，暂不能归档")
        review = review_repository.get(run_id)
        if review and review.get("review_status") == "PENDING":
            raise HTTPException(status_code=409, detail="待复核运行不能归档")
        for candidate in upgrade_candidates.list():
            if candidate.get("status") not in {"first_run_running", "awaiting_first_run", "awaiting_stability"}:
                continue
            if candidate.get("first_run_id") == run_id or candidate.get("source_run_id") == run_id:
                raise HTTPException(status_code=409, detail="候选脚本尚未完成稳定性验证，不能归档其首轮证据")
            batch_id = candidate.get("stability_batch_id")
            detail = stability_repository.get_batch(batch_id) if batch_id else None
            if detail and any(item.get("run_id") == run_id for item in detail.get("attempts", [])):
                raise HTTPException(status_code=409, detail="候选稳定性验证仍未完成，不能归档其证据")
        try:
            return artifact_queue.enqueue("archive", run_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.get("/api/archive/evidence/{run_id}/{relative_path:path}")
    def read_archived_evidence(run_id: str, relative_path: str) -> FileResponse:
        try:
            path = artifact_queue.evidence_file(run_id, relative_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="归档证据不存在") from exc
        return FileResponse(path)
    return application


app = create_app()
