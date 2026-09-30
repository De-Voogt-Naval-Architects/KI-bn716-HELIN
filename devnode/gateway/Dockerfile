FROM python:3.12-slim
RUN pip install --no-cache-dir paho-mqtt==2.1.0
COPY gateway.py /gateway.py
USER 1000
CMD ["python", "/gateway.py"]
