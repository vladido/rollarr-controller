FROM python:alpine
ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision="$VCS_REF"
RUN addgroup -g 1000 rollarr && adduser -D -u 1000 -G rollarr rollarr
WORKDIR /app
COPY rollarr ./rollarr
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/live',timeout=3)"
CMD ["python", "-m", "rollarr"]
