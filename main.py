from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from routers.tasks import router as tasks_router


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
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict:
        # 简单健康检查接口，供前端或运维探活使用
        return {
            "status": "ok",
            "workspaceRoot": str(settings.workspace_root),
        }

    app.include_router(tasks_router)

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

