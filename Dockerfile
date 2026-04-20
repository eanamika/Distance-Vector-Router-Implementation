FROM alpine:latest

RUN apk add --no-cache python3 iproute2

ENV PYTHONUNBUFFERED=1

COPY router.py /app/router.py

WORKDIR /app

CMD ["python3", "router.py"]