"""Audit lifecycle and per-instance contexts, independent of MCP registration.

Callbacks are explicit; the entrypoint supplies late-bound adapters for mutable
service operations. Network transport is not imported here.
"""
import contextvars
import datetime
import functools
import inspect
import json
import os
import sys
import threading
import uuid

import anyio


class RuntimeContext:
    """One instance per loaded core; child tasks inherit their caller's context."""
    def __init__(self):
        self.audit = contextvars.ContextVar("pubmed_audit_context", default=None)
        self.requests = contextvars.ContextVar("pubmed_req_ctr", default=None)
        self.lock = threading.Lock()
        self.session = uuid.uuid4().hex


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def clean(value, *, mask):
    """去掉凭据字段；诊断文本沿用现有掩码，不保存请求 URL。"""
    if isinstance(value, dict):
        return {str(k): clean(v, mask=mask) for k, v in value.items()
                if str(k).lower() not in {"api_key", "email", "url", "request_url"}}
    if isinstance(value, (list, tuple)):
        return [clean(v, mask=mask) for v in value]
    if isinstance(value, str):
        return mask(value)
    return value


def filename(session):
    # PID 加随机会话标识：并行进程及重启进程各写独立文件。
    return f"searches-{os.getpid()}-{session}.jsonl"


def audited_sync(fn, **dependencies):
    """同步文件命令复用异步审计生命周期；实际工作在调用线程执行。"""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        # 使用原签名绑定参数，避免审计只记录包装器的空参数。
        @functools.wraps(fn)
        async def operation(*args, **kwargs):
            return fn(*args, **kwargs)
        return anyio.run(functools.partial(audited(operation, **dependencies), *args, **kwargs))
    return wrapped


def audited(fn, *, context_var, schema, now, write, error_type):
    """保留工具签名；完成写文件后再提交审计，异常/取消也记录。"""
    if not inspect.iscoroutinefunction(fn):
        return audited_sync(fn, context_var=context_var, schema=schema, now=now, write=write, error_type=error_type)
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        parent = context_var.get()
        context = {"enabled": bool(os.environ.get("PUBMED_AUDIT_DIR", "").strip()),
                   "run_id": parent["run_id"] if parent else uuid.uuid4().hex,
                   "event_id": uuid.uuid4().hex, "started_at": now(),
                   "input_params": dict(bound.arguments), "requests": [], "pending": []}
        token = context_var.set(context)
        try:
            result = None
            data = None
            failure = None
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                failure = exc
                raise
            finally:
                context["finished_at"] = now()
                # _audit 被 mock 时 pending 可为空；正常路径兜底记录尚未留痕的失败。
                fields = {}
                for event, event_fields in context["pending"]:
                    fields.update(event_fields)
                fields["input_params"] = {**context["input_params"], **fields.get("input_params", {})}
                fields.update(audit_schema=schema, run_id=context["run_id"],
                              event_id=context["event_id"], started_at=context["started_at"],
                              finished_at=context["finished_at"], logical_requests=context["requests"],
                              outcome="success" if failure is None else "failed")
                if failure is not None:
                    fields["failure_type"] = type(failure).__name__
                    if isinstance(failure, anyio.get_cancelled_exc_class()):
                        fields["outcome"] = "cancelled"
                # out_path 的审计在文件成功发布后记录实际产物，而非预告写入成功。
                if failure is None and context["input_params"].get("out_path"):
                    try:
                        data = json.loads(result)
                        if isinstance(data, dict):
                            fields.update({k: data[k] for k in ("path", "sha256", "bytes") if k in data})
                    except (ValueError, TypeError):
                        pass
                # 取消路径也必须完成留痕；文件 I/O 不占用事件循环。
                with anyio.CancelScope(shield=True):
                    status = await anyio.to_thread.run_sync(
                        functools.partial(write, fn.__name__, fields,
                                          enabled=context["enabled"]))
                if isinstance(failure, error_type) and status["status"] == "failed":
                    # 异常没有 JSON 返回值，随原异常携带留痕失败，保留原错误语义。
                    failure.audit_failure = dict(status)
            if data is None:
                try:
                    data = dict(result) if isinstance(result, dict) else json.loads(result)
                except (ValueError, TypeError):
                    data = None
            if isinstance(data, dict):
                data["audit_status"] = status["status"]
                if status["status"] == "written":
                    data["audit_path"] = status["path"]
                    data["audit_event_id"] = context["event_id"]
                elif status["status"] == "failed":
                    data["audit_error"] = status["error"]
                    data.setdefault("warnings", []).append("⚠️ 检索留痕写入失败（检索结果保留）：" + status["error"])
                return data if isinstance(result, dict) else json.dumps(data, ensure_ascii=False)
            if status["status"] == "failed":
                return result + "\n⚠️ 检索留痕写入失败（检索结果保留）：" + status["error"]
            return result
        finally:
            context_var.reset(token)

    return wrapped


def write_audit(event, fields, *, enabled=True, clean_fields, now, version, lock, filename, mask):
    """失败仅返回状态，不能使已经完成的检索或文件发布失败。"""
    d = os.environ.get("PUBMED_AUDIT_DIR", "").strip()
    if not enabled or not d:
        return {"status": "disabled"}
    try:
        rec = {**clean_fields(fields), "ts": now(), "event": event, "version": version()}
        body = json.dumps(rec, ensure_ascii=False, allow_nan=False) + "\n"
        with lock:
            os.makedirs(d, exist_ok=True)
            path = os.path.abspath(os.path.join(d, filename()))
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(body)
        return {"status": "written", "path": path}
    except Exception as e:
        error = mask(f"{type(e).__name__}: {e}")
        print("⚠️ 检索留痕写入失败（不影响本次结果）：" + error, file=sys.stderr)
        return {"status": "failed", "error": error}


def audit(event, *, context_var, write, fields):
    """在工具完成后提交本次审计；单独调用仍可写入旧形状事件。"""
    context = context_var.get()
    if context is not None:
        context["pending"].append((event, fields))
        return {"status": "pending" if context["enabled"] else "disabled"}
    return write(event, fields)
