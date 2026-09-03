# -*- coding: utf-8 -*-
"""
本地模型路由网关 · 核心
==================================================================
功能：
  · 统一 OpenAI 兼容端点（/v1/chat/completions、/v1/models）
  · 本地网关鉴权：客户端统一填网关地址 + 本地 key，真实供应商 key 集中托管
  · 模型注册表路由：本地自定义模型名 → 供应商 + 上游真实模型 ID（别名映射）
  · 双级限流：供应商（账号级）信号量 + 模型级信号量，FIFO 排队
  · 时间阈值流转：429/5xx 先排队退避重试，超过「流转等待阈值」仍失败
    才按失败后流转链依次转移，最终自定义兜底
  · SSE 流式原样转发

仅依赖 Python 标准库，由 gateway_gui.py 调用，也可 headless 运行。
"""

import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------- 配置 ----------------


def default_gateway():
    return {
        "port": int(os.environ.get("GATEWAY_PORT", "8787")),
        "api_key": os.environ.get("GATEWAY_API_KEY", ""),
        "request_timeout": 600,
        "queue_timeout": 600,
        "transfer_wait_seconds": 60,
        "fallback_max_hops": 3,
        "lan_access": False,  # True=允许局域网设备访问（监听 0.0.0.0）
    }


def default_config():
    """空白配置：供应商和模型全由用户自行添加。"""
    return {"gateway": default_gateway(), "providers": [], "models": [],
            "token_stats": {}}  # 按本地模型名累计 token（跨运行持久化）


def coerce_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def coerce_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_hhmm(v):
    """解析 HH:MM 为当日分钟数；非法返回 None。"""
    try:
        hh, mm = str(v).strip().split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh * 60 + mm
    except (ValueError, AttributeError):
        pass
    return None


def _extract_usage_sse(buf):
    """从 SSE 原始流字节中提取 usage（尾帧常带 usage 字段）。
    找最后一个 "usage":{...} JSON 片段，取不到返回全 0。"""
    import re
    try:
        text = buf.decode("utf-8", "ignore")
        # 逐帧找 data: 行，解析 JSON 取 usage（取最后一次出现的）
        best = None
        for m in re.finditer(r'data:\s*(\{.*?\})\s*(?:\r?\n|$)', text, re.S):
            frag = m.group(1)
            if '"usage"' not in frag:
                continue
            try:
                j = json.loads(frag)
                u = j.get("usage")
                if isinstance(u, dict):
                    best = u
            except Exception:
                continue
        if best is None:
            return {"prompt_tokens": 0, "completion_tokens": 0}
        return {"prompt_tokens": coerce_int(best.get("prompt_tokens"), 0),
                "completion_tokens": coerce_int(best.get("completion_tokens"), 0)}
    except Exception:
        return {"prompt_tokens": 0, "completion_tokens": 0}


def _route_hit(route, cur_min):
    """判断当前时间（分钟数）是否命中时间段规则。
    cross_day=True 为显式「到第二天」；未标记时若 start > end 自动按跨午夜处理（兼容旧数据）。"""
    s = parse_hhmm(route.get("start"))
    e = parse_hhmm(route.get("end"))
    if s is None or e is None:
        return False
    if route.get("cross_day") or s > e:
        return cur_min >= s or cur_min < e  # 跨天：如 18:01 → 次日 06:00
    return s <= cur_min < e


def validate_config(cfg):
    errs = []
    gw = cfg.get("gateway", {})
    port = coerce_int(gw.get("port"), 0)
    if not (1 <= port <= 65535):
        errs.append("网关端口需在 1-65535 之间")
    if coerce_int(gw.get("transfer_wait_seconds"), 0) < 1:
        errs.append("流转等待阈值需 >= 1 秒")
    if coerce_int(gw.get("fallback_max_hops"), -1) < 0:
        errs.append("失败后流转最大跳数不能为负")
    prov_ids = set()
    for p in cfg.get("providers", []):
        if not p.get("id"):
            errs.append("供应商 id 不能为空")
        elif p["id"] in prov_ids:
            errs.append("供应商 id 重复：%s" % p["id"])
        prov_ids.add(p.get("id"))
        if not str(p.get("base_url", "")).startswith("http"):
            errs.append("供应商 %s 的上游地址需以 http/https 开头" % p.get("id"))
    model_ids = set()
    for m in cfg.get("models", []):
        if not m.get("id"):
            errs.append("模型本地名不能为空")
        elif m["id"] in model_ids:
            errs.append("模型本地名重复：%s" % m["id"])
        model_ids.add(m.get("id"))
        if m.get("provider") not in prov_ids:
            errs.append("模型 %s 引用的供应商 %s 不存在" % (m.get("id"), m.get("provider")))
        if not m.get("upstream_model"):
            errs.append("模型 %s 的上游模型 ID 不能为空" % m.get("id"))
    return errs


