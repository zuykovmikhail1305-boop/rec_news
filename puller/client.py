import requests
from os import getenv
from time import sleep, time

URL = getenv('PULLER_URL', None)
if URL is None:
    raise ValueError('Url is not specified')
TOKEN = getenv('PULLER_KEY', None)
if TOKEN is None:
    raise ValueError('Token is not specified')
TIMEOUT = getenv('PULLER_TIMEOUT', None)
if TIMEOUT is not None:
    TIMEOUT = int(TIMEOUT)
else:
    TIMEOUT = 15*60

req = requests.post(f'{URL}/puller', headers={'Token': TOKEN})
if req.status_code != 200:
    print(req.json()['detail'])
    exit(-1)

timestamp = int(req.text)
begin = time()

while True:
    if time() - begin > TIMEOUT:
        print('Client timeout')
        exit(-1)

    req = requests.get(f'{URL}/puller/{timestamp}', headers={'Token': TOKEN})
    if req.status_code == 200:
        print('Success')
        break

    if req.status_code != 202:
        print(req.json()['detail'])
        exit(-1)

    sleep(1)
    