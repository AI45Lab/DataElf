# Custom exceptions for DataElf


class PilotError(Exception):
    #Base exception for all pilot errors.
    pass


class DatabaseError(PilotError):
    #Database related errors (connection, query, etc.).
    pass


class DatabaseConnectionError(DatabaseError):
    #Failed to connect to database.
    pass


class DatasetNotFoundError(DatabaseError):
    #Requested dataset/table not found in database.
    pass


class LLMError(PilotError):
    #LLM API related errors.
    pass


class LLMConnectionError(LLMError):
    #Failed to connect to LLM API.
    pass


class LLMResponseError(LLMError):
    #LLM returned invalid response.
    pass


class ToolExecutionError(PilotError):
    #Tool execution failed.
    pass


class ToolNotFoundError(ToolExecutionError):
    #Requested tool not found in registry.
    pass


class ToolParameterError(ToolExecutionError):
    #Tool parameter validation failed.
    pass


class PipelineExecutionError(PilotError):
    #Pipeline DSL execution failed.
    pass


class ConfigError(PilotError):
    #Configuration related errors.
    pass
