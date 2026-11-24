FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

#RUN yum install -y bash
RUN pip install requests

WORKDIR /app
COPY bluesky_collector.py keywords.txt ./
RUN useradd --create-home appuser && mkdir /data && chown appuser /data
#USER appuser
ENTRYPOINT ["python", "/app/bluesky_collector.py"]
CMD ["--lang", "en", "--max_per_query", "200"]
