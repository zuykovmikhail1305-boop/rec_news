from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import models
from .database import engine
from .routes import router

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Alfa Hack — Compact Registration")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)
