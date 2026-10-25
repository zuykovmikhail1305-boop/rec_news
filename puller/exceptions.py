from fastapi import HTTPException


class TaskRunningException(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=202,
            detail='Task is running yet'
        )


class InvalidTimestampException(HTTPException):
    def __init__(self):
        super().__init__(
                status_code=400,
                detail='Invalid timestamp'
        )

class UnauthenticatedException(HTTPException):
    def __init__(self):
        super().__init__(
                status_code=401
        )

class BusyException(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=503,
            detail='Puller is busy'
        )


class TimestampCollisionException(HTTPException):
    def __init__(self):
        super().__init__(
                status_code=500,
                detail='Timestamp collision occured'
        )


class TaskExecutionException(HTTPException):
    def __init__(self, reason):
        super().__init__(
                status_code=500,
                detail=reason
        )
