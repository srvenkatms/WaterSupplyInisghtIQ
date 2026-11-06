FROM debian:trixie-20251020
ARG OPENPLC_REVISION=b5d41356dab4aeadca0dd7ca64ba542f870b595d
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3 \
    && git clone https://github.com/thiagoralves/OpenPLC_v3.git /opt/openplc \
    && cd /opt/openplc && git checkout "$OPENPLC_REVISION" \
    && ./background_installer.sh docker
WORKDIR /opt/openplc/webserver
COPY tank_control.st st_files/tank_control.st
COPY hardware.cpp core/hardware_layer.cpp
RUN cp core/debug.blank core/debug.cpp \
    && echo linux > scripts/openplc_platform && echo blank_linux > scripts/openplc_driver \
    && ./scripts/compile_program.sh tank_control.st
COPY start.py /opt/swu/start.py
ENV SWU_SENSOR_FILE=/sensors/inputs.bin
EXPOSE 1502
HEALTHCHECK --interval=5s --timeout=3s --start-period=30s CMD python3 -c "import os,socket; assert os.path.isfile('/tmp/swu-ready'); socket.create_connection(('127.0.0.1',1502),2).close()"
ENTRYPOINT ["python3", "/opt/swu/start.py"]