# ---------------- 限流器（供应商级 + 模型级） ----------------


class RateLimiter:
    def __init__(self, providers):
        self._lock = threading.Lock()
        self._prov_sem = {}
        self._model_sem = {}
        for p in providers:
            self._prov_sem[p["id"]] = threading.Semaphore(
                max(1, coerce_int(p.get("max_concurrency"), 16)))

    def provider_sem(self, provider_id):
        with self._lock:
            if provider_id not in self._prov_sem:
                self._prov_sem[provider_id] = threading.Semaphore(16)
            return self._prov_sem[provider_id]

    def model_sem(self, key, concurrency):
        with self._lock:
            if key not in self._model_sem:
                self._model_sem[key] = threading.Semaphore(
                    max(1, coerce_int(concurrency, 16)))
            return self._model_sem[key]


# ---------------- 网关 ----------------


class ModelGateway:
    def __init__(self, config=None, logger=None):
        self.cfg = config or default_config()
        self.logger = logger or (lambda msg: None)
        self._httpd = None
        self._thread = None
        self._stat_lock = threading.Lock()
        self.stats = {"done": 0, "retried": 0, "failed": 0,
                      "fallback": 0, "inflight": 0}
        self.model_stats = {}  # 按本地模型名统计
        self._start_ts = time.time()
        self._rebuild_limiter()
        self._log("网关核心已创建 · 供应商 %d · 模型 %d"
                  % (len(self.cfg.get("providers", [])),
                     len(self.cfg.get("models", []))))

    def _rebuild_limiter(self):
        self.limiter = RateLimiter(self.cfg.get("providers", []))
        self._prov_map = {p["id"]: p for p in self.cfg.get("providers", [])}
        self._model_map = {m["id"]: m for m in self.cfg.get("models", [])}

    def reload_config(self, config):
        """GUI 保存后热更新（不重启服务，限流器重建）。"""
        self.cfg = config
        self._rebuild_limiter()
        self._log("配置已热更新")

    # ---- 生命周期 ----
    def start(self):
        if self._httpd is not None:
            return False, "已在运行"
        errs = validate_config(self.cfg)
        if errs:
            return False, "配置有误：" + "；".join(errs)
        port = coerce_int(self.cfg["gateway"].get("port"), 8787)
        try:
            lan = bool(self.cfg["gateway"].get("lan_access", False))
            host = "0.0.0.0" if lan else "127.0.0.1"
            self._bind_host = host
            httpd = ThreadingHTTPServer((host, port), self._make_handler())
        except Exception as e:
            return False, "启动失败：%s" % e
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        daemon=True, name="gateway-httpd")
        self._thread.start()
        self._log("已启动 · 监听 %s:%d · 模型 %d 个 · 局域网访问：%s"
                  % (host, port, len(self._model_map), "开" if lan else "关"))
        if lan:
            return True, "已启动，监听 %s:%d（局域网可访问）" % (host, port)
        return True, "已启动，监听 127.0.0.1:%d" % port

    def stop(self):
        if self._httpd is None:
            return "未在运行"
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
        self._httpd = None
        self._thread = None
        self._log("已停止 · 成功 %d / 重试 %d / 流转 %d / 失败 %d"
                  % (self.stats["done"], self.stats["retried"],
                     self.stats["fallback"], self.stats["failed"]))
        return "已停止"

    @property
    def running(self):
        return self._httpd is not None

    def snapshot(self):
        with self._stat_lock:
            s = dict(self.stats)
        s["running"] = self.running
        s["port"] = coerce_int(self.cfg["gateway"].get("port"), 8787)
        s["lan"] = bool(self.cfg["gateway"].get("lan_access", False))
        s["uptime"] = int(time.time() - self._start_ts)
        return s

    # ---- 内部 ----
    def _log(self, msg):
        try:
            self.logger(msg)
        except Exception:
            pass

    def _bump(self, key, delta=1):
        with self._stat_lock:
            self.stats[key] = self.stats.get(key, 0) + delta
            return self.stats[key]

    def _bump_model(self, mid, key, delta=1):
        with self._stat_lock:
            st = self.model_stats.setdefault(
                mid, {"done": 0, "retried": 0, "failed": 0, "fallback": 0})
            st[key] = st.get(key, 0) + delta

    def _bump_tokens(self, mid, prompt, completion):
        """累计 token 用量写进 cfg（由 GUI 层负责持久化到 config 文件）。"""
        prompt = max(0, coerce_int(prompt, 0))
        completion = max(0, coerce_int(completion, 0))
        if prompt <= 0 and completion <= 0:
            return
        with self._stat_lock:
            ts = self.cfg.setdefault("token_stats", {})
            st = ts.setdefault(mid, {"prompt": 0, "completion": 0})
            st["prompt"] += prompt
            st["completion"] += completion
        self._token_dirty = True  # GUI 轮询据此节流落盘

    def token_flush_pending(self):
        """token_stats 是否有未落盘的更新（GUI 每 5 秒查询一次并 save_config）。"""
        return bool(getattr(self, "_token_dirty", False))

    def token_flush_done(self):
        self._token_dirty = False

    # ---- 路由 ----
    def _build_chain(self, local_id):
        """本地模型名 → 调用链 [主目标, 流转1, ..., 兜底]。
        每个目标 = (provider_cfg, upstream_model, model_entry, sem_key, sem_conc)
        主跳按本机时间匹配 time_routes（如有）：命中则换用该时段的供应商/上游模型，
        未命中任何时段则用默认上游。"""
        m = self._model_map.get(local_id)
        if m is None:
            return None
        # 时间段选路：命中第一条规则即用它作为主跳目标；
        # 未命中任何时段则用兜底模型 default_route（未配置则回退模型默认上游）
        main_entry = m
        routes = m.get("time_routes") or []
        if routes:
            now = time.localtime()
            cur = now.tm_hour * 60 + now.tm_min
            hit = None
            for r in routes:
                if _route_hit(r, cur):
                    hit = r
                    break
            if hit:
                main_entry = hit
            elif isinstance(m.get("default_route"), dict):
                main_entry = m["default_route"]
        chain = []
        hops = coerce_int(self.cfg["gateway"].get("fallback_max_hops"), 3)

        def make(entry):
            prov = self._prov_map.get(entry.get("provider"))
            if prov is None:
                return None
            upstream = entry.get("upstream_model") or m.get("upstream_model")
            if not upstream:
                return None  # 纯时段模型既未命中也无兜底 → 无可用目标
            return (prov, upstream, m)

        main = make(main_entry)
        if main:
            chain.append(main)
        for fb in (m.get("fallback") or [])[:hops]:
            t = make(fb)
            if t:
                chain.append(t)
        return chain

    # ---- HTTP handler ----
    def _make_handler(self):
        gw = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "ModelGateway/1.0"

            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _auth_ok(self):
                key = gw.cfg["gateway"].get("api_key", "")
                if not key:
                    return True
                auth = self.headers.get("Authorization", "")
                return auth == "Bearer " + key

            def _dispatch(self, method):
                if not self._auth_ok():
                    self._reply(401, self._jerr("网关鉴权失败（本地 key 不匹配）"),
                                "application/json")
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None

                if method == "GET":
                    self._handle_get()
                    return
                if method == "POST" and "chat/completions" in self.path:
                    self._handle_chat(body)
                    return
                self._reply(404, self._jerr("未知路径 %s" % self.path),
                            "application/json")

            def _handle_get(self):
                if "models" in self.path:
                    data = {"object": "list", "data": [
                        {"id": m["id"], "object": "model",
                         "created": int(gw._start_ts), "owned_by": m.get("provider")}
                        for m in gw.cfg.get("models", [])]}
                    self._reply(200, json.dumps(data).encode(), "application/json")
                else:
                    self._reply(200, self._jerr('{"status":"ok","running":%s}'
                                                % ("true" if gw.running else "false")),
                                "application/json")

            def _handle_chat(self, body):
                try:
                    payload = json.loads(body or b"{}")
                except Exception:
                    self._reply(400, self._jerr("请求体不是合法 JSON"),
                                "application/json")
                    return
                local_id = payload.get("model", "")
                is_stream = bool(payload.get("stream"))
                chain = gw._build_chain(local_id)
                if not chain:
                    self._reply(404, self._jerr(
                        "模型 %s 未在网关注册，或当前时段未配置可用上游。"
                        "请在网关「模型」区添加映射或兜底模型。" % local_id),
                        "application/json")
                    return

                last_err = (500, "未知错误")
                for hop, (prov, upstream, model_entry) in enumerate(chain):
                    if hop > 0:
                        gw._bump("fallback")
                        gw._bump_model(local_id, "fallback")
                        gw._log("[%s] 主路失败 → 流转到 %s/%s"
                                % (local_id, prov["id"], upstream))
                    ok, last_err = self._try_target(
                        prov, upstream, model_entry, local_id,
                        payload, is_stream, hop)
                    if ok:
                        return
                code, msg = last_err
                self._reply(code if isinstance(code, int) else 502,
                            self._jerr(msg), "application/json")

            def _try_target(self, prov, upstream, model_entry, local_id,
                            payload, is_stream, hop):
                """尝试单个目标，返回 (成功?, (code,msg))。"""
                transfer_wait = coerce_int(
                    gw.cfg["gateway"].get("transfer_wait_seconds"), 60)
                max_retries = coerce_int(prov.get("max_retries"), 4)
                base_delay = coerce_float(prov.get("base_delay"), 8)
                req_timeout = coerce_int(
                    gw.cfg["gateway"].get("request_timeout"), 600)
                queue_timeout = coerce_int(
                    gw.cfg["gateway"].get("queue_timeout"), 600)
                conc = model_entry.get("max_concurrency") or \
                    prov.get("max_concurrency") or 16

                prov_sem = gw.limiter.provider_sem(prov["id"])
                model_sem = gw.limiter.model_sem(
                    "%s|%s" % (prov["id"], upstream), conc)

                url = self._compose_url(prov["base_url"], "chat/completions")
                send = dict(payload)
                send["model"] = upstream
                body = json.dumps(send).encode()
                headers = {"Content-Type": "application/json"}
                if prov.get("api_key"):
                    headers["Authorization"] = "Bearer " + prov["api_key"]

                deadline = time.time() + transfer_wait
                attempt = 0
                last_err = (502, "请求失败")

                while True:
                    if time.time() >= deadline:
                        gw._log("[%s] %s 超过流转阈值(%ds)，流转"
                                % (local_id, upstream, transfer_wait))
                        return False, (504, "%s 超过流转等待阈值" % upstream)

                    got_p = prov_sem.acquire(timeout=queue_timeout)
                    if not got_p:
                        return False, (503, "%s 排队超时" % upstream)
                    got_m = model_sem.acquire(timeout=queue_timeout)
                    if not got_m:
                        prov_sem.release()
                        return False, (503, "%s 模型排队超时" % upstream)

                    gw._bump("inflight", 1)
                    try:
                        req = urllib.request.Request(
                            url, data=body, headers=headers, method="POST")
                        resp = urllib.request.urlopen(req, timeout=req_timeout)
                        usage = {"prompt_tokens": 0, "completion_tokens": 0}
                        if is_stream:
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Cache-Control", "no-cache")
                            self.send_header("Connection", "keep-alive")
                            self.send_header("Transfer-Encoding", "chunked")
                            self.end_headers()
                            try:
                                buf = b""
                                while True:
                                    chunk = resp.read(4096)
                                    if not chunk:
                                        break
                                    buf += chunk
                                    size = b"%X\r\n" % len(chunk)
                                    self.wfile.write(size + chunk + b"\r\n")
                                    self.wfile.flush()
                                self.wfile.write(b"0\r\n\r\n")
                                self.wfile.flush()
                                # 从 SSE 流中提取 usage（多数兼容实现在尾帧带 usage）
                                usage = _extract_usage_sse(buf)
                            finally:
                                resp.close()
                        else:
                            data = resp.read()
                            resp.close()
                            self._reply(200, data, "application/json")
                            try:
                                j = json.loads(data.decode("utf-8", "ignore"))
                                u = j.get("usage") or {}
                                usage = {"prompt_tokens": coerce_int(
                                             u.get("prompt_tokens"), 0),
                                         "completion_tokens": coerce_int(
                                             u.get("completion_tokens"), 0)}
                            except Exception:
                                pass
                        gw._bump_tokens(local_id,
                                        usage["prompt_tokens"],
                                        usage["completion_tokens"])
                        gw._bump("done")
                        gw._bump_model(local_id, "done")
                        if hop == 0:
                            gw._log("[%s] 成功 · %s/%s"
                                    % (local_id, prov["id"], upstream))
                        else:
                            gw._log("[%s] 流转成功 · %s/%s"
                                    % (local_id, prov["id"], upstream))
                        return True, None
                    except urllib.error.HTTPError as e:
                        try:
                            err_body = e.read()
                        except Exception:
                            err_body = b""
                        last_err = (e.code, err_body.decode("utf-8", "ignore")[:200])
                        if (e.code == 429 or e.code >= 500) \
                                and attempt < max_retries \
                                and time.time() < deadline:
                            attempt += 1
                            gw._bump("retried")
                            gw._bump_model(local_id, "retried")
                            delay = min(base_delay * (2 ** (attempt - 1))
                                        * (0.8 + 0.4 * random.random()), 60)
                            gw._log("[%s] %s 上游 %d → %.0fs 后重试 %d/%d（先排队）"
                                    % (local_id, upstream, e.code, delay,
                                       attempt, max_retries))
                            time.sleep(delay)
                            continue
                        return False, last_err
                    except Exception as e:
                        last_err = (502, str(e))
                        if attempt < max_retries and time.time() < deadline:
                            attempt += 1
                            gw._bump("retried")
                            time.sleep(base_delay)
                            continue
                        return False, last_err
                    finally:
                        model_sem.release()
                        prov_sem.release()
                        gw._bump("inflight", -1)

            def _compose_url(self, base_url, path):
                """智能拼接上游 URL：兼容 base_url 带不带 /v1 结尾。
                阿里云百炼 base_url 形如 .../compatible-mode/v1，DeepSeek 形如 .../api.deepseek.com。
                统一约定：base_url 已经指到 v1 层时不再重复拼 /v1。"""
                base = str(base_url or "").rstrip("/")
                if not base:
                    return ""
                # 若 base 已以 /v1 结尾（含 /compatible-mode/v1），直接拼 path
                if base.endswith("/v1"):
                    return base + "/" + path
                # 否则补 /v1
                return base + "/v1/" + path

            def _jerr(self, msg):
                return json.dumps(
                    {"error": {"message": msg, "type": "gateway_error"}},
                    ensure_ascii=False).encode()

            def _reply(self, code, data, ctype):
                data = data or b""
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

        return Handler


# ---------------- 命令行 / 自检 ----------------


def _selftest():
    print("[selftest] 使用空白配置启动，仅验证服务可拉起", flush=True)
    cfg = default_config()
    cfg["gateway"]["port"] = 8790
    p = ModelGateway(cfg, print)
    ok, msg = p.start()
    print("[selftest] 启动 ->", ok, msg, flush=True)
    p.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    cfg = default_config()

    def log(msg):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        sys.stderr.flush()

    g = ModelGateway(cfg, log)
    ok, msg = g.start()
    print(msg)
    if not ok:
        sys.exit(1)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        g.stop()
