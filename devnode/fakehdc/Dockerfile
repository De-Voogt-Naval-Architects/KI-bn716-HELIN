# Fake Helin Data Collector for the dev node (build context: repo root).
FROM python:3.12-slim
RUN pip install --no-cache-dir pg8000==1.31.5
COPY module/tests/fake_hdc.py /fake_hdc.py
USER 1000
ENTRYPOINT ["python", "/fake_hdc.py"]
