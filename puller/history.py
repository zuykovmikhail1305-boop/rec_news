from collections import deque
from threading import Lock

class History:
    tasks = deque(maxlen=3)
    lock = Lock()

    def has(timestamp):
        with History.lock:
            for task in History.tasks:
                if task.timestamp == timestamp:
                    return True
            return False

    def get(timestamp):
        with History.lock:
            for task in History.tasks:
                if task.timestamp == timestamp:
                    return task
            return None

    def add(timestamp):
        with History.lock:
            History.tasks.append(timestamp)
