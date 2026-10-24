from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

if __package__:
    from .routes import router
else:
    from routes import router

app = FastAPI(title="Бизнес Онлайн — инсайты", version="0.3.0")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
