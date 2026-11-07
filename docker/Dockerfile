# equiv-env Docker image
# Ubuntu 24.04 with pinned gcc, clang, CBMC, Python 3, valgrind
#
# Build:
#   docker build -t equiv-env -f docker/Dockerfile .
# Run:
#   docker run -it --rm -v $(pwd):/workspace equiv-env bash

FROM ubuntu:24.04

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# System packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc-14 \
    g++-14 \
    clang-18 \
    llvm-18 \
    python3.12 \
    python3.12-venv \
    python3-pip \
    valgrind \
    curl \
    ca-certificates \
    time \
    && rm -rf /var/lib/apt/lists/*

# Set gcc-14 and clang-18 as defaults
RUN update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-14 100 \
    && update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-14 100 \
    && update-alternatives --install /usr/bin/clang clang /usr/bin/clang-18 100

# Install CBMC from GitHub releases
# Pin: CBMC 6.11.0 for Ubuntu 24.04
ARG CBMC_VERSION=6.11.0
ARG CBMC_DEB=ubuntu-24.04-cbmc-${CBMC_VERSION}-Linux.deb
RUN curl -L -o /tmp/${CBMC_DEB} \
    "https://github.com/diffblue/cbmc/releases/download/cbmc-${CBMC_VERSION}/${CBMC_DEB}" \
    && dpkg -i /tmp/${CBMC_DEB} \
    && rm /tmp/${CBMC_DEB}

# Verify installations
RUN gcc --version \
    && clang --version \
    && cbmc --version \
    && python3.12 --version \
    && valgrind --version

# Set up Python
RUN python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir \
    pyyaml>=6.0 \
    zstandard>=0.22 \
    pytest>=7.0

# Working directory
WORKDIR /workspace

# Smoke test: copy golden tests and run
COPY tests/golden/000_smoke/ /tmp/smoke/
RUN cbmc --function harness /tmp/smoke/test_equiv.c \
    && echo "Smoke test (equiv): PASS" \
    && cbmc --function harness --trace /tmp/smoke/test_neq.c 2>&1 | grep -q "VERIFICATION FAILED" \
    && echo "Smoke test (neq): PASS" \
    || (echo "Smoke test FAILED" && exit 1)

CMD ["bash"]