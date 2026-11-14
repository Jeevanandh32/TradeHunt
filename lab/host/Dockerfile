# Exchange and subscriber hosts: Python plus the Linux tools a support engineer reaches for.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 iputils-ping tcpdump iptables procps net-tools \
 && rm -rf /var/lib/apt/lists/*
CMD ["sleep", "infinity"]
