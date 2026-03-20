from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from routers.tasks import router as tasks_router
from routers.conversations import router as conversations_router
from auth.session import SESSION_COOKIE, ensure_session_id, get_session_id_from_request
from storage.sqlite_store import SqliteStore
from maintenance.cleanup import cleanup_expired_conversations


def _setup_progress_logging() -> None:
    """配置 agent.progress logger，将 AI 工作与命令执行进度写入日志文件。"""
    logger = logging.getLogger("agent.progress")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        log_path = settings.progress_log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)

    # 不向上冒泡，避免重复输出到其它 handler
    logger.propagate = False


def create_app() -> FastAPI:
    _setup_progress_logging()

    # 创建 FastAPI 应用实例，title 主要用于文档页面展示
    app = FastAPI(title="AIBuilder Python Backend")

    # CORS：默认放开，保持与原 Express 后端类似的宽松策略，
    # 方便前端在本地开发时，从任意端口直接访问该后端。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _anonymous_session_cookie_middleware(request: Request, call_next):
        """为无登录态用户签发匿名 session_id cookie。

        设计目标：
        - 前端无 token，只能靠 cookie 隔离 conversation。
        - middleware 保证所有 HTTP 请求都具备 session cookie（缺失则写入）。
        - WebSocket 不经过 http middleware，因此 WS handler 仍需要额外从 cookie header 校验 session_id。
        """

        existing = get_session_id_from_request(request)
        response: Response = await call_next(request)
        if not existing:
            # 注意：这里不依赖 request.cookies，因为它在新 cookie 设置前仍为空。
            session_id = ensure_session_id(existing)
            response.set_cookie(
                key=SESSION_COOKIE.cookie_name,
                value=session_id,
                max_age=SESSION_COOKIE.max_age_seconds,
                httponly=SESSION_COOKIE.http_only,
                samesite=SESSION_COOKIE.same_site,
            )
        return response

    @app.get("/health")
    def health() -> dict:
        # 简单健康检查接口，供前端或运维探活使用
        return {
            "status": "ok",
            "workspaceRoot": str(settings.workspace_root),
        }

    # 显式初始化 SQLite schema（用于满足 db-migration-init-on-startup 语义）
    # 注意：routers/conversations.py 也会创建 store 实例，但这里仍保留显式初始化与日志。
    try:
        _ = SqliteStore(settings.db_path)
        logging.getLogger("db").info("SQLite store initialized")
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("db").exception("Failed to init SQLite store: %r", exc)

    # 可选：启动时执行一次过期清理（避免无穷增长）
    if settings.cleanup_on_startup:
        try:
            deleted = cleanup_expired_conversations(
                db_path=settings.db_path,
                workspace_root=settings.workspace_root,
                ttl_days=settings.conversation_ttl_days,
            )
            logging.getLogger("db").info("Cleanup on startup: deleted=%s", deleted)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("db").exception("Cleanup on startup failed: %r", exc)

    app.include_router(tasks_router)
    app.include_router(conversations_router)

    @app.post("/maintenance/cleanup")
    def maintenance_cleanup() -> dict[str, object]:
        """手动触发过期 conversation 清理（删除 DB 数据 + 对应生成目录）。"""

        try:
            deleted = cleanup_expired_conversations(
                db_path=settings.db_path,
                workspace_root=settings.workspace_root,
                ttl_days=settings.conversation_ttl_days,
            )
            return {"status": "ok", "deleted": deleted}
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("db").exception("cleanup failed: %r", exc)
            return JSONResponse(status_code=500, content={"error": str(exc)})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        # 统一未捕获错误处理，避免泄露内部实现细节。
        # 实际生产环境可以对这里接入更完整的日志系统，例如 Sentry / 日志聚合服务等。
        print("[AIBuilder_backend_python] Unhandled error:", repr(exc))
        return JSONResponse(status_code=500, content={"error": "Internal Server Error"})

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=True)

