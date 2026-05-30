FROM python:3.12-slim

ENV LANG C.UTF-8

# Copy data for add-on
COPY *.py run.sh /

# Install requirements for add-on
RUN python3 -m pip install --no-cache-dir pyserial-asyncio aiomqtt

WORKDIR /share

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
