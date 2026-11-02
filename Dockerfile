FROM python:3.12-slim
WORKDIR /app
COPY bot.py app.py ./
RUN mkdir -p /app/data
ENV PORT=8080 MODE=paper DATA_DIR=/app/data PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "app.py"]
