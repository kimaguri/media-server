FROM python:3.12-alpine

RUN apk add --no-cache docker-cli docker-cli-compose curl

WORKDIR /wizard
COPY api.py index.html ./

EXPOSE 8888

CMD ["python3", "api.py"]
