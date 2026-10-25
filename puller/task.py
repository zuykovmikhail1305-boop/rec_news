from time import time, sleep
from threading import Thread, Lock
from subprocess import run, TimeoutExpired, CalledProcessError
from history import History
from exceptions import BusyException, TimestampCollisionException

class Task:
    current_task = None
    lock = Lock()

    def __init__(self, command, timeout):
        with Task.lock:
            if Task.current_task is not None:
                raise BusyException()
            timestamp = int(time())
            if History.has(timestamp):
                raise TimestampCollisionException()
            self.timestamp = timestamp
            self.success = True
            self.fail_reason = None
            Task.current_task = self
        Thread(target=Task.task, args=(self, command, timeout)).start()

    def task(task_state, command, timeout):
        try:
            run(command,
                shell=True,
                timeout=timeout,
                check=True
            )
        except TimeoutExpired:
            task_state.success = False
            task_state.fail_reason = 'timeout'
        except CalledProcessError:
            task_state.success = False
            task_state.fail_reason = 'error'
        except Exception:
            task_state.success = False
            task_state.fail_reason = 'another exception'
        finally:
            with Task.lock:
                Task.current_task = None
                History.add(task_state)
