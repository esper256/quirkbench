FROM registry.fedoraproject.org/fedora:43 AS qb-load
RUN dnf -y install cargo gcc curl tar glslang python3 && dnf clean all
WORKDIR /build
RUN curl --fail --location --retry 3 https://gitlab.freedesktop.org/mesa/vkrunner/-/archive/4dff5e7f450d59e24be5b11f1898d2c3091d908b/vkrunner-4dff5e7f450d59e24be5b11f1898d2c3091d908b.tar.gz -o source.tar.gz && \
    echo 'b5b5d68ab4e47712c174b4688af4e5bb8ffb27e9d44bc6ae3d6d8bbd3b6c03a7  source.tar.gz' | sha256sum -c - && \
    tar --strip-components=1 -xf source.tar.gz && cargo build --release --offline
COPY load/scene.shader_test /build/scene.shader_test
RUN mkdir /out && python3 precompile-script.py -o /out/scene.shader_test scene.shader_test && \
    cp target/release/vkrunner /out/vkrunner && cp COPYING /out/COPYING
