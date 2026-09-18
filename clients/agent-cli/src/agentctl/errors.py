"""Static, safe diagnostics: never include server bodies or credentials."""


class CLIError(Exception):
    def __init__(self, message, *, code="configuration", exit_code=2, status=None):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.status = status

    def payload(self):
        result = {"code": self.code, "message": str(self)}
        if self.status is not None:
            result["status"] = self.status
        return {"error": result}
