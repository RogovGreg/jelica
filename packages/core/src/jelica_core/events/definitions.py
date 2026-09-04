from __future__ import annotations

from jelica_contracts import CodeNamespace, EventDefinition, EventType

from .catalog import EventCatalog

CORE_SYSTEM_CONFIG_INITIALIZED = EventDefinition(
    code=2100,
    name="CORE_SYSTEM_CONFIG_INITIALIZED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="System config initialized",
    message_template="System config initialized at '{config_path}'.",
    category="system_config",
)
CORE_SYSTEM_CONFIG_ALREADY_EXISTS = EventDefinition(
    code=2101,
    name="CORE_SYSTEM_CONFIG_ALREADY_EXISTS",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="System config already exists",
    message_template="System config already exists at '{config_path}'.",
    category="system_config",
)
CORE_SYSTEM_CONFIG_LOADED = EventDefinition(
    code=2102,
    name="CORE_SYSTEM_CONFIG_LOADED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="System config loaded",
    message_template="System config loaded from '{config_path}'.",
    category="system_config",
)
CORE_SYSTEM_CONFIG_VALIDATED = EventDefinition(
    code=2103,
    name="CORE_SYSTEM_CONFIG_VALIDATED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="System config validated",
    message_template="System config is valid at '{config_path}'.",
    category="system_config",
)
CORE_SYSTEM_CONFIG_VALUE_SET = EventDefinition(
    code=2104,
    name="CORE_SYSTEM_CONFIG_VALUE_SET",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="System config value changed",
    message_template="System config parameter '{parameter}' was updated.",
    category="system_config",
)
CORE_SYSTEM_CONFIG_VALUE_UNSET = EventDefinition(
    code=2105,
    name="CORE_SYSTEM_CONFIG_VALUE_UNSET",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="System config value removed",
    message_template="System config parameter '{parameter}' was removed.",
    category="system_config",
)
CORE_SYSTEM_CONFIG_INVALID = EventDefinition(
    code=2106,
    name="CORE_SYSTEM_CONFIG_INVALID",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="System config invalid",
    message_template="System config is invalid: {detail}",
    category="system_config",
)
CORE_SYSTEM_CONFIG_NOT_FOUND = EventDefinition(
    code=2107,
    name="CORE_SYSTEM_CONFIG_NOT_FOUND",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="System config not found",
    message_template=(
        "JELICA Core is not initialized. System config file is missing at '{config_path}'."
    ),
    category="system_config",
)
CORE_SYSTEM_CONFIG_READ_ERROR = EventDefinition(
    code=2108,
    name="CORE_SYSTEM_CONFIG_READ_ERROR",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="System config read error",
    message_template="Cannot read system config '{config_path}': {detail}",
    category="system_config",
)
CORE_SYSTEM_CONFIG_WRITE_ATOMIC_ERROR = EventDefinition(
    code=2109,
    name="CORE_SYSTEM_CONFIG_WRITE_ATOMIC_ERROR",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="System config write error",
    message_template="Cannot write system config '{config_path}' atomically: {detail}",
    category="system_config",
)
CORE_SYSTEM_CONFIG_PATH_RESOLVED = EventDefinition(
    code=2110,
    name="CORE_SYSTEM_CONFIG_PATH_RESOLVED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="System config path resolved",
    message_template="Resolved system config path: '{config_path}'.",
    category="system_config",
)

