# A local lab that mirrors production: blossoML and blossoml-ops on one
# Ubuntu 24.04 host with systemd, set up by following blossoml_ops/deploy/README.md.
# Build context: the repository root.   See local/README.md.

ARG BLOSSOML_REF=64b236c
ARG PYTHON_VERSION=3.12

# ---- 1. blossoML (same steps as its deploy.yml, plus our 0640 patch) ----------
FROM ocaml/opam:ubuntu-24.04-ocaml-5.2 AS blossoml
ARG BLOSSOML_REF
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgmp-dev libffi-dev pkg-config libsqlite3-dev libsecp256k1-dev libssl-dev git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
USER opam
WORKDIR /home/opam
RUN git clone https://github.com/kengirie/blossoML.git && git -C blossoML checkout -q ${BLOSSOML_REF}
COPY --chown=opam blossoml-patches /home/opam/patches
RUN git -C blossoML apply /home/opam/patches/*.patch
# --confirm-level=unsafe-yes lets opam install any remaining system packages (depexts) itself.
RUN opam update -y && cd blossoML && opam install . --deps-only -y --confirm-level=unsafe-yes
RUN cd blossoML && opam exec -- dune build bin/main.exe

# ---- 2. the agent runtime (same steps as the CI build job) -------------------
FROM mambaorg/micromamba:2-ubuntu24.04 AS agent
ARG PYTHON_VERSION
USER root
RUN micromamba create -y -p /opt/runtime -c conda-forge "python=${PYTHON_VERSION}" "spot>=2.14" pip \
 && micromamba create -y -p /opt/pack -c conda-forge conda-pack \
 && micromamba clean -a -y
COPY ltlguard /src/ltlguard
COPY blossoml_ops /src/blossoml_ops
RUN rm -rf /src/*/*.egg-info /src/*/build \
 && /opt/runtime/bin/python -m pip install --no-cache-dir /src/ltlguard /src/blossoml_ops \
 && /opt/runtime/bin/python -m ltlguard check /src/blossoml_ops/blossoml_ops/rules.yaml
RUN mkdir /dist && /opt/pack/bin/conda-pack -q -p /opt/runtime -o /dist/env.tar.gz --ignore-missing-files \
 && cd /dist && echo lab > VERSION && sha256sum env.tar.gz VERSION > SHA256SUMS \
 && tar -cf /blossoml-ops-lab.tar env.tar.gz VERSION SHA256SUMS

# ---- 3. the host -------------------------------------------------------------
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive container=docker
RUN apt-get update && apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus acl sudo tinyproxy nftables ca-certificates curl sqlite3 python3 \
      libgmp10 libffi8 libsqlite3-0 libsecp256k1-dev libssl3t64 less \
    && rm -rf /var/lib/apt/lists/* \
    && find /etc/systemd/system /lib/systemd/system -path '*.wants/*' \
         \( -name '*getty*' -o -name '*udev*' -o -name 'systemd-remount-fs*' \) -delete
# The deploy user, like production: blossoML runs as ubuntu, which can sudo.
RUN echo 'ubuntu ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/ubuntu && chmod 0440 /etc/sudoers.d/ubuntu

COPY --from=blossoml /home/opam/blossoML/_build/default/bin/main.exe /home/ubuntu/blossoML/main.exe
COPY blossoml_ops/deploy /opt/lab/deploy
COPY local/blossoml.service /etc/systemd/system/blossoml.service
COPY local/provision.sh local/lab-init.sh local/seed.py /opt/lab/
COPY local/lab-init.service /etc/systemd/system/lab-init.service
COPY --from=agent /blossoml-ops-lab.tar /opt/lab/blossoml-ops-lab.tar
# Fail the build, not the first start, if main.exe misses a shared library.
RUN ! ldd /home/ubuntu/blossoML/main.exe | grep -q "not found"
RUN bash /opt/lab/provision.sh

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
