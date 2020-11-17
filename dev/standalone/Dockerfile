FROM localhost/galaxy_ng/galaxy_ng:base

COPY requirements/requirements.standalone.txt /tmp/requirements.standalone.txt

USER root

RUN set -ex; \
    touch /var/lib/rpm/*; \
    pip install --no-cache-dir --requirement /tmp/requirements.standalone.txt; \
    dnf -y install wget \
    && cd /etc/yum.repos.d/ \
    && wget https://download.opensuse.org/repositories/devel:/kubic:/libcontainers:/stable/CentOS_8/devel:kubic:libcontainers:stable.repo \
    && dnf -y install \
        fuse-overlayfs \
        podman \
        slirp4netns

USER galaxy
