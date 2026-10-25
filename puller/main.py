from os import getenv
from fastapi import FastAPI, Depends
from fastapi.security import APIKeyHeader
from exceptions import TaskRunningException
from exceptions import InvalidTimestampException
from exceptions import TaskExecutionException
from exceptions import UnauthenticatedException
from task import Task
from history import History

COMMAND = getenv('PULLER_COMMAND', None)
if COMMAND is None:
    raise ValueError('Command is not specified')
TIMEOUT = getenv('PULLER_TIMEOUT', None)
if TIMEOUT is not None:
    TIMEOUT = int(TIMEOUT)
KEY = getenv('PULLER_KEY', None)
if KEY is None:
    raise ValueError('API key is not specified')

app = FastAPI()
key_header = APIKeyHeader(name='Token')


@app.post("/puller")
def task_start(key: str = Depends(key_header)):
    if key != KEY:
        raise UnauthenticatedException()
    return Task(COMMAND, TIMEOUT).timestamp


@app.get("/puller/{timestamp}")
def task_state(timestamp: int, key: str = Depends(key_header)):
    if key != KEY:
        raise UnauthenticatedException()
    with Task.lock:
        if Task.current_task is not None:
            if Task.current_task.timestamp == timestamp:
                raise TaskRunningException()
    task = History.get(timestamp)
    if task is None:
        raise InvalidTimestampException()
    if task.success:
        return 'Success'
    raise TaskExecutionException(task.fail_reason)
