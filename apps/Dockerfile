# One image for every Utility A role; the container command picks the role.
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
COPY wheelhouse /wheelhouse
ARG PIP_NO_INDEX=false
ARG PIP_FIND_LINKS=/wheelhouse
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /wheelhouse
COPY swugen ./swugen
COPY profiles ./profiles
COPY swuapps ./swuapps
ENV PYTHONUNBUFFERED=1
ENV SWU_PROFILE_DIR=/app/profiles
CMD ["python", "-m", "swuapps.gateway"]
