FROM mcr.microsoft.com/azure-cli:2.92.0
RUN tdnf install -y openssh-clients && tdnf clean all \
    && az extension add --name bastion --version 1.4.3 --yes \
    && python3 -m venv /opt/harness
WORKDIR /lab
COPY harness/requirements.txt /tmp/harness-requirements.txt
COPY wheelhouse /wheelhouse
ARG PIP_NO_INDEX=false
ARG PIP_FIND_LINKS=/wheelhouse
RUN /opt/harness/bin/pip install --no-cache-dir -r /tmp/harness-requirements.txt
COPY harness /lab/harness
COPY swugen /lab/generator/swugen
COPY profiles /lab/profiles
COPY rules /lab/rules
COPY infra/tables.json /lab/infra/tables.json
COPY harness/ssh_config /root/.ssh/config
RUN chmod 700 /root/.ssh && chmod 600 /root/.ssh/config
ENV SWU_ATTACK_SSH=swu-attack SWU_GATEWAY_SSH=swu-gateway
HEALTHCHECK --interval=10s --timeout=3s --start-period=180s --retries=3 CMD test -f /state/ready
ENTRYPOINT ["python3", "/lab/harness/private_operator.py"]