CORE_TASK_REGISTRY_SCHEMA_INITIALIZED = EventDefinition(
    code=2200,
    name="CORE_TASK_REGISTRY_SCHEMA_INITIALIZED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task registry schema initialized",
    message_template="Task registry schema initialized at '{database_path}'.",
    category="task_registry",
)
CORE_TASK_REGISTRY_SCHEMA_VALIDATED = EventDefinition(
    code=2201,
    name="CORE_TASK_REGISTRY_SCHEMA_VALIDATED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task registry schema validated",
    message_template="Task registry schema is valid at '{database_path}'.",
    category="task_registry",
)
CORE_TASK_REGISTRY_DATABASE_UNAVAILABLE = EventDefinition(
    code=2202,
    name="CORE_TASK_REGISTRY_DATABASE_UNAVAILABLE",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task registry database unavailable",
    message_template="Task registry database is unavailable: {detail}",
    category="task_registry",
)
CORE_TASK_REGISTRY_DATABASE_CORRUPTED = EventDefinition(
    code=2203,
    name="CORE_TASK_REGISTRY_DATABASE_CORRUPTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task registry database corrupted",
    message_template="Task registry database is corrupted: {detail}",
    category="task_registry",
)
CORE_TASK_REGISTRY_FOREIGN_DATABASE = EventDefinition(
    code=2204,
    name="CORE_TASK_REGISTRY_FOREIGN_DATABASE",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task registry database belongs to another application",
    message_template=(
        "Task registry database at '{database_path}' has unexpected application_id "
        "{application_id}."
    ),
    category="task_registry",
)
CORE_TASK_REGISTRY_SCHEMA_VERSION_UNSUPPORTED = EventDefinition(
    code=2205,
    name="CORE_TASK_REGISTRY_SCHEMA_VERSION_UNSUPPORTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task registry schema version unsupported",
    message_template=(
        "Task registry schema version {schema_version} is not supported "
        "(supported: {supported_version})."
    ),
    category="task_registry",
)
CORE_TASK_REGISTRY_SCHEMA_INCOMPATIBLE = EventDefinition(
    code=2206,
    name="CORE_TASK_REGISTRY_SCHEMA_INCOMPATIBLE",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task registry schema incompatible",
    message_template="Task registry schema is incompatible: {detail}",
    category="task_registry",
)
CORE_ANALYTICAL_TASK_REGISTERED = EventDefinition(
    code=2207,
    name="CORE_ANALYTICAL_TASK_REGISTERED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Analytical task registered",
    message_template="Analytical task '{task_id}' registered in task registry.",
    category="task_registry",
)
CORE_ANALYTICAL_TASKS_LISTED = EventDefinition(
    code=2208,
    name="CORE_ANALYTICAL_TASKS_LISTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Analytical tasks listed",
    message_template="Retrieved {count} analytical task records from task registry.",
    category="task_registry",
)
CORE_ANALYTICAL_TASK_FETCHED = EventDefinition(
    code=2209,
    name="CORE_ANALYTICAL_TASK_FETCHED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Analytical task fetched",
    message_template="Retrieved analytical task '{task_id}' from task registry.",
    category="task_registry",
)
CORE_ANALYTICAL_TASK_NOT_FOUND = EventDefinition(
    code=2210,
    name="CORE_ANALYTICAL_TASK_NOT_FOUND",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Analytical task not found",
    message_template="Analytical task '{task_id}' was not found.",
    category="task_registry",
)
CORE_ANALYTICAL_TASK_ALREADY_EXISTS = EventDefinition(
    code=2211,
    name="CORE_ANALYTICAL_TASK_ALREADY_EXISTS",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Analytical task already exists",
    message_template="Analytical task already exists: {detail}",
    category="task_registry",
)
CORE_ANALYTICAL_TASK_REQUEST_INVALID = EventDefinition(
    code=2212,
    name="CORE_ANALYTICAL_TASK_REQUEST_INVALID",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Analytical task request is invalid",
    message_template="Analytical task request is invalid: {detail}",
    category="task_registry",
)
CORE_ANALYZE_TASK_WORKSPACE_COMPENSATION_FAILED = EventDefinition(
    code=2213,
    name="CORE_ANALYZE_TASK_WORKSPACE_COMPENSATION_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task workspace cleanup failed",
    message_template=(
        "Analysis task was not registered, but temporary workspace cleanup failed at "
        "'{task_dir}': {detail}"
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_JOBS_LISTED = EventDefinition(
    code=2214,
    name="CORE_ANALYTICAL_TASK_JOBS_LISTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Analytical task jobs listed",
    message_template="Retrieved {count} job records for analytical task '{task_id}'.",
    category="task_registry",
)
CORE_ANALYTICAL_TASK_JOB_CREATED = EventDefinition(
    code=2215,
    name="CORE_ANALYTICAL_TASK_JOB_CREATED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Analytical task job created",
    message_template="Created job '{job_id}' for analytical task '{task_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_RESTARTED = EventDefinition(
    code=2216,
    name="CORE_ANALYTICAL_TASK_RESTARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Analytical task restarted",
    message_template="Analytical task '{task_id}' started with a new job '{job_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_CONFIG_REVISION_CREATED = EventDefinition(
    code=2217,
    name="CORE_ANALYTICAL_TASK_CONFIG_REVISION_CREATED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task config revision created",
    message_template=(
        "Created config revision {revision} for analytical task '{task_id}' "
        "at '{config_relative_path}'."
    ),
    category="task_config",
)
CORE_ANALYTICAL_TASK_CONFIG_UPDATED = EventDefinition(
    code=2218,
    name="CORE_ANALYTICAL_TASK_CONFIG_UPDATED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task config updated",
    message_template="Updated analytical task config for '{task_id}'.",
    category="task_config",
)
CORE_ANALYTICAL_TASK_LIFECYCLE_TRANSITION_APPLIED = EventDefinition(
    code=2219,
    name="CORE_ANALYTICAL_TASK_LIFECYCLE_TRANSITION_APPLIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task lifecycle transition applied",
    message_template="Applied lifecycle operation '{operation}' for analytical task '{task_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_LIFECYCLE_TRANSITION_INVALID = EventDefinition(
    code=2220,
    name="CORE_ANALYTICAL_TASK_LIFECYCLE_TRANSITION_INVALID",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Task lifecycle transition invalid",
    message_template=(
        "Lifecycle operation '{operation}' is invalid for analytical task '{task_id}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_LIFECYCLE_TRANSITION_CONFLICT = EventDefinition(
    code=2221,
    name="CORE_ANALYTICAL_TASK_LIFECYCLE_TRANSITION_CONFLICT",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Task lifecycle transition conflict",
    message_template=(
        "Lifecycle operation '{operation}' conflicts with current state for analytical task "
        "'{task_id}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_LIFECYCLE_CONCURRENT_UPDATE = EventDefinition(
    code=2222,
    name="CORE_ANALYTICAL_TASK_LIFECYCLE_CONCURRENT_UPDATE",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Task lifecycle concurrent update",
    message_template=(
        "Lifecycle operation '{operation}' encountered concurrent update for analytical task "
        "'{task_id}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_SECOND_ACTIVE_JOB_BLOCKED = EventDefinition(
    code=2223,
    name="CORE_ANALYTICAL_TASK_SECOND_ACTIVE_JOB_BLOCKED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Second active job blocked",
    message_template=(
        "Blocked creation of a second active job for analytical task '{task_id}'. "
        "Existing job: '{job_id}'."
    ),
    category="task_lifecycle",
)
CORE_TASK_REGISTRY_MIGRATION_FAILED = EventDefinition(
    code=2224,
    name="CORE_TASK_REGISTRY_MIGRATION_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task registry migration failed",
    message_template="Task registry migration failed: {detail}",
    category="task_registry",
)
CORE_TASK_CONFIG_COMPENSATION_FAILED = EventDefinition(
    code=2225,
    name="CORE_TASK_CONFIG_COMPENSATION_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task config compensation failed",
    message_template="Task config compensation failed for '{task_id}': {detail}",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_START_REQUESTED = EventDefinition(
    code=2226,
    name="CORE_ANALYTICAL_TASK_START_REQUESTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task start requested",
    message_template="Requested start for analytical task '{task_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_START_APPLIED = EventDefinition(
    code=2227,
    name="CORE_ANALYTICAL_TASK_START_APPLIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task start applied",
    message_template=(
        "Start applied for analytical task '{task_id}', active job is '{job_id}' "
        "in state '{state}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_START_ALREADY_SATISFIED = EventDefinition(
    code=2228,
    name="CORE_ANALYTICAL_TASK_START_ALREADY_SATISFIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task start already satisfied",
    message_template=(
        "Task start is already satisfied for analytical task '{task_id}', active job is '{job_id}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_START_REJECTED = EventDefinition(
    code=2229,
    name="CORE_ANALYTICAL_TASK_START_REJECTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task start rejected",
    message_template="Task start was rejected for analytical task '{task_id}': {detail}",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_PAUSE_REQUESTED = EventDefinition(
    code=2250,
    name="CORE_ANALYTICAL_TASK_PAUSE_REQUESTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task pause requested",
    message_template="Requested pause for analytical task '{task_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_PAUSE_APPLIED = EventDefinition(
    code=2251,
    name="CORE_ANALYTICAL_TASK_PAUSE_APPLIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task pause applied",
    message_template=(
        "Pause applied for analytical task '{task_id}', active job is '{job_id}' "
        "in state '{state}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_PAUSE_ALREADY_SATISFIED = EventDefinition(
    code=2252,
    name="CORE_ANALYTICAL_TASK_PAUSE_ALREADY_SATISFIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task pause already satisfied",
    message_template=(
        "Pause is already satisfied for analytical task '{task_id}', active job is '{job_id}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_PAUSE_REJECTED = EventDefinition(
    code=2253,
    name="CORE_ANALYTICAL_TASK_PAUSE_REJECTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task pause rejected",
    message_template="Task pause was rejected for analytical task '{task_id}': {detail}",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_RESUME_REQUESTED = EventDefinition(
    code=2254,
    name="CORE_ANALYTICAL_TASK_RESUME_REQUESTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task resume requested",
    message_template="Requested resume for analytical task '{task_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_RESUME_APPLIED = EventDefinition(
    code=2255,
    name="CORE_ANALYTICAL_TASK_RESUME_APPLIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task resume applied",
    message_template=(
        "Resume applied for analytical task '{task_id}', active job is '{job_id}' "
        "in state '{state}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_RESUME_ALREADY_SATISFIED = EventDefinition(
    code=2256,
    name="CORE_ANALYTICAL_TASK_RESUME_ALREADY_SATISFIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task resume already satisfied",
    message_template=(
        "Resume is already satisfied for analytical task '{task_id}', active job is '{job_id}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_RESUME_REJECTED = EventDefinition(
    code=2257,
    name="CORE_ANALYTICAL_TASK_RESUME_REJECTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task resume rejected",
    message_template="Task resume was rejected for analytical task '{task_id}': {detail}",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_CANCEL_REQUESTED = EventDefinition(
    code=2258,
    name="CORE_ANALYTICAL_TASK_CANCEL_REQUESTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task cancel requested",
    message_template="Requested cancel for analytical task '{task_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_CANCEL_APPLIED = EventDefinition(
    code=2259,
    name="CORE_ANALYTICAL_TASK_CANCEL_APPLIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task cancel applied",
    message_template=(
        "Cancel applied for analytical task '{task_id}', active job is '{job_id}' "
        "in state '{state}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_CANCEL_ALREADY_SATISFIED = EventDefinition(
    code=2260,
    name="CORE_ANALYTICAL_TASK_CANCEL_ALREADY_SATISFIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task cancel already satisfied",
    message_template=(
        "Cancel is already satisfied for analytical task '{task_id}', active job is '{job_id}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_CANCEL_REJECTED = EventDefinition(
    code=2261,
    name="CORE_ANALYTICAL_TASK_CANCEL_REJECTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task cancel rejected",
    message_template="Task cancel was rejected for analytical task '{task_id}': {detail}",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_UPDATE_REQUESTED = EventDefinition(
    code=2264,
    name="CORE_ANALYTICAL_TASK_UPDATE_REQUESTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task update requested",
    message_template="Requested config update for analytical task '{task_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_UPDATE_APPLIED = EventDefinition(
    code=2265,
    name="CORE_ANALYTICAL_TASK_UPDATE_APPLIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task update applied",
    message_template=(
        "Config update applied for analytical task '{task_id}' at revision "
        "'{current_config_revision}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_UPDATE_ALREADY_SATISFIED = EventDefinition(
    code=2266,
    name="CORE_ANALYTICAL_TASK_UPDATE_ALREADY_SATISFIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task update already satisfied",
    message_template="Config update is already satisfied for analytical task '{task_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_UPDATE_REJECTED = EventDefinition(
    code=2267,
    name="CORE_ANALYTICAL_TASK_UPDATE_REJECTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task update rejected",
    message_template="Task update was rejected for analytical task '{task_id}': {detail}",
    category="task_lifecycle",
)
CORE_ANALYTICAL_JOB_REPRIORITIZE_REQUESTED = EventDefinition(
    code=2268,
    name="CORE_ANALYTICAL_JOB_REPRIORITIZE_REQUESTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Job reprioritize requested",
    message_template=(
        "Requested reprioritization for analytical task '{task_id}' to priority '{priority}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_JOB_REPRIORITIZE_APPLIED = EventDefinition(
    code=2269,
    name="CORE_ANALYTICAL_JOB_REPRIORITIZE_APPLIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Job reprioritize applied",
    message_template=(
        "Job '{job_id}' for analytical task '{task_id}' reprioritized from "
        "'{old_priority}' to '{new_priority}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_JOB_REPRIORITIZE_ALREADY_SATISFIED = EventDefinition(
    code=2270,
    name="CORE_ANALYTICAL_JOB_REPRIORITIZE_ALREADY_SATISFIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Job reprioritize already satisfied",
    message_template=(
        "Reprioritization is already satisfied for job '{job_id}' (task '{task_id}')."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_JOB_REPRIORITIZE_REJECTED = EventDefinition(
    code=2271,
    name="CORE_ANALYTICAL_JOB_REPRIORITIZE_REJECTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Job reprioritize rejected",
    message_template="Job reprioritization was rejected for analytical task '{task_id}': {detail}",
    category="task_lifecycle",
)
CORE_RUNTIME_LEASE_ACQUIRED = EventDefinition(
    code=2230,
    name="CORE_RUNTIME_LEASE_ACQUIRED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Runtime lease acquired",
    message_template="Execution runtime lease acquired by instance '{runtime_instance_id}'.",
    category="runtime",
)
CORE_RUNTIME_LEASE_RELEASED = EventDefinition(
    code=2231,
    name="CORE_RUNTIME_LEASE_RELEASED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Runtime lease released",
    message_template="Execution runtime lease released by instance '{runtime_instance_id}'.",
    category="runtime",
)
CORE_RUNTIME_LEASE_CONFLICT = EventDefinition(
    code=2232,
    name="CORE_RUNTIME_LEASE_CONFLICT",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Runtime lease conflict",
    message_template=(
        "Cannot acquire execution runtime lease; current owner is runtime instance "
        "'{runtime_instance_id}'."
    ),
    category="runtime",
)
CORE_RUNTIME_LEASE_EXPIRED = EventDefinition(
    code=2233,
    name="CORE_RUNTIME_LEASE_EXPIRED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Runtime lease expired",
    message_template=(
        "Execution runtime lease expired for runtime instance '{runtime_instance_id}'."
    ),
    category="runtime",
)
CORE_RUNTIME_SCHEDULER_STARTED = EventDefinition(
    code=2234,
    name="CORE_RUNTIME_SCHEDULER_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Runtime scheduler started",
    message_template="Foreground scheduler started in runtime instance '{runtime_instance_id}'.",
    category="runtime",
)
CORE_RUNTIME_SCHEDULER_STOPPED = EventDefinition(
    code=2235,
    name="CORE_RUNTIME_SCHEDULER_STOPPED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Runtime scheduler stopped",
    message_template="Foreground scheduler stopped in runtime instance '{runtime_instance_id}'.",
    category="runtime",
)
CORE_RUNTIME_JOB_CLAIMED = EventDefinition(
    code=2236,
    name="CORE_RUNTIME_JOB_CLAIMED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Runtime job claimed",
    message_template="Scheduler claimed job '{job_id}' for analytical task '{task_id}'.",
    category="runtime",
)
CORE_RUNTIME_WORKER_STARTED = EventDefinition(
    code=2237,
    name="CORE_RUNTIME_WORKER_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Worker started",
    message_template="Worker process started for job '{job_id}' (task '{task_id}').",
    category="runtime",
)
CORE_RUNTIME_WORKER_HEARTBEAT_LOST = EventDefinition(
    code=2238,
    name="CORE_RUNTIME_WORKER_HEARTBEAT_LOST",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Worker heartbeat lost",
    message_template="Worker heartbeat was lost for job '{job_id}' (task '{task_id}').",
    category="runtime",
)
CORE_RUNTIME_WORKER_EXITED = EventDefinition(
    code=2239,
    name="CORE_RUNTIME_WORKER_EXITED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Worker exited",
    message_template="Worker process exited for job '{job_id}' (task '{task_id}').",
    category="runtime",
)
CORE_RUNTIME_STAGE_STARTED = EventDefinition(
    code=2240,
    name="CORE_RUNTIME_STAGE_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Stage started",
    message_template=("Stage '{stage_id}' started for job '{job_id}' (task '{task_id}')."),
    category="runtime",
)
CORE_RUNTIME_STAGE_COMMITTED = EventDefinition(
    code=2241,
    name="CORE_RUNTIME_STAGE_COMMITTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Stage committed",
    message_template=("Stage '{stage_id}' committed for job '{job_id}' (task '{task_id}')."),
    category="runtime",
)
CORE_RUNTIME_JOB_COMPLETED = EventDefinition(
    code=2242,
    name="CORE_RUNTIME_JOB_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Job completed",
    message_template="Job '{job_id}' for analytical task '{task_id}' completed successfully.",
    category="runtime",
)
CORE_RUNTIME_JOB_FAILED = EventDefinition(
    code=2243,
    name="CORE_RUNTIME_JOB_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Job failed",
    message_template="Job '{job_id}' for analytical task '{task_id}' failed: {detail}",
    category="runtime",
)
CORE_LOCAL_NOTIFICATION_DIAGNOSTIC = EventDefinition(
    code=2390,
    name="CORE_LOCAL_NOTIFICATION_DIAGNOSTIC",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Local notification diagnostic",
    message_template="local_notification {phase}: {reason}.",
    category="notifications",
)
CORE_RUNTIME_RECOVERY_STARTED = EventDefinition(
    code=2244,
    name="CORE_RUNTIME_RECOVERY_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Recovery started",
    message_template="Recovery started for runtime instance '{runtime_instance_id}'.",
    category="runtime",
)
CORE_RUNTIME_RECOVERY_COMPLETED = EventDefinition(
    code=2245,
    name="CORE_RUNTIME_RECOVERY_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Recovery completed",
    message_template="Recovery completed for runtime instance '{runtime_instance_id}'.",
    category="runtime",
)
CORE_RUNTIME_RECOVERY_FAILED = EventDefinition(
    code=2246,
    name="CORE_RUNTIME_RECOVERY_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Recovery failed",
    message_template=("Recovery failed for job '{job_id}' (task '{task_id}'): {reason}"),
    category="runtime",
)
CORE_RUNTIME_STALE_WORKER_MESSAGE_REJECTED = EventDefinition(
    code=2247,
    name="CORE_RUNTIME_STALE_WORKER_MESSAGE_REJECTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Stale worker message rejected",
    message_template="Rejected stale worker message for job '{job_id}'.",
    category="runtime",
)
CORE_RUNTIME_PROCESS_SPAWN_FAILED = EventDefinition(
    code=2248,
    name="CORE_RUNTIME_PROCESS_SPAWN_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Worker process spawn failed",
    message_template=(
        "Cannot start worker process for job '{job_id}' (task '{task_id}'): {detail}"
    ),
    category="runtime",
)
CORE_RUNTIME_INTERRUPTED = EventDefinition(
    code=2249,
    name="CORE_RUNTIME_INTERRUPTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Runtime interrupted",
    message_template="Execution runtime interrupted for instance '{runtime_instance_id}'.",
    category="runtime",
)
CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PAUSE = EventDefinition(
    code=2262,
    name="CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PAUSE",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Worker safely stopped for pause",
    message_template="Worker safely stopped for pause for job '{job_id}' (task '{task_id}').",
    category="runtime",
)
CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_CANCEL = EventDefinition(
    code=2263,
    name="CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_CANCEL",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Worker safely stopped for cancel",
    message_template="Worker safely stopped for cancel for job '{job_id}' (task '{task_id}').",
    category="runtime",
)
CORE_RUNTIME_PREEMPTION_SELECTED = EventDefinition(
    code=2272,
    name="CORE_RUNTIME_PREEMPTION_SELECTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Preemption candidate selected",
    message_template=(
        "Selected preemption victim job '{victim_job_id}' (task '{victim_task_id}') for "
        "queued job '{candidate_job_id}' (task '{candidate_task_id}')."
    ),
    category="runtime",
)
CORE_RUNTIME_PREEMPTION_REQUESTED = EventDefinition(
    code=2273,
    name="CORE_RUNTIME_PREEMPTION_REQUESTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Preemption requested",
    message_template=(
        "Requested safe preemption of job '{victim_job_id}' (task '{victim_task_id}') for "
        "queued job '{candidate_job_id}' (task '{candidate_task_id}')."
    ),
    category="runtime",
)
CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PREEMPTION = EventDefinition(
    code=2274,
    name="CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PREEMPTION",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Worker safely stopped for preemption",
    message_template=(
        "Worker safely stopped for preemption for job '{job_id}' (task '{task_id}')."
    ),
    category="runtime",
)
CORE_RUNTIME_PREEMPTED_JOB_RETURNED_TO_WAITING = EventDefinition(
    code=2275,
    name="CORE_RUNTIME_PREEMPTED_JOB_RETURNED_TO_WAITING",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Preempted job returned to waiting",
    message_template="Preempted job '{job_id}' for task '{task_id}' returned to waiting.",
    category="runtime",
)
CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_DELETION = EventDefinition(
    code=2287,
    name="CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_DELETION",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Worker safely stopped for deletion",
    message_template="Worker safely stopped for deletion for job '{job_id}' (task '{task_id}').",
    category="runtime",
)
CORE_INPUT_SOURCE_UNSUPPORTED = EventDefinition(
    code=2288,
    name="CORE_INPUT_SOURCE_UNSUPPORTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Input source unsupported",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_PATH_NOT_FOUND = EventDefinition(
    code=2289,
    name="CORE_INPUT_PATH_NOT_FOUND",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Input path not found",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_FILE_TYPE_UNSUPPORTED = EventDefinition(
    code=2290,
    name="CORE_INPUT_FILE_TYPE_UNSUPPORTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Input file type unsupported",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_FILE_UNREADABLE = EventDefinition(
    code=2291,
    name="CORE_INPUT_FILE_UNREADABLE",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Input file unreadable",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_FILE_EMPTY = EventDefinition(
    code=2292,
    name="CORE_INPUT_FILE_EMPTY",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Input file empty",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_DIRECTORY_EMPTY = EventDefinition(
    code=2293,
    name="CORE_INPUT_DIRECTORY_EMPTY",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Input directory empty",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_DIRECTORY_NO_SUPPORTED_FILES = EventDefinition(
    code=2294,
    name="CORE_INPUT_DIRECTORY_NO_SUPPORTED_FILES",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Input directory has no supported files",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_NO_DATA_ACQUIRED = EventDefinition(
    code=2295,
    name="CORE_INPUT_NO_DATA_ACQUIRED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="No input data acquired",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_UNSUPPORTED_FILES_SKIPPED = EventDefinition(
    code=2296,
    name="CORE_INPUT_UNSUPPORTED_FILES_SKIPPED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Unsupported input files skipped",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_SYMLINK_UNSUPPORTED = EventDefinition(
    code=2297,
    name="CORE_INPUT_SYMLINK_UNSUPPORTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Input symlink unsupported",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_SYMLINKS_SKIPPED = EventDefinition(
    code=2298,
    name="CORE_INPUT_SYMLINKS_SKIPPED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Input symlinks skipped",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_DIRECTORY_DEPTH_LIMIT_REACHED = EventDefinition(
    code=2299,
    name="CORE_INPUT_DIRECTORY_DEPTH_LIMIT_REACHED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Input directory depth limit reached",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_DUPLICATES_SKIPPED = EventDefinition(
    code=2300,
    name="CORE_INPUT_DUPLICATES_SKIPPED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Input duplicates skipped",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_COPY_FAILED = EventDefinition(
    code=2301,
    name="CORE_INPUT_COPY_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Input copy failed",
    message_template="{detail}",
    category="input_data",
)
CORE_INLINE_SEQUENCE_INVALID = EventDefinition(
    code=2302,
    name="CORE_INLINE_SEQUENCE_INVALID",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Inline sequence invalid",
    message_template="{detail}",
    category="input_data",
)
CORE_NCBI_URL_UNSUPPORTED = EventDefinition(
    code=2303,
    name="CORE_NCBI_URL_UNSUPPORTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="NCBI URL unsupported",
    message_template="{detail}",
    category="input_data",
)
CORE_NCBI_ACCESSION_INVALID = EventDefinition(
    code=2304,
    name="CORE_NCBI_ACCESSION_INVALID",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="NCBI accession invalid",
    message_template="{detail}",
    category="input_data",
)
CORE_NCBI_RECORD_NOT_FOUND = EventDefinition(
    code=2305,
    name="CORE_NCBI_RECORD_NOT_FOUND",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="NCBI record not found",
    message_template="{detail}",
    category="input_data",
)
CORE_NCBI_REQUEST_FAILED = EventDefinition(
    code=2306,
    name="CORE_NCBI_REQUEST_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="NCBI request failed",
    message_template="{detail}",
    category="input_data",
)
CORE_NCBI_REQUEST_TIMEOUT = EventDefinition(
    code=2307,
    name="CORE_NCBI_REQUEST_TIMEOUT",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="NCBI request timeout",
    message_template="{detail}",
    category="input_data",
)
CORE_NCBI_RESPONSE_EMPTY = EventDefinition(
    code=2308,
    name="CORE_NCBI_RESPONSE_EMPTY",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="NCBI response empty",
    message_template="{detail}",
    category="input_data",
)
CORE_NCBI_RESPONSE_INVALID = EventDefinition(
    code=2309,
    name="CORE_NCBI_RESPONSE_INVALID",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="NCBI response invalid",
    message_template="{detail}",
    category="input_data",
)
CORE_NCBI_PARTIAL_RESPONSE = EventDefinition(
    code=2310,
    name="CORE_NCBI_PARTIAL_RESPONSE",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="NCBI partial response",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_ACQUISITION_COMPLETED = EventDefinition(
    code=2311,
    name="CORE_INPUT_ACQUISITION_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Input acquisition completed",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_PROCESSING_STARTED = EventDefinition(
    code=2312,
    name="CORE_INPUT_PROCESSING_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Input processing started",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_PROCESSING_FILE_PROCESSED = EventDefinition(
    code=2313,
    name="CORE_INPUT_PROCESSING_FILE_PROCESSED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Input processing file processed",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_PROCESSING_COMPLETED = EventDefinition(
    code=2314,
    name="CORE_INPUT_PROCESSING_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Input processing completed",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_PROCESSING_VALIDATION_FAILED = EventDefinition(
    code=2315,
    name="CORE_INPUT_PROCESSING_VALIDATION_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Input processing validation failed",
    message_template="{detail}",
    category="input_data",
)
CORE_INPUT_PROCESSING_FAILED = EventDefinition(
    code=2316,
    name="CORE_INPUT_PROCESSING_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Input processing failed",
    message_template="{detail}",
    category="input_data",
)
CORE_ALIGNMENT_STARTED = EventDefinition(
    code=2317,
    name="CORE_ALIGNMENT_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Alignment stage started",
    message_template="{detail}",
    category="alignment",
)
CORE_ALIGNMENT_SKIPPED = EventDefinition(
    code=2318,
    name="CORE_ALIGNMENT_SKIPPED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Alignment skipped",
    message_template="{detail}",
    category="alignment",
)
CORE_ALIGNMENT_PREALIGNED_VALIDATION_STARTED = EventDefinition(
    code=2319,
    name="CORE_ALIGNMENT_PREALIGNED_VALIDATION_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Prealigned validation started",
    message_template="{detail}",
    category="alignment",
)
CORE_ALIGNMENT_MAFFT_AVAILABILITY_CONFIRMED = EventDefinition(
    code=2320,
    name="CORE_ALIGNMENT_MAFFT_AVAILABILITY_CONFIRMED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="MAFFT availability confirmed",
    message_template="{detail}",
    category="alignment",
)
CORE_ALIGNMENT_MAFFT_PROCESS_STARTED = EventDefinition(
    code=2321,
    name="CORE_ALIGNMENT_MAFFT_PROCESS_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="MAFFT process started",
    message_template="{detail}",
    category="alignment",
)
CORE_ALIGNMENT_MAFFT_PROCESS_COMPLETED = EventDefinition(
    code=2322,
    name="CORE_ALIGNMENT_MAFFT_PROCESS_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="MAFFT process completed",
    message_template="{detail}",
    category="alignment",
)
CORE_ALIGNMENT_MAFFT_PROCESS_FAILED = EventDefinition(
    code=2323,
    name="CORE_ALIGNMENT_MAFFT_PROCESS_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="MAFFT process failed",
    message_template="{detail}",
    category="alignment",
)
CORE_ALIGNMENT_MAFFT_STOPPED_FOR_PAUSE = EventDefinition(
    code=2324,
    name="CORE_ALIGNMENT_MAFFT_STOPPED_FOR_PAUSE",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="MAFFT stopped for pause",
    message_template="{detail}",
    category="alignment",
)
CORE_ALIGNMENT_MAFFT_STOPPED_FOR_CANCEL = EventDefinition(
    code=2325,
    name="CORE_ALIGNMENT_MAFFT_STOPPED_FOR_CANCEL",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="MAFFT stopped for cancel",
    message_template="{detail}",
    category="alignment",
)
CORE_ALIGNMENT_MAFFT_STOPPED_FOR_SHUTDOWN = EventDefinition(
    code=2326,
    name="CORE_ALIGNMENT_MAFFT_STOPPED_FOR_SHUTDOWN",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="MAFFT stopped for runtime shutdown",
    message_template="{detail}",
    category="alignment",
)
CORE_ALIGNMENT_RESULT_VALIDATION_FAILED = EventDefinition(
    code=2327,
    name="CORE_ALIGNMENT_RESULT_VALIDATION_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Alignment result validation failed",
    message_template="{detail}",
    category="alignment",
)
CORE_ALIGNMENT_RESULT_PUBLISHED = EventDefinition(
    code=2328,
    name="CORE_ALIGNMENT_RESULT_PUBLISHED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Alignment result published",
    message_template="{detail}",
    category="alignment",
)
CORE_ALIGNMENT_COMPLETED = EventDefinition(
    code=2329,
    name="CORE_ALIGNMENT_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Alignment stage completed",
    message_template="{detail}",
    category="alignment",
)
CORE_COMPARATIVE_ANALYSIS_STARTED = EventDefinition(
    code=2330,
    name="CORE_COMPARATIVE_ANALYSIS_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Comparative analysis started",
    message_template="{detail}",
    category="comparative_analysis",
)
CORE_COMPARATIVE_ANALYSIS_SKIPPED = EventDefinition(
    code=2331,
    name="CORE_COMPARATIVE_ANALYSIS_SKIPPED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Comparative analysis skipped",
    message_template="{detail}",
    category="comparative_analysis",
)
CORE_COMPARATIVE_ANALYSIS_PHASE_STARTED = EventDefinition(
    code=2332,
    name="CORE_COMPARATIVE_ANALYSIS_PHASE_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Comparative-analysis phase started",
    message_template="{detail}",
    category="comparative_analysis",
)
CORE_COMPARATIVE_ANALYSIS_PROGRESS = EventDefinition(
    code=2333,
    name="CORE_COMPARATIVE_ANALYSIS_PROGRESS",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Comparative-analysis progress",
    message_template="{detail}",
    category="comparative_analysis",
)
CORE_COMPARATIVE_ANALYSIS_OPERATION_FAILED = EventDefinition(
    code=2334,
    name="CORE_COMPARATIVE_ANALYSIS_OPERATION_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Comparative-analysis operation failed",
    message_template="{detail}",
    category="comparative_analysis",
)
CORE_COMPARATIVE_ANALYSIS_RESULT_PUBLISHED = EventDefinition(
    code=2335,
    name="CORE_COMPARATIVE_ANALYSIS_RESULT_PUBLISHED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Comparative-analysis result published",
    message_template="{detail}",
    category="comparative_analysis",
)
CORE_COMPARATIVE_ANALYSIS_COMPLETED = EventDefinition(
    code=2336,
    name="CORE_COMPARATIVE_ANALYSIS_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Comparative analysis completed",
    message_template="{detail}",
    category="comparative_analysis",
)
CORE_COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS = EventDefinition(
    code=2337,
    name="CORE_COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Comparative analysis partially completed",
    message_template="{detail}",
    category="comparative_analysis",
)
CORE_COMPARATIVE_ANALYSIS_FAILED = EventDefinition(
    code=2338,
    name="CORE_COMPARATIVE_ANALYSIS_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Comparative analysis failed",
    message_template="{detail}",
    category="comparative_analysis",
)
CORE_DISTANCE_MATRIX_STARTED = EventDefinition(
    code=2339,
    name="CORE_DISTANCE_MATRIX_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Distance matrix started",
    message_template="{detail}",
    category="distance_matrix",
)
CORE_DISTANCE_MATRIX_SKIPPED = EventDefinition(
    code=2340,
    name="CORE_DISTANCE_MATRIX_SKIPPED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Distance matrix skipped",
    message_template="{detail}",
    category="distance_matrix",
)
CORE_DISTANCE_MATRIX_PROGRESS = EventDefinition(
    code=2341,
    name="CORE_DISTANCE_MATRIX_PROGRESS",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Distance-matrix progress",
    message_template="{detail}",
    category="distance_matrix",
)
CORE_DISTANCE_MATRIX_RESULT_PUBLISHED = EventDefinition(
    code=2342,
    name="CORE_DISTANCE_MATRIX_RESULT_PUBLISHED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Distance-matrix result published",
    message_template="{detail}",
    category="distance_matrix",
)
CORE_DISTANCE_MATRIX_COMPLETED = EventDefinition(
    code=2343,
    name="CORE_DISTANCE_MATRIX_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Distance matrix completed",
    message_template="{detail}",
    category="distance_matrix",
)
CORE_DISTANCE_MATRIX_PARTIAL_SUCCESS = EventDefinition(
    code=2344,
    name="CORE_DISTANCE_MATRIX_PARTIAL_SUCCESS",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Distance matrix partially completed",
    message_template="{detail}",
    category="distance_matrix",
)
CORE_DISTANCE_MATRIX_FAILED = EventDefinition(
    code=2345,
    name="CORE_DISTANCE_MATRIX_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Distance matrix failed",
    message_template="{detail}",
    category="distance_matrix",
)
CORE_PHYLOGENETIC_TREE_STARTED = EventDefinition(
    code=2346,
    name="CORE_PHYLOGENETIC_TREE_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Phylogenetic tree started",
    message_template="{detail}",
    category="phylogenetic_tree",
)
CORE_PHYLOGENETIC_TREE_SKIPPED = EventDefinition(
    code=2347,
    name="CORE_PHYLOGENETIC_TREE_SKIPPED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Phylogenetic tree skipped",
    message_template="{detail}",
    category="phylogenetic_tree",
)
CORE_PHYLOGENETIC_TREE_PROGRESS = EventDefinition(
    code=2348,
    name="CORE_PHYLOGENETIC_TREE_PROGRESS",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Phylogenetic-tree progress",
    message_template="{detail}",
    category="phylogenetic_tree",
)
CORE_PHYLOGENETIC_TREE_RESULT_PUBLISHED = EventDefinition(
    code=2349,
    name="CORE_PHYLOGENETIC_TREE_RESULT_PUBLISHED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Phylogenetic-tree result published",
    message_template="{detail}",
    category="phylogenetic_tree",
)
CORE_PHYLOGENETIC_TREE_COMPLETED = EventDefinition(
    code=2350,
    name="CORE_PHYLOGENETIC_TREE_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Phylogenetic tree completed",
    message_template="{detail}",
    category="phylogenetic_tree",
)
CORE_PHYLOGENETIC_TREE_FAILED = EventDefinition(
    code=2351,
    name="CORE_PHYLOGENETIC_TREE_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Phylogenetic tree failed",
    message_template="{detail}",
    category="phylogenetic_tree",
)
CORE_CLADE_DETECTION_STARTED = EventDefinition(
    code=2352,
    name="CORE_CLADE_DETECTION_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Clade detection started",
    message_template="{detail}",
    category="clade_detection",
)
CORE_CLADE_DETECTION_SKIPPED = EventDefinition(
    code=2353,
    name="CORE_CLADE_DETECTION_SKIPPED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Clade detection skipped",
    message_template="{detail}",
    category="clade_detection",
)
CORE_CLADE_DETECTION_PROGRESS = EventDefinition(
    code=2354,
    name="CORE_CLADE_DETECTION_PROGRESS",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Clade-detection progress",
    message_template="{detail}",
    category="clade_detection",
)
CORE_CLADE_DETECTION_RESULT_PUBLISHED = EventDefinition(
    code=2355,
    name="CORE_CLADE_DETECTION_RESULT_PUBLISHED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Clade-detection result published",
    message_template="{detail}",
    category="clade_detection",
)
CORE_CLADE_DETECTION_COMPLETED = EventDefinition(
    code=2356,
    name="CORE_CLADE_DETECTION_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Clade detection completed",
    message_template="{detail}",
    category="clade_detection",
)
CORE_CLADE_DETECTION_FAILED = EventDefinition(
    code=2357,
    name="CORE_CLADE_DETECTION_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Clade detection failed",
    message_template="{detail}",
    category="clade_detection",
)
CORE_ANALYTICAL_TASKS_DELETE_REQUESTED = EventDefinition(
    code=2276,
    name="CORE_ANALYTICAL_TASKS_DELETE_REQUESTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Batch task deletion requested",
    message_template="Requested deletion of {count} analytical tasks.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASKS_DELETE_COMPLETED = EventDefinition(
    code=2277,
    name="CORE_ANALYTICAL_TASKS_DELETE_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Batch task deletion completed",
    message_template="Deleted or queued deletion for {applied} analytical tasks.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASKS_DELETE_PARTIALLY_COMPLETED = EventDefinition(
    code=2278,
    name="CORE_ANALYTICAL_TASKS_DELETE_PARTIALLY_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Batch task deletion partially completed",
    message_template=(
        "Batch deletion partially completed: {applied} tasks applied, {rejected} tasks rejected."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_DELETE_REQUESTED = EventDefinition(
    code=2279,
    name="CORE_ANALYTICAL_TASK_DELETE_REQUESTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task deletion requested",
    message_template="Requested safe deletion for analytical task '{task_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_DELETE_APPLIED = EventDefinition(
    code=2280,
    name="CORE_ANALYTICAL_TASK_DELETE_APPLIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task deleted",
    message_template="Deleted analytical task '{task_id}' and associated files.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_DELETE_ALREADY_SATISFIED = EventDefinition(
    code=2281,
    name="CORE_ANALYTICAL_TASK_DELETE_ALREADY_SATISFIED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task deletion already satisfied",
    message_template="Task deletion already requested for analytical task '{task_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_DELETE_REJECTED = EventDefinition(
    code=2282,
    name="CORE_ANALYTICAL_TASK_DELETE_REJECTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Task deletion rejected",
    message_template="Task deletion was rejected for analytical task '{task_id}': {detail}",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_WATCH_STARTED = EventDefinition(
    code=2283,
    name="CORE_ANALYTICAL_TASK_WATCH_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task watch started",
    message_template="Started watching analytical task '{task_id}' and job '{job_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_WATCH_COMPLETED = EventDefinition(
    code=2284,
    name="CORE_ANALYTICAL_TASK_WATCH_COMPLETED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task watch completed",
    message_template=(
        "Task watch completed for analytical task '{task_id}' and job '{job_id}' with result "
        "'{result}'."
    ),
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_WATCH_INTERRUPTED = EventDefinition(
    code=2285,
    name="CORE_ANALYTICAL_TASK_WATCH_INTERRUPTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Task watch interrupted",
    message_template="Task watch interrupted for analytical task '{task_id}'.",
    category="task_lifecycle",
)
CORE_ANALYTICAL_TASK_WATCH_REJECTED = EventDefinition(
    code=2286,
    name="CORE_ANALYTICAL_TASK_WATCH_REJECTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task watch rejected",
    message_template="Task watch was rejected for analytical task '{task_id}': {detail}",
    category="task_lifecycle",
)

CORE_ANALYZE_REQUEST_STARTED = EventDefinition(
    code=2000,
    name="CORE_ANALYZE_REQUEST_STARTED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.INFO,
    title="Task initialization started",
    message_template="Started analysis task initialization request.",
    category="task_lifecycle",
)
CORE_ANALYZE_CONFIG_PARSED = EventDefinition(
    code=2001,
    name="CORE_ANALYZE_CONFIG_PARSED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task config parsed",
    message_template="Analysis task configuration parsed successfully.",
    category="task_config",
)
CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED = EventDefinition(
    code=2002,
    name="CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.WARNING,
    title="Unknown task config parameter ignored",
    message_template="Unknown analysis config parameter was ignored: '{parameter}'.",
    category="task_config",
)
CORE_ANALYZE_TASK_DIRECTORY_CREATED = EventDefinition(
    code=2003,
    name="CORE_ANALYZE_TASK_DIRECTORY_CREATED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task directory created",
    message_template="Task directory was created: '{task_dir}'.",
    category="task_lifecycle",
)
CORE_ANALYZE_CONFIG_SAVED = EventDefinition(
    code=2004,
    name="CORE_ANALYZE_CONFIG_SAVED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task config saved",
    message_template="Normalized task config saved to '{config_path}'.",
    category="task_lifecycle",
)
CORE_ANALYZE_TASK_INITIALIZED = EventDefinition(
    code=2005,
    name="CORE_ANALYZE_TASK_INITIALIZED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.SUCCESS,
    title="Task initialized",
    message_template="Analysis task '{task_id}' initialized successfully.",
    category="task_lifecycle",
)
CORE_ANALYZE_SOURCE_NOT_FOUND = EventDefinition(
    code=2006,
    name="CORE_ANALYZE_SOURCE_NOT_FOUND",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Input source not found",
    message_template="Input source was not found: '{source}'.",
    category="input_data",
)
CORE_ANALYZE_SOURCE_UNAVAILABLE = EventDefinition(
    code=2007,
    name="CORE_ANALYZE_SOURCE_UNAVAILABLE",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Input source unavailable",
    message_template="Input source is unavailable: '{source}'.",
    category="input_data",
)
CORE_ANALYZE_TASK_DIRECTORY_CREATE_FAILED = EventDefinition(
    code=2008,
    name="CORE_ANALYZE_TASK_DIRECTORY_CREATE_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task directory creation failed",
    message_template="Failed to create analysis task directory: {detail}",
    category="task_lifecycle",
)
CORE_ANALYZE_TASK_CONFIG_SAVE_FAILED = EventDefinition(
    code=2009,
    name="CORE_ANALYZE_TASK_CONFIG_SAVE_FAILED",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task config save failed",
    message_template="Failed to save normalized task config: {detail}",
    category="task_lifecycle",
)
CORE_ANALYZE_TASK_CONFIG_INVALID = EventDefinition(
    code=2010,
    name="CORE_ANALYZE_TASK_CONFIG_INVALID",
    namespace=CodeNamespace.CORE,
    default_type=EventType.ERROR,
    title="Task config invalid",
    message_template="Analysis task configuration is invalid: {detail}",
    category="task_config",
)
CORE_INTERNAL_UNEXPECTED_ERROR = EventDefinition(
    code=2011,
    name="CORE_INTERNAL_UNEXPECTED_ERROR",
    namespace=CodeNamespace.CORE,
    default_type=EventType.CRITICAL,
    title="Unexpected internal error",
    message_template="Unexpected internal Core error. See diagnostics log for details.",
    category="internal",
)

CORE_EVENT_DEFINITIONS: tuple[EventDefinition, ...] = (
    CORE_SYSTEM_CONFIG_INITIALIZED,
    CORE_SYSTEM_CONFIG_ALREADY_EXISTS,
    CORE_SYSTEM_CONFIG_LOADED,
    CORE_SYSTEM_CONFIG_VALIDATED,
    CORE_SYSTEM_CONFIG_VALUE_SET,
    CORE_SYSTEM_CONFIG_VALUE_UNSET,
    CORE_SYSTEM_CONFIG_INVALID,
    CORE_SYSTEM_CONFIG_NOT_FOUND,
    CORE_SYSTEM_CONFIG_READ_ERROR,
    CORE_SYSTEM_CONFIG_WRITE_ATOMIC_ERROR,
    CORE_SYSTEM_CONFIG_PATH_RESOLVED,
    CORE_TASK_REGISTRY_SCHEMA_INITIALIZED,
    CORE_TASK_REGISTRY_SCHEMA_VALIDATED,
    CORE_TASK_REGISTRY_DATABASE_UNAVAILABLE,
    CORE_TASK_REGISTRY_DATABASE_CORRUPTED,
    CORE_TASK_REGISTRY_FOREIGN_DATABASE,
    CORE_TASK_REGISTRY_SCHEMA_VERSION_UNSUPPORTED,
    CORE_TASK_REGISTRY_SCHEMA_INCOMPATIBLE,
    CORE_ANALYTICAL_TASK_REGISTERED,
    CORE_ANALYTICAL_TASKS_LISTED,
    CORE_ANALYTICAL_TASK_FETCHED,
    CORE_ANALYTICAL_TASK_NOT_FOUND,
    CORE_ANALYTICAL_TASK_ALREADY_EXISTS,
    CORE_ANALYTICAL_TASK_REQUEST_INVALID,
    CORE_ANALYZE_TASK_WORKSPACE_COMPENSATION_FAILED,
    CORE_ANALYTICAL_TASK_JOBS_LISTED,
    CORE_ANALYTICAL_TASK_JOB_CREATED,
    CORE_ANALYTICAL_TASK_RESTARTED,
    CORE_ANALYTICAL_TASK_CONFIG_REVISION_CREATED,
    CORE_ANALYTICAL_TASK_CONFIG_UPDATED,
    CORE_ANALYTICAL_TASK_LIFECYCLE_TRANSITION_APPLIED,
    CORE_ANALYTICAL_TASK_LIFECYCLE_TRANSITION_INVALID,
    CORE_ANALYTICAL_TASK_LIFECYCLE_TRANSITION_CONFLICT,
    CORE_ANALYTICAL_TASK_LIFECYCLE_CONCURRENT_UPDATE,
    CORE_ANALYTICAL_TASK_SECOND_ACTIVE_JOB_BLOCKED,
    CORE_TASK_REGISTRY_MIGRATION_FAILED,
    CORE_TASK_CONFIG_COMPENSATION_FAILED,
    CORE_ANALYTICAL_TASK_START_REQUESTED,
    CORE_ANALYTICAL_TASK_START_APPLIED,
    CORE_ANALYTICAL_TASK_START_ALREADY_SATISFIED,
    CORE_ANALYTICAL_TASK_START_REJECTED,
    CORE_ANALYTICAL_TASK_PAUSE_REQUESTED,
    CORE_ANALYTICAL_TASK_PAUSE_APPLIED,
    CORE_ANALYTICAL_TASK_PAUSE_ALREADY_SATISFIED,
    CORE_ANALYTICAL_TASK_PAUSE_REJECTED,
    CORE_ANALYTICAL_TASK_RESUME_REQUESTED,
    CORE_ANALYTICAL_TASK_RESUME_APPLIED,
    CORE_ANALYTICAL_TASK_RESUME_ALREADY_SATISFIED,
    CORE_ANALYTICAL_TASK_RESUME_REJECTED,
    CORE_ANALYTICAL_TASK_CANCEL_REQUESTED,
    CORE_ANALYTICAL_TASK_CANCEL_APPLIED,
    CORE_ANALYTICAL_TASK_CANCEL_ALREADY_SATISFIED,
    CORE_ANALYTICAL_TASK_CANCEL_REJECTED,
    CORE_ANALYTICAL_TASK_UPDATE_REQUESTED,
    CORE_ANALYTICAL_TASK_UPDATE_APPLIED,
    CORE_ANALYTICAL_TASK_UPDATE_ALREADY_SATISFIED,
    CORE_ANALYTICAL_TASK_UPDATE_REJECTED,
    CORE_ANALYTICAL_JOB_REPRIORITIZE_REQUESTED,
    CORE_ANALYTICAL_JOB_REPRIORITIZE_APPLIED,
    CORE_ANALYTICAL_JOB_REPRIORITIZE_ALREADY_SATISFIED,
    CORE_ANALYTICAL_JOB_REPRIORITIZE_REJECTED,
    CORE_RUNTIME_LEASE_ACQUIRED,
    CORE_RUNTIME_LEASE_RELEASED,
    CORE_RUNTIME_LEASE_CONFLICT,
    CORE_RUNTIME_LEASE_EXPIRED,
    CORE_RUNTIME_SCHEDULER_STARTED,
    CORE_RUNTIME_SCHEDULER_STOPPED,
    CORE_RUNTIME_JOB_CLAIMED,
    CORE_RUNTIME_WORKER_STARTED,
    CORE_RUNTIME_WORKER_HEARTBEAT_LOST,
    CORE_RUNTIME_WORKER_EXITED,
    CORE_RUNTIME_STAGE_STARTED,
    CORE_RUNTIME_STAGE_COMMITTED,
    CORE_RUNTIME_JOB_COMPLETED,
    CORE_RUNTIME_JOB_FAILED,
    CORE_LOCAL_NOTIFICATION_DIAGNOSTIC,
    CORE_RUNTIME_RECOVERY_STARTED,
    CORE_RUNTIME_RECOVERY_COMPLETED,
    CORE_RUNTIME_RECOVERY_FAILED,
    CORE_RUNTIME_STALE_WORKER_MESSAGE_REJECTED,
    CORE_RUNTIME_PROCESS_SPAWN_FAILED,
    CORE_RUNTIME_INTERRUPTED,
    CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PAUSE,
    CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_CANCEL,
    CORE_RUNTIME_PREEMPTION_SELECTED,
    CORE_RUNTIME_PREEMPTION_REQUESTED,
    CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PREEMPTION,
    CORE_RUNTIME_PREEMPTED_JOB_RETURNED_TO_WAITING,
    CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_DELETION,
    CORE_INPUT_SOURCE_UNSUPPORTED,
    CORE_INPUT_PATH_NOT_FOUND,
    CORE_INPUT_FILE_TYPE_UNSUPPORTED,
    CORE_INPUT_FILE_UNREADABLE,
    CORE_INPUT_FILE_EMPTY,
    CORE_INPUT_DIRECTORY_EMPTY,
    CORE_INPUT_DIRECTORY_NO_SUPPORTED_FILES,
    CORE_INPUT_NO_DATA_ACQUIRED,
    CORE_INPUT_UNSUPPORTED_FILES_SKIPPED,
    CORE_INPUT_SYMLINK_UNSUPPORTED,
    CORE_INPUT_SYMLINKS_SKIPPED,
    CORE_INPUT_DIRECTORY_DEPTH_LIMIT_REACHED,
    CORE_INPUT_DUPLICATES_SKIPPED,
    CORE_INPUT_COPY_FAILED,
    CORE_INLINE_SEQUENCE_INVALID,
    CORE_NCBI_URL_UNSUPPORTED,
    CORE_NCBI_ACCESSION_INVALID,
    CORE_NCBI_RECORD_NOT_FOUND,
    CORE_NCBI_REQUEST_FAILED,
    CORE_NCBI_REQUEST_TIMEOUT,
    CORE_NCBI_RESPONSE_EMPTY,
    CORE_NCBI_RESPONSE_INVALID,
    CORE_NCBI_PARTIAL_RESPONSE,
    CORE_INPUT_ACQUISITION_COMPLETED,
    CORE_INPUT_PROCESSING_STARTED,
    CORE_INPUT_PROCESSING_FILE_PROCESSED,
    CORE_INPUT_PROCESSING_COMPLETED,
    CORE_INPUT_PROCESSING_VALIDATION_FAILED,
    CORE_INPUT_PROCESSING_FAILED,
    CORE_ALIGNMENT_STARTED,
    CORE_ALIGNMENT_SKIPPED,
    CORE_ALIGNMENT_PREALIGNED_VALIDATION_STARTED,
    CORE_ALIGNMENT_MAFFT_AVAILABILITY_CONFIRMED,
    CORE_ALIGNMENT_MAFFT_PROCESS_STARTED,
    CORE_ALIGNMENT_MAFFT_PROCESS_COMPLETED,
    CORE_ALIGNMENT_MAFFT_PROCESS_FAILED,
    CORE_ALIGNMENT_MAFFT_STOPPED_FOR_PAUSE,
    CORE_ALIGNMENT_MAFFT_STOPPED_FOR_CANCEL,
    CORE_ALIGNMENT_MAFFT_STOPPED_FOR_SHUTDOWN,
    CORE_ALIGNMENT_RESULT_VALIDATION_FAILED,
    CORE_ALIGNMENT_RESULT_PUBLISHED,
    CORE_ALIGNMENT_COMPLETED,
    CORE_COMPARATIVE_ANALYSIS_STARTED,
    CORE_COMPARATIVE_ANALYSIS_SKIPPED,
    CORE_COMPARATIVE_ANALYSIS_PHASE_STARTED,
    CORE_COMPARATIVE_ANALYSIS_PROGRESS,
    CORE_COMPARATIVE_ANALYSIS_OPERATION_FAILED,
    CORE_COMPARATIVE_ANALYSIS_RESULT_PUBLISHED,
    CORE_COMPARATIVE_ANALYSIS_COMPLETED,
    CORE_COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS,
    CORE_COMPARATIVE_ANALYSIS_FAILED,
    CORE_DISTANCE_MATRIX_STARTED,
    CORE_DISTANCE_MATRIX_SKIPPED,
    CORE_DISTANCE_MATRIX_PROGRESS,
    CORE_DISTANCE_MATRIX_RESULT_PUBLISHED,
    CORE_DISTANCE_MATRIX_COMPLETED,
    CORE_DISTANCE_MATRIX_PARTIAL_SUCCESS,
    CORE_DISTANCE_MATRIX_FAILED,
    CORE_PHYLOGENETIC_TREE_STARTED,
    CORE_PHYLOGENETIC_TREE_SKIPPED,
    CORE_PHYLOGENETIC_TREE_PROGRESS,
    CORE_PHYLOGENETIC_TREE_RESULT_PUBLISHED,
    CORE_PHYLOGENETIC_TREE_COMPLETED,
    CORE_PHYLOGENETIC_TREE_FAILED,
    CORE_CLADE_DETECTION_STARTED,
    CORE_CLADE_DETECTION_SKIPPED,
    CORE_CLADE_DETECTION_PROGRESS,
    CORE_CLADE_DETECTION_RESULT_PUBLISHED,
    CORE_CLADE_DETECTION_COMPLETED,
    CORE_CLADE_DETECTION_FAILED,
    CORE_ANALYTICAL_TASKS_DELETE_REQUESTED,
    CORE_ANALYTICAL_TASKS_DELETE_COMPLETED,
    CORE_ANALYTICAL_TASKS_DELETE_PARTIALLY_COMPLETED,
    CORE_ANALYTICAL_TASK_DELETE_REQUESTED,
    CORE_ANALYTICAL_TASK_DELETE_APPLIED,
    CORE_ANALYTICAL_TASK_DELETE_ALREADY_SATISFIED,
    CORE_ANALYTICAL_TASK_DELETE_REJECTED,
    CORE_ANALYTICAL_TASK_WATCH_STARTED,
    CORE_ANALYTICAL_TASK_WATCH_COMPLETED,
    CORE_ANALYTICAL_TASK_WATCH_INTERRUPTED,
    CORE_ANALYTICAL_TASK_WATCH_REJECTED,
    CORE_ANALYZE_REQUEST_STARTED,
    CORE_ANALYZE_CONFIG_PARSED,
    CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED,
    CORE_ANALYZE_TASK_DIRECTORY_CREATED,
    CORE_ANALYZE_CONFIG_SAVED,
    CORE_ANALYZE_TASK_INITIALIZED,
    CORE_ANALYZE_SOURCE_NOT_FOUND,
    CORE_ANALYZE_SOURCE_UNAVAILABLE,
    CORE_ANALYZE_TASK_DIRECTORY_CREATE_FAILED,
    CORE_ANALYZE_TASK_CONFIG_SAVE_FAILED,
    CORE_ANALYZE_TASK_CONFIG_INVALID,
    CORE_INTERNAL_UNEXPECTED_ERROR,
)

CORE_EVENT_CATALOG = EventCatalog(CORE_EVENT_DEFINITIONS)
