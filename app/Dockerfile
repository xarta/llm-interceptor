FROM python:3.12-alpine

WORKDIR /app

# Build deps for any C extensions (pyyaml can need them)
RUN apk add --no-cache gcc musl-dev

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Remove build deps to keep image small
RUN apk del gcc musl-dev

COPY proxy.py .

EXPOSE 8080

CMD ["uvicorn", "proxy:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--loop", "uvloop"]
