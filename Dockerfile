FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY yuna_bot.py config.json ./

RUN useradd --create-home --shell /usr/sbin/nologin bot
USER bot

CMD ["python", "yuna_bot.py"]
