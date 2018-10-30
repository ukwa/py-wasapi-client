FROM python:3-alpine

WORKDIR /usr/src/py-wasapi-client

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python setup.py install

CMD wasapi-client

