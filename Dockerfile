FROM python:3.9.25

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN chmod +x entrypoint.sh

EXPOSE 4000

CMD ["./entrypoint.sh"]
