FROM jrottenberg/ffmpeg:4.1-alpine

RUN apk add --no-cache python3 && \
    python3 -m ensurepip --upgrade && \
    rm -r /usr/lib/python*/ensurepip && \
    pip3 install --no-cache --upgrade pip setuptools

COPY requirements.txt /
RUN pip3 install --no-cache-dir -r /requirements.txt

COPY syno_telegram_gifs.py /

VOLUME /config /gifs /data
ENV PATH /usr/local/bin:$PATH
ENV LANG C.UTF-8

ENTRYPOINT ["/usr/bin/env"]
CMD ["python3", "/syno_telegram_gifs.py", "/config/config.json"]