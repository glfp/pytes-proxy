FROM python:3.12-slim

WORKDIR /app
COPY pytes_service.py /app/pytes_service.py

EXPOSE 8080
ENV HTTP_PORT=8080
CMD ["python", "-u", "/app/pytes_service.py"]