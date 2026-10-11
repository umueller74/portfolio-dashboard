FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY portfolio ./portfolio
COPY fetcher ./fetcher
RUN pip install --no-cache-dir . && useradd -u 1000 -m app && mkdir /data && chown 1000 /data
USER 1000
ENV DB_PATH=/data/portfolio.db
EXPOSE 8000
CMD ["uvicorn", "portfolio.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
