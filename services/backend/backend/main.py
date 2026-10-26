import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

if __package__:
    from .routes import router
else:
    from routes import router

@asynccontextmanager
async def lifespan(app):
    if __package__:
        from .processing import ProcessingConfig, processing_loop
    else:
        from backend.processing import ProcessingConfig, processing_loop
    config = ProcessingConfig.from_env()
    stop = asyncio.Event()
    app.state.processing = {"enabled": config.enabled, "status": "starting" if config.enabled else "disabled"}
    task = asyncio.create_task(processing_loop(config, stop, app.state.processing)) if config.enabled else None
    try:
        yield
    finally:
        stop.set()
        if task:
            await task


app = FastAPI(lifespan=lifespan, title="Бизнес Онлайн — инсайты", version="0.3.0")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
