class CollectionError(RuntimeError):
    def __init__(self, message, *, retryable=False, status_code=None, retry_after=None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
