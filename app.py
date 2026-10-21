"""QQ 群精华消息本地存档应用 —— Flask 入口。

启动方式::

    python app.py                # 默认 127.0.0.1:5000，自动打开浏览器
    python app.py --port 8080    # 指定端口
    python app.py --no-browser   # 不自动打开浏览器

架构分层：
- ``core.qq_client``   网络请求（页面 / 群列表 / bkn / Cookie）
- ``core.parser``      页面解析与消息归一化
- ``core.storage``     本地快照与配置存储
- ``core.downloader``  图片并发下载
- ``core.exporter``    HTML / Markdown / JSON 导出

所有 API 统一返回 ``{ok: bool, data|error: ...}`` 结构。
"""

from __future__ import annotations

import argparse
import logging
import re
import socket
import sys
import webbrowser
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify, request, send_from_directory

from core import downloader, exporter, storage
from core.qq_client import CookieInfo, QQClientError, QQEssenceClient

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

BASE_DIR: Path = Path(__file__).resolve().parent
STATIC_DIR: Path = BASE_DIR / "static"

HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 5000
MAX_PORT_ATTEMPTS: int = 50

GC_PATTERN = re.compile(r"^\d{5,12}$")
DEFAULT_MAX_PAGES: int = 3
MAX_PAGES_LIMIT: int = 20

EXPORT_FORMATS: dict[str, str] = {
    "html": "text/html; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "json": "application/json; charset=utf-8",
}


# --------------------------------------------------------------------------- #
# 应用工厂
# --------------------------------------------------------------------------- #


def create_app() -> Flask:
    """创建并配置 Flask 应用。

    Returns:
        配置完毕的 Flask 实例。
    """
    application = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
    )
    application.config["JSON_SORT_KEYS"] = False
    application.json.ensure_ascii = False
    application.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

    storage.ensure_dirs()
    _register_routes(application)
    _register_error_handlers(application)
    return application


# --------------------------------------------------------------------------- #
# 响应辅助
# --------------------------------------------------------------------------- #


def ok(data: Any = None, **extra: Any) -> Response:
    """构造成功响应。

    Args:
        data: 业务数据。
        **extra: 附加到顶层的额外字段。

    Returns:
        JSON 响应。
    """
    payload: dict[str, Any] = {"ok": True, "data": data}
    payload.update(extra)
    return jsonify(payload)


def fail(message: str, status: int = 400, hint: str = "", **extra: Any) -> Response:
    """构造失败响应。

    Args:
        message: 错误描述。
        status: HTTP 状态码。
        hint: 可操作建议。
        **extra: 附加到顶层的额外字段。

    Returns:
        JSON 响应。
    """
    payload: dict[str, Any] = {"ok": False, "error": message}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    response = jsonify(payload)
    response.status_code = status
    return response


def _build_client() -> QQEssenceClient:
    """根据已保存的配置构造网络客户端。"""
    config = storage.load_config()
    cookie = CookieInfo.parse(config.get("cookie", ""))
    return QQEssenceClient(cookie)


def _public_config() -> dict[str, Any]:
    """生成对外暴露的配置视图（Cookie 已脱敏）。"""
    config = storage.load_config()
    cookie = CookieInfo.parse(config.get("cookie", ""))
    view = cookie.masked()
    view["updated_at"] = config.get("updated_at", "")
    return view


# --------------------------------------------------------------------------- #
# 路由注册
# --------------------------------------------------------------------------- #


