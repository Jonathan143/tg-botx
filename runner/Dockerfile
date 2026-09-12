FROM python:3.13-slim
COPY --chmod=0444 runner/entrypoint.py /runner/entrypoint.py
USER 65534:65534
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "-B", "/runner/entrypoint.py"]
