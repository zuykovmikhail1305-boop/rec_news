FROM python:3.12

WORKDIR /usr/src/app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_NO_DEV=1

COPY pyproject.toml uv.lock ./
RUN uv sync --locked

COPY . .

ENTRYPOINT uv run backend/main.py
