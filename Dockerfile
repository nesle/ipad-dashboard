FROM python:3.12-alpine
WORKDIR /app
RUN adduser -D -u 10001 dashboard
COPY app/ /app/
USER dashboard
EXPOSE 8080
CMD ["python", "server.py"]
