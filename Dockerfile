# Larkspur Clinic Ops: the self-hosted test target. Builds the React skin, then serves everything
# from one Python process.
FROM node:22-alpine AS web
WORKDIR /web
COPY clinic/modern/package.json clinic/modern/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY clinic/modern/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY clinic/requirements.txt ./clinic-requirements.txt
RUN pip install -r clinic-requirements.txt
COPY clinic/ ./clinic/
COPY --from=web /web/dist ./clinic/modern/dist
EXPOSE 8100
CMD ["sh", "-c", "uvicorn clinic.app:app --host 0.0.0.0 --port ${PORT:-8100}"]
