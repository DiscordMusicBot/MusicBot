FROM alpine:edge

LABEL maintainer "TBK <tbk@jjtc.eu>"

# Add project source
WORKDIR /usr/src/musicbot
COPY . .

# Install Dependencies
RUN apk add --update \
&& apk add --no-cache python3 ffmpeg opus ca-certificates \
&& apk add --no-cache --virtual .build-deps git python3-dev libffi-dev libsodium-dev musl-dev gcc make \
\
# Install pip dependencies
&& pip3 install --no-cache-dir -r requirements.txt && pip3 install --upgrade --force-reinstall https://github.com/DiscordMusicBot/websockets/archive/deploy.zip \
\
# Clean up build dependencies
&& apk del .build-deps

# Create volume for mapping the config
VOLUME /usr/src/musicbot/config

ENTRYPOINT ["python3", "run.py"]
