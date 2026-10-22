# Preserve upstream binary/dependencies; expose the executable to arbitrary non-root UIDs.
FROM ghcr.io/erebe/wstunnel@sha256:e2b62fac898406c73342e8e50fc21f7e221c17811aad532ed3a80a2e3c327b79
USER 0:0
RUN cp /home/app/wstunnel /usr/local/bin/wstunnel && chmod 0555 /usr/local/bin/wstunnel
COPY LICENSE.wstunnel /usr/share/doc/wstunnel/LICENSE
LABEL org.opencontainers.image.source="https://github.com/enthus-appdev/pcai-inference-probe" \
      org.opencontainers.image.title="pcai-probe-wireguard-https" \
      org.opencontainers.image.licenses="BSD-3-Clause"
ENV RUST_LOG=WARN NO_COLOR=true
WORKDIR /
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/wstunnel"]
CMD ["--help"]
