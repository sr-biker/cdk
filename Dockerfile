FROM ghcr.io/runatlantis/atlantis:latest

USER root

RUN apk add --no-cache python3 py3-pip nodejs npm && \
    npm install -g aws-cdk && \
    ln -sf python3 /usr/bin/python

USER atlantis
