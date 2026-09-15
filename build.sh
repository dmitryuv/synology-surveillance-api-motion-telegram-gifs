#!/bin/bash

rm syno_telegram_gifs.tar

docker buildx build --platform linux/amd64 -t syno/syno_telegram_gifs .

docker save syno/syno_telegram_gifs >syno_telegram_gifs.tar