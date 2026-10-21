from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from models import BusinessCategory, FsoExecution, PreviousSector, ServiceModel, User, UserProfile
from database import engine, Base
from routes import router

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Alfa Hack — Compact Registration")

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)