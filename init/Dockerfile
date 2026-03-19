FROM alpine:3.19

RUN apk add --no-cache bash curl python3 py3-yaml grep sed docker-cli

COPY setup.sh /setup.sh
RUN chmod +x /setup.sh

ENTRYPOINT ["/setup.sh"]
