FROM python:3.9.25

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Fail at build time if no env file was copied (plain docker run has no --env-file)
RUN test -f .env -o -f .env.prod || (echo "ERROR: copy .env or .env.prod into the build context" >&2 && exit 1)

RUN chmod +x entrypoint.sh

EXPOSE 4000

CMD ["./entrypoint.sh"]
