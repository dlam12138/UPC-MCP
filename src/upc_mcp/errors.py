class UPCError(Exception):
    """Only fixed, credential-free messages cross the CLI/MCP boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def payload(self) -> dict:
        return {"status": self.code, "source_system": "jhzf", "error": {"message": str(self)}}
