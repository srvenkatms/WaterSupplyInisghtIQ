FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /lab
COPY requirements.txt ./
COPY wheelhouse /wheelhouse
RUN pip install --no-cache-dir --no-index --find-links=/wheelhouse -r requirements.txt && rm -r /wheelhouse
COPY apps ./apps
COPY generator ./generator
COPY profiles ./profiles
COPY console-readiness.json ./
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "--pythonpath", "apps,generator", "--workers", "1", "--threads", "4", "--timeout", "120", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-", "swuapps.console:create_app()"]
