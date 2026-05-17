# syntax=docker/dockerfile:1
FROM python:3.11-slim
WORKDIR /code
COPY ./requirements.txt /code/requirements.txt
RUN pip3 install -r requirements.txt
CMD [ "python3", "main.py"]

