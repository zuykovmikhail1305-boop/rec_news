FROM python:3.12

WORKDIR /usr/src/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python", "-m", "uvicorn", "web_app.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
