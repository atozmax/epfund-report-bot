FROM python:3.9.25

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# App code + env (main.py loads /app/.env via python-dotenv)
COPY .env .env
COPY . .

CMD ["python", "-u", "main.py"]