def _register_routes(application: Flask) -> None:
    """注册全部 HTTP 路由。

    Args:
        application: Flask 实例。
    """

    # -- 首页 ------------------------------------------------------------- #

    @application.get("/")
    def index() -> Any:
        """返回前端单页。"""
        index_path = STATIC_DIR / "index.html"
        if not index_path.exists():
            return fail("前端文件缺失：static/index.html", status=500)
        return send_from_directory(str(STATIC_DIR), "index.html")

    # -- 配置 ------------------------------------------------------------- #

    @application.get("/api/config")
    def get_config() -> Response:
        """读取当前 Cookie 配置（脱敏）。"""
        return ok(_public_config())

    @application.post("/api/config")
    def set_config() -> Response:
        """保存 Cookie 配置。"""
        body = request.get_json(silent=True) or {}
        raw_cookie = body.get("cookie", "") if isinstance(body, dict) else ""
        if not isinstance(raw_cookie, str) or not raw_cookie.strip():
            return fail("Cookie 不能为空", hint="请从浏览器开发者工具中复制完整 Cookie 后粘贴")

        cookie = CookieInfo.parse(raw_cookie)
        if not cookie.raw:
            return fail("Cookie 格式无法识别", hint="期望形如 a=b; c=d 的 Cookie 字符串")
        if not cookie.has_cookie:
            return fail(
                "Cookie 中缺少 skey 或 p_skey",
                hint="请确认复制的是 qun.qq.com 域下的完整 Cookie（应包含 skey 与 p_skey）",
            )

        storage.save_config(cookie.raw, cookie.uin)
        return ok(_public_config())

    @application.post("/api/config/clear")
    def clear_config() -> Response:
        """清除已保存的 Cookie。"""
        storage.clear_config()
        return ok(_public_config())

    # -- 群列表 ----------------------------------------------------------- #

    @application.get("/api/groups")
    def get_groups() -> Response:
        """获取当前账号加入的群列表。

        接口在 Cookie 未通过鉴权时返回 ``ec != 0``，此处转换为友好错误并提示手动输入群号。
        """
        client = _build_client()
        try:
            groups = client.fetch_group_list()
        except QQClientError as exc:
            return fail(
                exc.message,
                status=502,
                hint=exc.hint or "可手动输入群号继续使用",
                groups=[],
            )
        return ok({"groups": groups, "count": len(groups)})

    # -- 抓取 ------------------------------------------------------------- #

    @application.post("/api/fetch")
    def fetch_snapshot() -> Response:
        """抓取精华消息并生成一个本地快照。"""
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return fail("请求体必须是 JSON 对象")

        gc = str(body.get("gc", "")).strip()
        if not GC_PATTERN.match(gc):
            return fail("群号格式不正确", hint="群号必须是 5-12 位数字，例如 123456789")

        # 显式传 0 时应当被 clamp 到 1，而不是被 `or` 吞成默认值 3。
        raw_max_pages = body.get("max_pages", None)
        if raw_max_pages is None or raw_max_pages == "":
            max_pages = DEFAULT_MAX_PAGES
        else:
            try:
                max_pages = int(raw_max_pages)
            except (TypeError, ValueError):
                max_pages = DEFAULT_MAX_PAGES
        max_pages = max(1, min(MAX_PAGES_LIMIT, max_pages))
        download_images = bool(body.get("download_images", False))

        client = _build_client()
        if not client.cookie.has_cookie:
            return fail("尚未配置 Cookie", hint="请先在左侧「连接」卡片粘贴 Cookie 并保存")

        try:
            result = client.fetch_essence(gc, max_pages=max_pages)
        except QQClientError as exc:
            return fail(exc.message, status=502, hint=exc.hint)

        extra_meta = result.to_meta()
        extra_meta["cookie_uin"] = client.cookie.uin
        meta = storage.create_snapshot(gc, result.messages, extra_meta)

        warnings = list(result.warnings)
        if download_images and result.messages:
            meta, download_stats = _run_download(meta["id"], result.messages, client)
            warnings.extend(_download_warnings(download_stats))

        detail = storage.load_snapshot(meta["id"])
        return ok(
            {
                "snapshot": detail["meta"],
                "messages": detail["messages"],
                "warnings": warnings,
            },
            warnings=warnings,
        )

    # -- 快照列表 / 详情 / 删除 ------------------------------------------- #

    @application.get("/api/snapshots")
    def get_snapshots() -> Response:
        """列出全部快照索引。"""
        entries = storage.list_snapshots()
        return ok({"snapshots": entries, "count": len(entries)})

    @application.get("/api/snapshots/<snapshot_id>")
    def get_snapshot(snapshot_id: str) -> Response:
        """读取单个快照详情。"""
        if not storage.is_valid_snapshot_id(snapshot_id):
            return fail("快照 ID 非法", status=400)
        try:
            detail = storage.load_snapshot(snapshot_id)
        except FileNotFoundError:
            return fail("快照不存在", status=404)
        except ValueError:
            return fail("快照 ID 非法", status=400)
        return ok({"snapshot": detail["meta"], "messages": detail["messages"]})

    @application.delete("/api/snapshots/<snapshot_id>")
    def delete_snapshot(snapshot_id: str) -> Response:
        """删除快照（含图片目录）。"""
        if not storage.is_valid_snapshot_id(snapshot_id):
            return fail("快照 ID 非法", status=400)
        try:
            removed = storage.delete_snapshot(snapshot_id)
        except ValueError:
            return fail("快照 ID 非法", status=400)
        if not removed:
            return fail("快照不存在", status=404)
        return ok({"id": snapshot_id, "deleted": True})

    # -- 图片下载 --------------------------------------------------------- #

    @application.post("/api/snapshots/<snapshot_id>/download")
    def download_snapshot_images(snapshot_id: str) -> Response:
        """下载该快照内的全部图片。"""
        if not storage.is_valid_snapshot_id(snapshot_id):
            return fail("快照 ID 非法", status=400)
        try:
            detail = storage.load_snapshot(snapshot_id)
        except FileNotFoundError:
            return fail("快照不存在", status=404)
        except ValueError:
            return fail("快照 ID 非法", status=400)

        client = _build_client()
        meta, stats = _run_download(snapshot_id, detail["messages"], client)
        refreshed = storage.load_snapshot(snapshot_id)
        return ok(
            {"stats": stats, "snapshot": refreshed["meta"]},
            warnings=_download_warnings(stats),
        )

    # -- 导出 ------------------------------------------------------------- #

    @application.get("/api/snapshots/<snapshot_id>/export")
    def export_snapshot(snapshot_id: str) -> Any:
        """导出快照为 html / md / json。"""
        if not storage.is_valid_snapshot_id(snapshot_id):
            return fail("快照 ID 非法", status=400)

        export_format = (request.args.get("format") or "html").strip().lower()
        if export_format not in EXPORT_FORMATS:
            return fail(
                "不支持的导出格式",
                hint="可选格式：html、md、json",
            )

        try:
            detail = storage.load_snapshot(snapshot_id)
        except FileNotFoundError:
            return fail("快照不存在", status=404)
        except ValueError:
            return fail("快照 ID 非法", status=400)

        meta = detail["meta"]
        messages = detail["messages"]
        image_map = _load_image_map(snapshot_id)

        if export_format == "json":
            payload = exporter.export_json(messages, meta)
            filename = exporter.default_filename(meta, "json")
            return _binary_response(payload, EXPORT_FORMATS["json"], filename)

        if export_format in ("md", "markdown"):
            text = exporter.export_markdown(messages, meta, image_map=image_map)
            filename = exporter.default_filename(meta, "md")
            return _text_response(text, EXPORT_FORMATS["md"], filename)

        documents = exporter.export_html(
            messages,
            meta,
            images_dir=storage.snapshot_dir(snapshot_id) / "images",
            image_map=image_map,
        )
        filename = exporter.default_filename(meta, "html")
        return _text_response(documents, EXPORT_FORMATS["html"], filename)

    # -- 媒体文件 --------------------------------------------------------- #

    @application.get("/media/<snapshot_id>/<path:filename>")
    def serve_media(snapshot_id: str, filename: str) -> Any:
        """提供快照内图片的静态访问。"""
        if not storage.is_valid_snapshot_id(snapshot_id):
            return fail("快照 ID 非法", status=400)
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            return fail("文件名非法", status=400)
        try:
            directory = storage.snapshot_dir(snapshot_id) / "images"
        except ValueError:
            return fail("快照 ID 非法", status=400)
        if not directory.exists():
            return fail("图片不存在", status=404)
        return send_from_directory(str(directory), filename)


