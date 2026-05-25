FROM python:3.9.25

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN test -f .env -o -f .env.prod || (echo "ERROR: copy .env or .env.prod into the build context" >&2 && exit 1)

EXPOSE 4000

CMD ["python", "-u", "api.py"]
