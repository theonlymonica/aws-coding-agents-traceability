# Strands Box, built from one commit of its repository: the commit has the listener for a program inside a box
# (`[[agent.ingress]]`) and the default credential chain of the AWS SDK (`aws://`), which the release 0.2.2
# does not have. The build needs the Rust toolchain of the repository (rust-toolchain.toml).
FROM public.ecr.aws/amazonlinux/amazonlinux:2023 AS box-build
RUN dnf install -y gcc gcc-c++ make cmake openssl-devel pkgconf-pkg-config git curl-minimal tar gzip perl python3 && dnf clean all
ARG BOX_REV=70eb3afcecfbe435371a60d557c6644eb83c0a06
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh \
    && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain 1.98.1
ENV PATH="/root/.cargo/bin:${PATH}"
RUN git init /src && git -C /src remote add origin https://github.com/strands-agents/box.git \
    && git -C /src fetch --depth 1 origin ${BOX_REV} && git -C /src checkout --detach FETCH_HEAD \
    && test "$(git -C /src rev-parse HEAD)" = "${BOX_REV}"
WORKDIR /src
RUN cargo build --locked --release --all-features -p strands-box -p strands-box-containment
RUN mkdir -p /out && cp target/release/strands-box /out/box \
    && cp target/release/strands-box-sock-alias target/release/strands-box-contain-trampoline /out/ \
    && git rev-parse HEAD > /out/box.revision

FROM public.ecr.aws/docker/library/python:3.13-slim

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.txt

# Strands Box, from the build stage. An agent runtime with the setting AGENT_BOX runs the work of a session, or
# the complete agent, inside a box (boxed.py).
COPY --from=box-build /out/ /opt/box/

COPY *.py /app/
COPY box /app/box/

# The agent runs without root. The image contains no code base and no git client.
RUN useradd --create-home agent
USER agent
WORKDIR /home/agent
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/app/main.py"]