def _text_response(content: str, mimetype: str, filename: str) -> Response:
    """构造带下载头的文本响应。"""
    response = Response(content, mimetype=mimetype)
    response.headers["Content-Disposition"] = exporter.content_disposition(filename)
    response.headers["Cache-Control"] = "no-store"
    return response


def _binary_response(content: bytes, mimetype: str, filename: str) -> Response:
    """构造带下载头的二进制响应。"""
    response = Response(content, mimetype=mimetype)
    response.headers["Content-Disposition"] = exporter.content_disposition(filename)
    response.headers["Cache-Control"] = "no-store"
    return response


def _load_image_map(snapshot_id: str) -> dict[str, str]:
    """读取快照的「远程 URL → 本地文件名」映射。"""
    try:
        detail = storage.load_snapshot(snapshot_id)
    except (FileNotFoundError, ValueError):
        return {}
    local_images = detail["meta"].get("local_images", {})
    if not isinstance(local_images, dict):
        return {}
    mapping: dict[str, str] = {}
    for url, media_path in local_images.items():
        name = str(media_path).rsplit("/", 1)[-1]
        if name:
            mapping[str(url)] = name
    return mapping


def _run_download(
    snapshot_id: str,
    messages: list[dict[str, Any]],
    client: QQEssenceClient,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """执行一次图片下载并更新快照 meta。

    Args:
        snapshot_id: 快照 ID。
        messages: 快照内消息列表。
        client: 网络客户端（复用 UA 配置）。

    Returns:
        ``(更新后的 meta, 下载统计字典)``。
    """
    urls = downloader.collect_image_urls(messages)
    report = downloader.download_images(
        urls,
        storage.images_dir(snapshot_id),
        concurrency=downloader.DEFAULT_CONCURRENCY,
        user_agent=client.user_agent,
    )
    if report.mapping:
        storage.merge_image_map(snapshot_id, report.mapping)

    stats = report.to_dict()
    stats.pop("mapping", None)
    meta = storage.update_meta(
        snapshot_id,
        {
            "downloaded_images": report.success + report.skipped,
            "download_failed": report.failed_items,
            "download_total": report.total,
        },
    )
    return meta, stats


def _download_warnings(stats: dict[str, Any]) -> list[str]:
    """把下载统计转换成人话提示。"""
    warnings: list[str] = []
    if stats.get("total", 0) == 0:
        warnings.append("该快照没有图片需要下载")
        return warnings
    if stats.get("failed", 0):
        warnings.append(f"{stats['failed']} 张图片下载失败（可能是链接已失效或群被封）")
    if stats.get("success", 0):
        warnings.append(f"成功下载 {stats['success']} 张图片")
    if stats.get("skipped", 0):
        warnings.append(f"{stats['skipped']} 张图片已存在，已跳过")
    return warnings


# --------------------------------------------------------------------------- #
# 全局错误处理
# --------------------------------------------------------------------------- #


def _register_error_handlers(application: Flask) -> None:
    """注册全局异常处理器。

    Args:
        application: Flask 实例。
    """

    @application.errorhandler(QQClientError)
    def handle_client_error(exc: QQClientError) -> Any:
        """处理客户端异常。"""
        application.logger.warning("QQClientError: %s", exc.message)
        return fail(exc.message, status=502, hint=exc.hint)

    @application.errorhandler(404)
    def handle_not_found(exc: Any) -> Any:
        """处理 404。"""
        if request.path.startswith("/api/"):
            return fail("接口不存在", status=404)
        return fail("资源不存在", status=404)

    @application.errorhandler(405)
    def handle_method_not_allowed(exc: Any) -> Any:
        """处理 405。"""
        return fail("请求方法不允许", status=405)

    @application.errorhandler(Exception)
    def handle_unexpected(exc: Any) -> Any:
        """兜底处理未捕获异常。"""
        application.logger.exception("未捕获异常: %s", exc)
        return fail(f"服务器内部错误：{exc.__class__.__name__}", status=500)


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #


def find_available_port(start_port: int, attempts: int = MAX_PORT_ATTEMPTS) -> int:
    """从 ``start_port`` 开始寻找一个可用端口。

    Args:
        start_port: 起始端口。
        attempts: 最多尝试次数。

    Returns:
        可用端口号。

    Raises:
        OSError: 在尝试范围内找不到可用端口。
    """
    for offset in range(attempts):
        candidate = start_port + offset
        if candidate > 65535:
            break
        # 注意：这里刻意**不设置** SO_REUSEADDR。
        # 在 Windows 上 SO_REUSEADDR 允许绑定到已被占用的地址（语义等同 Unix 的
        # SO_REUSEPORT），会导致「端口探测永远成功」而实际启动失败。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((HOST, candidate))
                probe.listen(1)
            except OSError:
                continue
            return candidate
    raise OSError(f"在 {start_port}-{start_port + attempts - 1} 范围内找不到可用端口")


def main(argv: Optional[list[str]] = None) -> int:
    """命令行入口。

    Args:
        argv: 命令行参数（默认取 ``sys.argv[1:]``）。

    Returns:
        进程退出码。
    """
    arg_parser = argparse.ArgumentParser(description="QQ 群精华消息本地存档应用")
    arg_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 5000（被占用时自动递增）")
    arg_parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parsed = arg_parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    application = app

    try:
        port = find_available_port(parsed.port)
    except OSError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    if port != parsed.port:
        application.logger.warning("端口 %s 已被占用，改用 %s", parsed.port, port)

    url = f"http://{HOST}:{port}/"
    print("=" * 62)
    print("  QQ 群精华消息 · 本地存档")
    print(f"  访问地址：{url}")
    print(f"  数据目录：{storage.DATA_DIR}")
    print("  按 Ctrl+C 退出")
    print("=" * 62)

    if not parsed.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 打开浏览器失败不影响服务
            pass

    application.run(host=HOST, port=port, debug=False, use_reloader=False, threaded=True)
    return 0


app = create_app()


if __name__ == "__main__":
    raise SystemExit(main())
