FROM python:3.12

WORKDIR /usr/src/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PARSER_API_HOST=0.0.0.0 PARSER_API_PORT=8001

ENTRYPOINT ["python", "scripts/run_api.py"]
