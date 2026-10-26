FROM python:3.12

WORKDIR /usr/src/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV BACKEND_API_HOST=0.0.0.0 BACKEND_API_PORT=8000

ENTRYPOINT ["python", "scripts/run_api.py"]